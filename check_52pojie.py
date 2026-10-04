#!/usr/bin/env python3
"""
52pojie.cn open-registration watcher - GitHub Actions edition.
Checks once per run, alerts via Telegram on change to 'open',
persists state in state.json (cached between runs by the workflow).
"""

import os
import sys
import json
import requests
from datetime import datetime, timezone

REG_URL = "https://www.52pojie.cn/member.php?mod=register"
STATE_FILE = "state.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
EVENT = os.environ.get("GITHUB_EVENT_NAME", "")

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
}


def telegram_send(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM: not configured, message skipped:", text)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={'chat_id': TELEGRAM_CHAT_ID, 'text': text},
            timeout=20)
        print("TELEGRAM:", r.status_code, r.text[:120])
        return r.status_code == 200
    except Exception as e:
        print("TELEGRAM unreachable:", str(e)[:100])
        return False


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f).get("state")
    except FileNotFoundError:
        return None
    except Exception:
        return None

def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"state": state, "updated": datetime.now(timezone.utc).isoformat()}, f)
    except Exception as e:
        print("WARNING: failed to save state:", str(e)[:100])


def check_registration():
    """'open' | 'code' | 'closed' | 'error' - same logic as the local watcher."""
    try:
        r = requests.get(REG_URL, headers=HEADERS, timeout=30)
        r.raise_for_status()
        html = r.text
        needs_code = ("invitecode" in html) or ("邀请码" in html)
        if "开放注册" in html:
            return "open"
        if "暂停注册" in html or "停止注册" in html:
            return "closed"
        if not needs_code:
            return "open"
        return "code"
    except requests.RequestException as e:
        print("network error:", str(e)[:120])
        return "error"


def main():
    open(STATE_FILE, "a").close()  # ensure file exists for the cache save step
    prev = load_state()
    state = check_registration()
    print(f"previous state: {prev!r} | current state: {state!r}")

    if state == "error":
        print("::error::registration check failed - see log above")
        sys.exit(1)  # red X in Actions; old state is kept, next run retries

    if state == "open" and prev != "open":
        if not telegram_send(f"🔓 52pojie OPEN REGISTRATION is live!\nRegister now: {REG_URL}"):
            print("alert delivery failed - state not saved, will retry next run")
            sys.exit(1)

    if state != "open" and prev == "open":
        telegram_send("52pojie registration window appears to have closed.")

    # Manual runs (Run workflow button) send a check-in so you can verify alerts
    if EVENT == "workflow_dispatch":
        telegram_send(f"52pojie watcher check-in: state = {state}. Alerts are live.")

    save_state(state)
    print("done")


if __name__ == "__main__":
    main()
