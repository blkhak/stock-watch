"""관심 종목 감시: 1시간마다 시세·뉴스를 확인하고, 전일 대비 ±5% 이상이면 폰(ntfy)으로 알림.

실행: python stock_watch.py              (종목 입력 후 감시 시작)
      python stock_watch.py --kakao-login (카카오톡 알림 연결, 처음 한 번)
      python stock_watch.py --test-push   (폰 알림 테스트만)
"""
import ctypes
import email.utils
import html
import json
import os
import secrets
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import kakao

INTERVAL_SEC = 3600       # 확인 주기
ALERT_PCT = 5.0           # 알림 기준 등락률(%)
NEWS_PER_STOCK = 5        # 종목당 표시할 뉴스 수
NEWS_MAX_AGE_H = 24       # 이보다 오래된 뉴스는 표시 안 함

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
LOG_PATH = os.path.join(HERE, "stock_watch.log")
UA = {"User-Agent": "Mozilla/5.0"}


def http_get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8")


def log(text):
    print(text)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(text + "\n")


# ---------- 설정 ----------

def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = {}
    if not cfg.get("ntfy_topic"):
        # 토픽 이름이 곧 비밀번호 역할이므로 추측하기 어렵게 만든다
        cfg["ntfy_topic"] = "stock-" + secrets.token_hex(8)
    cfg.setdefault("stocks", [])
    save_config(cfg)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ---------- 종목 검색·시세 ----------

def search_crypto(query):
    """업비트 원화마켓에서 한글명·영문명·심볼로 코인을 찾는다."""
    q = query.strip().lower()
    items = []
    for m in json.loads(http_get("https://api.upbit.com/v1/market/all")):
        if not m["market"].startswith("KRW-"):
            continue
        symbol = m["market"][4:]
        names = (m["korean_name"].lower(), m["english_name"].lower(), symbol.lower())
        if q in names[:2] or q == names[2] or (len(q) >= 2 and q in names[0]):
            items.append({"name": m["korean_name"], "code": m["market"],
                          "reutersCode": m["market"], "nationCode": "CRYPTO",
                          "typeName": "업비트 코인", "exact": q in names})
    items.sort(key=lambda x: not x["exact"])   # 정확히 일치하는 코인을 앞으로
    return items


def search_stock(query):
    url = ("https://ac.stock.naver.com/ac?q=" + urllib.parse.quote(query)
           + "&target=stock,worldstock")
    return search_crypto(query) + json.loads(http_get(url)).get("items", [])


def fmt_num(x):
    return f"{x:,.0f}" if abs(x) >= 100 else f"{x:,.4g}"


def get_quote(stock):
    if stock["nation"] == "CRYPTO":
        # 업비트의 '전일 대비'는 매일 09:00(KST) 기준가 대비
        d = json.loads(http_get(f"https://api.upbit.com/v1/ticker?markets={stock['code']}"))[0]
        day = d["trade_date_kst"]
        return {
            "price": fmt_num(d["trade_price"]) + "원",
            "change": ("+" if d["signed_change_price"] > 0 else "") + fmt_num(d["signed_change_price"]),
            "pct": d["signed_change_rate"] * 100,
            "status": "24H",
            "traded_at": f"{day[:4]}-{day[4:6]}-{day[6:]}",
        }
    if stock["nation"] == "KOR":
        url = f"https://m.stock.naver.com/api/stock/{stock['code']}/basic"
    else:
        url = f"https://api.stock.naver.com/stock/{stock['reuters']}/basic"
    d = json.loads(http_get(url))
    return {
        "price": d.get("closePrice", "?"),
        "change": d.get("compareToPreviousClosePrice", "?"),
        "pct": float(d.get("fluctuationsRatio") or 0),
        "status": d.get("marketStatus", ""),
        "traded_at": d.get("localTradedAt", ""),
    }


def ask_stocks():
    while True:
        n = input("감시할 종목 개수: ").strip()
        if n.isdigit() and int(n) > 0:
            n = int(n)
            break
        print("  1 이상의 숫자를 입력하세요.")
    stocks = []
    while len(stocks) < n:
        q = input(f"[{len(stocks) + 1}/{n}] 종목명 또는 코드: ").strip()
        if not q:
            continue
        try:
            items = search_stock(q)
        except Exception as e:
            print(f"  검색 실패: {e}")
            continue
        if not items:
            print("  검색 결과가 없습니다.")
            continue
        items = items[:5]
        for i, it in enumerate(items, 1):
            print(f"  {i}) {it['name']} ({it['code']}, {it.get('typeName') or it.get('typeCode')})")
        pick = input("  번호 선택 [Enter=1, 0=다시 입력]: ").strip() or "1"
        if not pick.isdigit() or not 1 <= int(pick) <= len(items):
            continue
        it = items[int(pick) - 1]
        stocks.append({"name": it["name"], "code": it["code"],
                       "reuters": it.get("reutersCode") or it["code"],
                       "nation": it.get("nationCode", "KOR")})
    return stocks


# ---------- 뉴스 ----------

def get_news(name, query=None):
    """query를 주면 그대로 검색 (이름이 흔한 단어라 엉뚱한 기사가 섞이는 종목용)"""
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(query or f'"{name}"')
           + "&hl=ko&gl=KR&ceid=KR:ko")
    root = ET.fromstring(http_get(url))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=NEWS_MAX_AGE_H)
    news = []
    for item in root.iter("item"):
        try:
            pub = email.utils.parsedate_to_datetime(item.findtext("pubDate"))
        except Exception:
            continue
        if pub < cutoff:
            continue
        news.append({"title": item.findtext("title", ""),
                     "link": item.findtext("link", ""),
                     "pub": pub.astimezone()})
    news.sort(key=lambda x: x["pub"], reverse=True)
    return news


def get_naver_news(code):
    """네이버 증권 종목 뉴스. 제목은 구글 뉴스처럼 '제목 - 언론사' 형식으로 맞춘다."""
    data = json.loads(http_get(f"https://m.stock.naver.com/api/news/stock/{code}?pageSize=20&page=1"))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=NEWS_MAX_AGE_H)
    kst = timezone(timedelta(hours=9))
    news = []
    for group in data:
        for it in group["items"]:
            pub = datetime.strptime(it["datetime"], "%Y%m%d%H%M").replace(tzinfo=kst)
            if pub < cutoff:
                continue
            news.append({"title": f"{html.unescape(it['titleFull'] or it['title'])} - {it['officeName']}",
                         "link": f"https://n.news.naver.com/mnews/article/{it['officeId']}/{it['articleId']}",
                         "pub": pub.astimezone()})
    news.sort(key=lambda x: x["pub"], reverse=True)
    return news


# ---------- 알림 ----------

def push(topic, title, message, priority=4, tags=None):
    body = json.dumps({"topic": topic, "title": title, "message": message,
                       "priority": priority, "tags": tags or []}).encode("utf-8")
    req = urllib.request.Request("https://ntfy.sh/", data=body,
                                 headers={"Content-Type": "application/json", **UA})
    urllib.request.urlopen(req, timeout=15).read()


def stock_url(stock):
    if stock["nation"] == "CRYPTO":
        return f"https://upbit.com/exchange?code=CRIX.UPBIT.{stock['code']}"
    if stock["nation"] == "KOR":
        return f"https://m.stock.naver.com/domestic/stock/{stock['code']}/total"
    return f"https://m.stock.naver.com/worldstock/stock/{stock['reuters']}/total"


def notify(cfg, title, message, link=None, tags=None):
    """설정된 모든 채널로 알림을 보내고, 채널별 결과를 문자열로 돌려준다."""
    results = []
    if kakao.is_ready(cfg):
        try:
            kakao.send(cfg, f"{title}\n{message}", link)
            results.append("카카오톡 OK")
        except Exception as e:
            results.append(f"카카오톡 실패({e})")
        save_config(cfg)   # 토큰이 갱신됐을 수 있음
    if cfg.get("ntfy_enabled", True):
        try:
            push(cfg["ntfy_topic"], title, message, priority=5, tags=tags)
            results.append("ntfy OK")
        except Exception as e:
            results.append(f"ntfy 실패({e})")
    return ", ".join(results) or "설정된 알림 채널 없음"


def keep_awake():
    # 노트북이 절전으로 들어가 감시가 멈추지 않게 함 (프로그램 종료 시 자동 해제)
    if sys.platform == "win32":
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(
            ctypes.c_uint32(ES_CONTINUOUS | ES_SYSTEM_REQUIRED))


# ---------- 메인 루프 ----------

def check_once(cfg, seen_links, alerted):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    log(f"\n{'=' * 60}\n[{now}] 확인")
    for s in cfg["stocks"]:
        label = f"{s['name']}({s['code']})"
        try:
            q = get_quote(s)
            arrow = "▲" if q["pct"] > 0 else "▼" if q["pct"] < 0 else "-"
            mark = "  ★5% 이상" if abs(q["pct"]) >= ALERT_PCT else ""
            log(f"\n■ {label}  {q['price']}  {arrow}{q['change']} ({q['pct']:+.2f}%)"
                f"  [{q['status']}]{mark}")
            # 같은 거래일·같은 방향으로는 한 번만 알림
            key = f"{s['code']}|{q['traded_at'][:10]}|{'up' if q['pct'] > 0 else 'down'}"
            if abs(q["pct"]) >= ALERT_PCT and key not in alerted:
                result = notify(
                    cfg, f"[주식 알림] {s['name']} {q['pct']:+.2f}%",
                    f"{s['name']}({s['code']}) 현재 {q['price']}, 전일 대비 {q['change']} ({q['pct']:+.2f}%)",
                    link=stock_url(s),
                    tags=["chart_with_upwards_trend" if q["pct"] > 0 else "chart_with_downwards_trend"])
                alerted.add(key)
                log(f"  → 폰 알림: {result}")
        except Exception as e:
            log(f"\n■ {label}  시세 조회 실패: {e}")
        try:
            new = [n for n in get_news(s["name"]) if n["link"] not in seen_links]
            for n in new:
                seen_links.add(n["link"])
            if new:
                for n in new[:NEWS_PER_STOCK]:
                    log(f"  - [{n['pub']:%m/%d %H:%M}] {n['title']}\n    {n['link']}")
                if len(new) > NEWS_PER_STOCK:
                    log(f"  (외 {len(new) - NEWS_PER_STOCK}건)")
            else:
                log("  새 뉴스 없음")
        except Exception as e:
            log(f"  뉴스 조회 실패: {e}")


def main():
    cfg = load_config()
    if "--kakao-login" in sys.argv:
        kakao.login(cfg)
        save_config(cfg)
        print("카카오 로그인 완료. 테스트 메시지를 보냅니다.")
        print(notify(cfg, "[주식 알림] 연결 완료", "카카오톡 알림이 정상 동작합니다."))
        return

    if "--test-push" in sys.argv:
        print(notify(cfg, "[주식 알림] 테스트", "주식 감시 프로그램 알림이 정상 동작합니다."))
        return

    print("알림 채널: " + ", ".join(
        (["카카오톡"] if kakao.is_ready(cfg) else [])
        + ([f"ntfy({cfg['ntfy_topic']})"] if cfg.get("ntfy_enabled", True) else [])))

    if cfg["stocks"]:
        print("지난번 종목: " + ", ".join(s["name"] for s in cfg["stocks"]))
        if input("그대로 쓸까요? [Y/n]: ").strip().lower() in ("n", "no", "아니"):
            cfg["stocks"] = ask_stocks()
    else:
        cfg["stocks"] = ask_stocks()
    save_config(cfg)

    keep_awake()
    alerted = set(cfg.get("alerted", []))
    seen_links = set()
    print(f"\n{INTERVAL_SEC // 60}분마다 확인합니다. 끝내려면 Ctrl+C")
    try:
        while True:
            check_once(cfg, seen_links, alerted)
            cfg["alerted"] = sorted(alerted)[-200:]
            save_config(cfg)
            nxt = datetime.now() + timedelta(seconds=INTERVAL_SEC)
            print(f"\n다음 확인: {nxt:%H:%M}")
            time.sleep(INTERVAL_SEC)
    except KeyboardInterrupt:
        print("\n종료합니다.")


if __name__ == "__main__":
    main()
