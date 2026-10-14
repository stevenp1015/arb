#!/usr/bin/env python3
"""
Send-only Telegram alerts. Token + chat_id come from /root/carry/tg.env
(TG_TOKEN, TG_CHAT) — same send-only pattern the other box bots use, so
it never calls getUpdates and never conflicts with the desktop plugin.
"""
import os, json, urllib.request, urllib.parse

TOKEN = os.environ.get("TG_TOKEN", "")
CHAT = os.environ.get("TG_CHAT", "")


def send(text, silent=False):
    if not TOKEN or not CHAT:
        print(f"[tg disabled] {text}")
        return False
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHAT, "text": text,
        "disable_notification": "true" if silent else "false",
    }).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=12) as r:
            return json.loads(r.read()).get("ok", False)
    except Exception as e:
        print(f"[tg error] {e}")
        return False


if __name__ == "__main__":
    import sys
    send(" ".join(sys.argv[1:]) or "carry: telegram test")
