"""GitHub Actions에서 실행되는 감시 스크립트 (평일 낮엔 1시간, 밤·주말엔 3시간마다).

- stocks.json의 종목 시세를 확인해 전일 대비 ±ALERT_PCT% 이상이면 텔레그램 알림 (같은 날·같은 방향 1회)
- 직전 실행 이후 새로 나온 뉴스(네이버 증권 + 구글 뉴스)를 종목별로 최대 3건씩 보냄
  (stocks.json에서 "news": false인 종목은 제외)
- 국내 종목은 DART에 새로 올라온 공시도 종목별로 최대 3건씩 보냄 (DART_API_KEY가 있을 때만)
- 상태(state/state.json)는 워크플로가 저장소에 커밋

필요한 환경변수(GitHub Secrets): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (설정 방법은 telegram.py 참고)
선택: DART_API_KEY (https://opendart.fss.or.kr 에서 발급)
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import dart
import telegram
import stock_watch as sw

ALERT_PCT = 5.0
NEWS_PER_STOCK = 3
NEWS_MAX_AGE_H = 4      # 실행 간격(최대 3시간)보다 길어야 함. 첫 실행이나 실행이 밀렸을 때 오래된 뉴스가 쏟아지지 않게
SEEN_KEEP = 1500
ALERTED_KEEP = 300
DART_PER_STOCK = 3
DART_SEEN_KEEP = 500

HERE = os.path.dirname(os.path.abspath(__file__))
STOCKS_PATH = os.path.join(HERE, "stocks.json")
STATE_PATH = os.path.join(HERE, "state", "state.json")


# ---------- 뉴스 ----------

def _norm(title):
    return "".join(ch for ch in title.rpartition(" - ")[0] or title if ch.isalnum())


def collect_news(s):
    """국내 종목은 네이버 증권 뉴스를 우선하고, 구글 뉴스에서 같은 제목의 기사는 뺀다."""
    news, errs = [], []
    if s["nation"] == "KOR":
        try:
            news += sw.get_naver_news(s["code"])
        except Exception as e:
            errs.append(f"네이버: {e}")
    try:
        titles = {_norm(n["title"]) for n in news}
        news += [n for n in sw.get_news(s["name"], s.get("news_query")) if _norm(n["title"]) not in titles]
    except Exception as e:
        errs.append(f"구글: {e}")
    if errs and not news:
        raise RuntimeError(", ".join(errs))
    for e in errs:
        print("  뉴스 일부 실패 -", e)
    return sorted(news, key=lambda n: n["pub"], reverse=True)


def test_items(stocks, dart_key, dart_corp):
    """수동 실행 때 test 옵션을 켜면 실제 기사·공시 링크로 알림 모양과 링크 연결을 확인한다."""
    s = next(x for x in stocks if x["nation"] == "KOR")
    keep, sw.NEWS_MAX_AGE_H = sw.NEWS_MAX_AGE_H, 24 * 365   # 오래된 기사라도 링크 확인용으로 쓴다
    try:
        naver = sw.get_naver_news(s["code"])[:1]
        google = sw.get_news(s["name"], s.get("news_query"))[:1]
    finally:
        sw.NEWS_MAX_AGE_H = keep
    items = []
    for n in naver + google:
        title, _, press = n["title"].rpartition(" - ")
        items.append((title or n["title"], f"{press} · {n['pub']:%m/%d %H:%M}", n["link"]))
    corp = dart_corp.get(s["code"])
    if dart_key and corp:
        f = (dart.filings(dart_key, corp, days=365) or [None])[0]
        if f:
            items.append((f["report_nm"].strip(), f"공시 · {f['rcept_dt'][4:6]}/{f['rcept_dt'][6:]}",
                          dart.filing_url(f["rcept_no"])))
    if not items:
        raise RuntimeError("테스트에 쓸 기사·공시를 찾지 못함")
    print("테스트 알림:", [i[2] for i in items])
    return s, items


# ---------- 본체 ----------

def main():
    tg_token, tg_chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    with open(STOCKS_PATH, encoding="utf-8") as f:
        stocks = json.load(f)
    state = {"alerted": [], "seen": [], "dart_corp": {}, "dart_seen": []}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            state.update(json.load(f))
    alerted, seen = set(state["alerted"]), list(state["seen"])
    dart_key = os.environ.get("DART_API_KEY", "")
    dart_corp, dart_seen = dict(state["dart_corp"]), list(state["dart_seen"])
    dart_new_corp = set()   # 이번에 처음 감시하는 종목은 기존 공시를 본 것으로만 기록
    if dart_key:
        missing = [s["code"] for s in stocks if s["nation"] == "KOR" and s["code"] not in dart_corp]
        if missing:
            try:
                found = dart.corp_codes(dart_key, missing)
                for c in missing:   # 못 찾은 종목도 None으로 기록해 매번 다시 받지 않게
                    dart_corp[c] = found.get(c)
                dart_new_corp.update(found)
                print("DART 고유번호:", found)
            except Exception as e:
                print("DART 고유번호 조회 실패:", e)
    else:
        print("DART_API_KEY 없음 - 공시 확인 건너뜀")
    seen_set, dart_seen_set = set(seen), set(dart_seen)
    errors = []

    def push(title, message, links):
        try:
            telegram.send(tg_token, tg_chat, title, message, links)
        except Exception as e:
            errors.append(str(e))
            print("  텔레그램 전송 실패:", e)

    def alert_list(header, items):
        """목록 알림. items: [(제목, 설명, 링크)]"""
        push(header, "\n".join(f"{i + 1}. {t} ({d})" for i, (t, d, _) in enumerate(items)),
             [(f"{i + 1}번 보기", u) for i, (_, _, u) in enumerate(items)])

    if os.environ.get("TEST_ALERT") == "true":
        try:
            s, items = test_items(stocks, dart_key, dart_corp)
            alert_list(f"🔔 [테스트] {s['name']} 링크 확인", items)
        except Exception as e:
            errors.append(str(e))
            print("테스트 알림 실패:", e)

    for s in stocks:
        label = f"{s['name']}({s['code']})"
        try:
            q = sw.get_quote(s)
            print(f"{label} {q['price']} {q['change']} ({q['pct']:+.2f}%) [{q['status']}]")
            k = f"{s['code']}|{q['traded_at'][:10]}|{'up' if q['pct'] > 0 else 'down'}"
            if abs(q["pct"]) >= ALERT_PCT and k not in alerted:
                arrow = "📈" if q["pct"] > 0 else "📉"
                title = f"{arrow} [주식 알림] {s['name']} {q['pct']:+.2f}%"
                body = f"현재 {q['price']}, 전일 대비 {q['change']}"
                push(title, body, [("시세 보기", sw.stock_url(s))])
                alerted.add(k)
        except Exception as e:
            print(f"{label} 시세 실패: {e}")

        if s.get("news") is not False:
            try:
                cutoff = datetime.now(timezone.utc) - timedelta(hours=NEWS_MAX_AGE_H)
                news = collect_news(s)
                new = [n for n in news if n["link"] not in seen_set and n["pub"] >= cutoff]
                for n in news:   # 오래된 것도 본 것으로 기록
                    if n["link"] not in seen_set:
                        seen_set.add(n["link"])
                        seen.append(n["link"])
                if new:
                    items = []
                    for n in new[:NEWS_PER_STOCK]:
                        title, _, press = n["title"].rpartition(" - ")
                        items.append((title or n["title"], f"{press} · {n['pub']:%m/%d %H:%M}", n["link"]))
                    extra = f" 외 {len(new) - NEWS_PER_STOCK}건" if len(new) > NEWS_PER_STOCK else ""
                    alert_list(f"📰 {s['name']} 새 뉴스{extra}", items)
                    print(f"  새 뉴스 {len(new)}건")
            except Exception as e:
                print(f"{label} 뉴스 실패: {e}")

        corp = dart_corp.get(s["code"]) if dart_key else None
        if corp:
            try:
                new = [f for f in dart.filings(dart_key, corp) if f["rcept_no"] not in dart_seen_set]
                for f in new:
                    dart_seen_set.add(f["rcept_no"])
                    dart_seen.append(f["rcept_no"])
                if new and s["code"] not in dart_new_corp:
                    items = [(f["report_nm"].strip(), f"{f['flr_nm']} · {f['rcept_dt'][4:6]}/{f['rcept_dt'][6:]}",
                              dart.filing_url(f["rcept_no"])) for f in new[:DART_PER_STOCK]]
                    extra = f" 외 {len(new) - DART_PER_STOCK}건" if len(new) > DART_PER_STOCK else ""
                    alert_list(f"📑 {s['name']} 새 공시{extra}", items)
                    print(f"  새 공시 {len(new)}건")
            except Exception as e:
                print(f"{label} 공시 실패: {e}")

    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"alerted": sorted(alerted, key=lambda x: x.split("|")[1])[-ALERTED_KEEP:], "seen": seen[-SEEN_KEEP:],
                   "dart_corp": dart_corp, "dart_seen": dart_seen[-DART_SEEN_KEEP:]},
                  f, ensure_ascii=False, indent=1)
    if errors:
        sys.exit("알림 전송 오류: " + errors[0])


if __name__ == "__main__":
    main()
