"""GitHub Actions에서 1시간마다 한 번 실행되는 감시 스크립트.

- stocks.json의 종목 시세를 확인해 전일 대비 ±ALERT_PCT% 이상이면 카톡 알림 (같은 날·같은 방향 1회)
- 직전 실행 이후 새로 나온 뉴스(네이버 증권 + 구글 뉴스)를 종목별로 최대 3건씩 카톡으로 보냄
  (stocks.json에서 "news": false인 종목은 제외)
- 국내 종목은 DART에 새로 올라온 공시도 종목별로 최대 3건씩 카톡으로 보냄 (DART_API_KEY가 있을 때만)
- 상태(state/state.json)와 암호화한 카카오 토큰(state/token.enc)은 워크플로가 저장소에 커밋

필요한 환경변수(GitHub Secrets): KAKAO_REST_KEY, KAKAO_CLIENT_SECRET, KAKAO_REFRESH_TOKEN, TOKEN_KEY
선택: DART_API_KEY (https://opendart.fss.or.kr 에서 발급)
"""
import base64
import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import dart
import kakao
import stock_watch as sw

ALERT_PCT = 5.0
NEWS_PER_STOCK = 3
NEWS_MAX_AGE_H = 2      # 첫 실행이나 실행이 밀렸을 때 오래된 뉴스가 쏟아지지 않게
SEEN_KEEP = 1500
ALERTED_KEEP = 300
DART_PER_STOCK = 3
DART_SEEN_KEEP = 500

HERE = os.path.dirname(os.path.abspath(__file__))
STOCKS_PATH = os.path.join(HERE, "stocks.json")
STATE_PATH = os.path.join(HERE, "state", "state.json")
TOKEN_PATH = os.path.join(HERE, "state", "token.enc")


# ---------- 토큰 암호화 (저장소에 평문으로 남기지 않기 위함) ----------

def _keystream(key, nonce, n):
    out = b""
    i = 0
    while len(out) < n:
        out += hashlib.sha256(key + nonce + i.to_bytes(4, "big")).digest()
        i += 1
    return out[:n]


def encrypt(key, data):
    nonce = os.urandom(16)
    ct = bytes(a ^ b for a, b in zip(data, _keystream(key, nonce, len(data))))
    tag = hmac.new(key, nonce + ct, hashlib.sha256).digest()
    return base64.b64encode(nonce + tag + ct).decode()


def decrypt(key, text):
    raw = base64.b64decode(text)
    nonce, tag, ct = raw[:16], raw[16:48], raw[48:]
    if not hmac.compare_digest(tag, hmac.new(key, nonce + ct, hashlib.sha256).digest()):
        raise ValueError("token.enc 검증 실패 (TOKEN_KEY가 바뀌었나요?)")
    return bytes(a ^ b for a, b in zip(ct, _keystream(key, nonce, len(ct))))


def load_kakao_cfg(key):
    kc = {"rest_api_key": os.environ["KAKAO_REST_KEY"],
          "client_secret": os.environ.get("KAKAO_CLIENT_SECRET", ""),
          "refresh_token": os.environ.get("KAKAO_REFRESH_TOKEN", "")}
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, encoding="utf-8") as f:
            saved = json.loads(decrypt(key, f.read().strip()))
        # Secret을 새 토큰으로 바꿔 넣었으면 그쪽을 우선 (재로그인한 경우)
        if saved.get("seed") == hashlib.sha256(kc["refresh_token"].encode()).hexdigest():
            kc["refresh_token"] = saved["refresh_token"]
    return {"kakao": kc}


def save_kakao_cfg(key, cfg):
    kc = cfg["kakao"]
    seed = hashlib.sha256(os.environ.get("KAKAO_REFRESH_TOKEN", "").encode()).hexdigest()
    data = json.dumps({"refresh_token": kc["refresh_token"], "seed": seed}).encode()
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        f.write(encrypt(key, data) + "\n")


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


# ---------- 본체 ----------

def main():
    key = hashlib.sha256(os.environ["TOKEN_KEY"].encode()).digest()
    cfg = load_kakao_cfg(key)
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
    # 매 실행마다 토큰을 갱신해 두어야 알림이 뜸할 때도 리프레시 토큰이 만료되지 않는다
    kakao._refresh(cfg["kakao"])
    seen_set, dart_seen_set = set(seen), set(dart_seen)
    errors = []

    def deliver(fn, *args):
        try:
            fn(cfg, *args)
        except Exception as e:
            errors.append(str(e))
            print("  카톡 전송 실패:", e)

    for s in stocks:
        label = f"{s['name']}({s['code']})"
        try:
            q = sw.get_quote(s)
            print(f"{label} {q['price']} {q['change']} ({q['pct']:+.2f}%) [{q['status']}]")
            k = f"{s['code']}|{q['traded_at'][:10]}|{'up' if q['pct'] > 0 else 'down'}"
            if abs(q["pct"]) >= ALERT_PCT and k not in alerted:
                arrow = "📈" if q["pct"] > 0 else "📉"
                deliver(kakao.send,
                        f"{arrow} [주식 알림] {s['name']} {q['pct']:+.2f}%\n"
                        f"현재 {q['price']}, 전일 대비 {q['change']}",
                        sw.stock_url(s))
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
                    deliver(kakao.send_list, f"📰 {s['name']} 새 뉴스{extra}", items, sw.stock_url(s))
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
                    deliver(kakao.send_list, f"📑 {s['name']} 새 공시{extra}", items, items[0][2])
                    print(f"  새 공시 {len(new)}건")
            except Exception as e:
                print(f"{label} 공시 실패: {e}")

    save_kakao_cfg(key, cfg)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"alerted": sorted(alerted, key=lambda x: x.split("|")[1])[-ALERTED_KEEP:], "seen": seen[-SEEN_KEEP:],
                   "dart_corp": dart_corp, "dart_seen": dart_seen[-DART_SEEN_KEEP:]},
                  f, ensure_ascii=False, indent=1)
    if errors:
        sys.exit("카톡 전송 오류: " + errors[0])


if __name__ == "__main__":
    main()
