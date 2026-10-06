#!/usr/bin/env python3
"""
52pojie.cn open-registration watcher - GitHub Actions edition.
Alerts via Telegram on change to 'open'. With POLL_DURATION_SECONDS set it
polls every POLL_INTERVAL_SECONDS for that long (the workflow chains runs
back to back for near-continuous coverage); otherwise it checks once.
Persists state in state.json (cached between runs by the workflow).
"""

import os
import sys
import json
import time
import requests
from datetime import datetime, timedelta, timezone

REG_URL = "https://www.52pojie.cn/member.php?mod=register"
STATE_FILE = "state.json"
HEARTBEAT_EVERY = timedelta(hours=23)  # scheduled runs are sparse; 23h avoids drifting past a day

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
EVENT = os.environ.get("GITHUB_EVENT_NAME", "")
POLL_DURATION = int(os.environ.get("POLL_DURATION_SECONDS") or 0)
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS") or 60)
ERROR_ALERT_AFTER = 30  # consecutive failed checks before warning that the watcher is blind
# A manual "Run workflow" click checks once and sends a check-in; looping runs send heartbeats
IS_MANUAL = EVENT == "workflow_dispatch" and POLL_DURATION == 0

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
    """Returns the saved state dict ({} if missing or unreadable)."""
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def save_state(state, last_heartbeat=None):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"state": state,
                       "updated": datetime.now(timezone.utc).isoformat(),
                       "last_heartbeat": last_heartbeat}, f)
    except Exception as e:
        print("WARNING: failed to save state:", str(e)[:100])


def heartbeat_due(last_heartbeat):
    try:
        last = datetime.fromisoformat(last_heartbeat)
    except (TypeError, ValueError):
        return True
    return datetime.now(timezone.utc) - last >= HEARTBEAT_EVERY


def check_registration():
    """'open' | 'code' | 'closed' | 'error' - same logic as the local watcher."""
    try:
        r = requests.get(REG_URL, headers=HEADERS, timeout=30)
        r.raise_for_status()
        r.encoding = 'gbk'
        html = r.text

        if "吾爱破解" not in html:
            print("WAF block or incomplete page loaded")
            return "error"

        needs_code = ("invitecode" in html) or ("邀请码" in html)
        if "开放注册" in html:
            return "open"
        if "暂停注册" in html or "停止注册" in html:
            return "closed"
        if not needs_code:
            return "open"
        return "code"
    except requests.exceptions.HTTPError as e:
        print(f"HTTP error {e.response.status_code}: {str(e)[:120]}")
        return "error"
    except requests.RequestException as e:
        print("network error:", str(e)[:120])
        return "error"


def main():
    open(STATE_FILE, "a").close()  # ensure file exists for the cache save step
    saved = load_state()
    prev = saved.get("state")
    last_heartbeat = saved.get("last_heartbeat")
    deadline = time.monotonic() + POLL_DURATION
    errors = 0
    error_alerted = False

    while True:
        state = check_registration()
        print(f"previous state: {prev!r} | current state: {state!r}")

        if state == "error":
            errors += 1
            if POLL_DURATION == 0:
                print("::error::registration check failed - see log above")
                sys.exit(1)  # red X in Actions; old state is kept, next run retries
            if errors >= ERROR_ALERT_AFTER and not error_alerted:
                error_alerted = telegram_send(
                    f"52pojie watcher: {errors} checks in a row failed (site blocking or down?). "
                    "Open windows can't be detected until this recovers.")
        else:
            errors = 0
            error_alerted = False
            alerted = False
            open_alert_failed = False
            if state == "open" and prev != "open":
                alerted = telegram_send(f"🔓 52pojie OPEN REGISTRATION is live!\nRegister now: {REG_URL}")
                open_alert_failed = not alerted

            if open_alert_failed:
                print("alert delivery failed - state not saved, will retry")
                if POLL_DURATION == 0:
                    sys.exit(1)
            else:
                if state != "open" and prev == "open":
                    alerted = telegram_send("52pojie registration window appears to have closed.")

                if IS_MANUAL:
                    telegram_send(f"52pojie watcher check-in: state = {state}. Alerts are live.")
                elif alerted:
                    # A state-change alert already proves liveness; a "no change" heartbeat next to it would be wrong
                    last_heartbeat = datetime.now(timezone.utc).isoformat()
                elif heartbeat_due(last_heartbeat):
                    # Daily: silence then means "no change", not "broken"
                    if telegram_send(f"52pojie watcher heartbeat: state = {state}, no change. Still watching."):
                        last_heartbeat = datetime.now(timezone.utc).isoformat()

                prev = state
                save_state(prev, last_heartbeat)

        if time.monotonic() + POLL_INTERVAL >= deadline:
            break
        time.sleep(POLL_INTERVAL)

    print("done")


if __name__ == "__main__":
    main()
