"""텔레그램 봇으로 알림 보내기. 카톡 '나와의 채팅'과 달리 소리·진동이 난다.

1. 텔레그램에서 @BotFather에게 /newbot → 봇 이름 정하면 토큰을 준다
2. 만든 봇과의 대화방에서 아무 말이나 한 번 보낸다
3. `python telegram.py <토큰>` 으로 chat_id를 확인한다
"""
import html
import json
import sys
import urllib.error
import urllib.request

API = "https://api.telegram.org/bot{token}/{method}"


def _call(token, method, data):
    req = urllib.request.Request(API.format(token=token, method=method),
                                 data=json.dumps(data).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))["result"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"텔레그램 오류 {e.code}: {e.read().decode('utf-8', 'replace')}") from None


def send(token, chat_id, title, message="", links=()):
    """제목은 굵게, links: [(버튼 이름, 주소)]는 메시지 아래 버튼으로 붙인다."""
    text = f"<b>{html.escape(title)}</b>" + (f"\n{html.escape(message)}" if message else "")
    data = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML",
            "disable_web_page_preview": True}
    if links:
        data["reply_markup"] = {"inline_keyboard": [[{"text": label, "url": url}] for label, url in links]}
    _call(token, "sendMessage", data)


def chat_ids(token):
    """봇에게 최근 말을 건 대화방들의 (chat_id, 이름)"""
    found = {}
    for u in _call(token, "getUpdates", {}):
        chat = (u.get("message") or u.get("my_chat_member") or {}).get("chat")
        if chat:
            found[chat["id"]] = chat.get("first_name") or chat.get("title") or ""
    return list(found.items())


if __name__ == "__main__":
    ids = chat_ids(sys.argv[1])
    print(ids or "봇과의 대화방에서 아무 메시지나 먼저 보내 주세요")
