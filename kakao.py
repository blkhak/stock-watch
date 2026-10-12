"""카카오톡 '나에게 보내기' 알림.

처음 한 번 `python stock_watch.py --kakao-login`으로 로그인하면 토큰이 config.json에 저장되고,
이후에는 만료 전에 자동으로 갱신한다.
"""
import http.server
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

REDIRECT_URI = "http://localhost:5000/kakao"
AUTH_URL = "https://kauth.kakao.com/oauth/authorize"
TOKEN_URL = "https://kauth.kakao.com/oauth/token"
MEMO_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"


def _post(url, data, token=None):
    headers = {"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode("utf-8"),
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"카카오 오류 {e.code}: {e.read().decode('utf-8', 'replace')}") from None


def _save_token(kc, tok):
    kc["access_token"] = tok["access_token"]
    kc["access_expires_at"] = int(time.time()) + int(tok.get("expires_in", 0))
    if tok.get("refresh_token"):   # 갱신 때는 만료 1달 전부터만 새로 내려옴
        kc["refresh_token"] = tok["refresh_token"]
        kc["refresh_expires_at"] = int(time.time()) + int(tok.get("refresh_token_expires_in", 0))


def _wait_for_code():
    """브라우저가 REDIRECT_URI로 돌아올 때 붙여 오는 인가 코드를 받는다."""
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in qs.items()})
            ok = "code" in result
            msg = "로그인 완료. 이 창을 닫으셔도 됩니다." if ok else f"로그인 실패: {result}"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<h2>{msg}</h2>".encode("utf-8"))

        def log_message(self, *args):
            pass

    with http.server.HTTPServer(("localhost", 5000), Handler) as srv:
        srv.timeout = 300
        srv.handle_request()
    if "code" not in result:
        raise RuntimeError(f"인가 코드를 받지 못했습니다: {result or '시간 초과'}")
    return result["code"]


def login(cfg):
    kc = cfg.setdefault("kakao", {})
    if not kc.get("rest_api_key"):
        kc["rest_api_key"] = input("REST API 키: ").strip()
        kc["client_secret"] = input("클라이언트 시크릿 (사용 안 하면 Enter): ").strip()
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": kc["rest_api_key"], "redirect_uri": REDIRECT_URI,
        "response_type": "code", "scope": "talk_message"})
    print("브라우저에서 카카오 로그인 후 '동의하고 계속하기'를 눌러 주세요...")
    webbrowser.open(url)
    code = _wait_for_code()
    data = {"grant_type": "authorization_code", "client_id": kc["rest_api_key"],
            "redirect_uri": REDIRECT_URI, "code": code}
    if kc.get("client_secret"):
        data["client_secret"] = kc["client_secret"]
    _save_token(kc, _post(TOKEN_URL, data))


def _refresh(kc):
    data = {"grant_type": "refresh_token", "client_id": kc["rest_api_key"],
            "refresh_token": kc["refresh_token"]}
    if kc.get("client_secret"):
        data["client_secret"] = kc["client_secret"]
    _save_token(kc, _post(TOKEN_URL, data))


def is_ready(cfg):
    return bool(cfg.get("kakao", {}).get("refresh_token"))


def send_template(cfg, template):
    """나와의 채팅으로 메시지를 보낸다. 토큰이 갱신되면 cfg가 바뀌므로 호출 측에서 저장할 것."""
    kc = cfg["kakao"]
    if time.time() > kc.get("access_expires_at", 0) - 600:
        _refresh(kc)
    res = _post(MEMO_URL, {"template_object": json.dumps(template, ensure_ascii=False)},
                token=kc["access_token"])
    if res.get("result_code") != 0:
        raise RuntimeError(f"카카오 전송 실패: {res}")


def _link(url):
    return {"web_url": url, "mobile_web_url": url}


def send(cfg, text, link=None, button="시세 보기"):
    link = link or "https://m.stock.naver.com"
    send_template(cfg, {"object_type": "text", "text": text[:200],
                        "link": _link(link), "button_title": button})


def send_list(cfg, header, items, header_link):
    """제목+링크 목록 메시지. items: [(제목, 설명, 링크)], 2~3개일 때만 리스트 형식을 쓸 수 있다."""
    if len(items) < 2:
        title, desc, url = items[0]
        return send(cfg, f"{header}\n{title}\n{desc}", url, button="기사 보기")
    send_template(cfg, {
        "object_type": "list", "header_title": header[:200], "header_link": _link(header_link),
        "contents": [{"title": t[:100], "description": d[:100], "link": _link(u)}
                     for t, d, u in items[:3]],
    })
