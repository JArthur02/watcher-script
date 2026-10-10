#!/usr/bin/env python3
"""
Telegram -> Venice AI chat bot.

Long-polls a Telegram bot and answers your messages with a Venice chat model
(Venice's API is OpenAI-compatible). Only the Telegram user IDs listed in
ALLOWED_TELEGRAM_USER_IDS are served; everyone else is ignored silently.

The repo is public, so Actions logs are public too: message text and model
replies are never printed. Conversation history lives in memory only.

Commands: /models [word]  /model [n|id]  /reset  /help

Env:
  VENICE_BOT_TOKEN           Telegram bot token from @BotFather (required)
  VENICE_API_KEY             Venice API key (required)
  ALLOWED_TELEGRAM_USER_IDS  comma-separated numeric user IDs. If empty, the bot
                             only replies with the sender's ID (setup mode) and
                             never calls Venice.
  VENICE_DEFAULT_MODEL       optional model id used until you pick one with /model
  POLL_DURATION_SECONDS      run this long then exit (the workflow chains runs);
                             0 = run until killed
  MAX_HISTORY_MESSAGES       messages of context kept per chat (default 20)
"""

import os
import re
import sys
import json
import time
import threading
import requests

TG_TOKEN = os.environ.get("VENICE_BOT_TOKEN", "").strip()
VENICE_KEY = os.environ.get("VENICE_API_KEY", "").strip()
ALLOWED = {int(x) for x in re.split(r"[,\s]+", os.environ.get("ALLOWED_TELEGRAM_USER_IDS", "")) if x.isdigit()}
DEFAULT_MODEL = os.environ.get("VENICE_DEFAULT_MODEL", "").strip()
POLL_DURATION = int(os.environ.get("POLL_DURATION_SECONDS") or 0)
MAX_HISTORY = int(os.environ.get("MAX_HISTORY_MESSAGES") or 20)
STATE_FILE = os.environ.get("STATE_FILE", "venice_state.json")
# Overridable so the bot can be tested against local mock servers
TG_API = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
VENICE_BASE = os.environ.get("VENICE_BASE_URL", "https://api.venice.ai/api/v1").rstrip("/")

LONG_POLL_SECONDS = 50
VENICE_TIMEOUT = 180
MODELS_TTL = 600
TG_MAX_CHARS = 4000  # Telegram's limit is 4096

history = {}       # chat_id -> [{"role", "content"}]
_models_cache = {"at": 0.0, "items": []}


class TelegramError(Exception):
    def __init__(self, status, description="", retry_after=0):
        super().__init__(f"telegram {status}")
        self.status = status
        self.description = description
        self.retry_after = retry_after


# --- state (only the chosen model; nothing sensitive) -----------------------

def load_state():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print("WARNING: failed to save state:", type(e).__name__)


# --- Telegram ---------------------------------------------------------------

def tg(method, **params):
    params = {k: v for k, v in params.items() if v is not None}
    timeout = params.get("timeout", 0) + 15
    r = requests.post(f"{TG_API}/bot{TG_TOKEN}/{method}", json=params, timeout=timeout)
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code != 200 or not data.get("ok"):
        raise TelegramError(r.status_code, str(data.get("description", ""))[:120],
                            (data.get("parameters") or {}).get("retry_after", 0))
    return data["result"]


def chunk(text):
    """Split into Telegram-sized pieces, preferring paragraph/line boundaries."""
    parts = []
    while len(text) > TG_MAX_CHARS:
        cut = max(text.rfind("\n\n", 0, TG_MAX_CHARS), text.rfind("\n", 0, TG_MAX_CHARS))
        if cut < TG_MAX_CHARS // 2:
            cut = TG_MAX_CHARS
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    parts.append(text)
    return [p for p in parts if p.strip()] or ["(empty reply)"]


def send(chat_id, text):
    for part in chunk(text):
        for attempt in (1, 2):
            try:
                tg("sendMessage", chat_id=chat_id, text=part)
                break
            except TelegramError as e:
                if e.status == 429 and attempt == 1:
                    time.sleep(min(e.retry_after or 3, 30))
                    continue
                print("sendMessage failed:", e.status)
                return


def with_typing(chat_id, fn):
    """Keep the 'typing…' indicator alive while a slow model call runs."""
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                tg("sendChatAction", chat_id=chat_id, action="typing")
            except Exception:
                pass
            stop.wait(4)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    try:
        return fn()
    finally:
        stop.set()


# --- Venice -----------------------------------------------------------------

def venice_headers():
    return {"Authorization": f"Bearer {VENICE_KEY}", "Content-Type": "application/json"}


def venice_error_text(r):
    try:
        body = r.json()
        err = body.get("error", body)
        msg = err.get("message") if isinstance(err, dict) else err
    except ValueError:
        msg = ""
    hints = {401: "the API key was rejected", 402: "insufficient Venice balance", 429: "rate limited, try again shortly"}
    return f"Venice error {r.status_code}" + (f" ({hints[r.status_code]})" if r.status_code in hints else "") + \
        (f": {str(msg)[:200]}" if msg else "")


def list_models(force=False):
    """[(id, display name)] of Venice text models; cached briefly."""
    if not force and _models_cache["items"] and time.monotonic() - _models_cache["at"] < MODELS_TTL:
        return _models_cache["items"]
    r = requests.get(f"{VENICE_BASE}/models", params={"type": "text"}, headers=venice_headers(), timeout=30)
    if r.status_code != 200:
        raise RuntimeError(venice_error_text(r))
    items = []
    for m in r.json().get("data", []):
        if not isinstance(m, dict) or not m.get("id"):
            continue
        name = (m.get("model_spec") or {}).get("name") or ""
        items.append((m["id"], name))
    items.sort(key=lambda x: x[0])
    _models_cache.update(at=time.monotonic(), items=items)
    return items


def venice_chat(model, messages):
    r = requests.post(f"{VENICE_BASE}/chat/completions", headers=venice_headers(),
                      json={"model": model, "messages": messages}, timeout=VENICE_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(venice_error_text(r))
    content = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
    return content or "(empty reply)"


# --- message handling -------------------------------------------------------

HELP = ("Venice bot. Just send a message to chat with the selected model.\n\n"
        "/models [word] - list text models (optionally filtered, e.g. /models claude)\n"
        "/model - show the current model\n"
        "/model <number|id> - switch model (number from /models)\n"
        "/reset - clear this conversation\n\n"
        "Conversation memory is kept only while the bot process runs, so it can reset every few hours.")


def current_model(state):
    return state.get("model") or DEFAULT_MODEL


def cmd_models(chat_id, state, query=""):
    try:
        items = list_models(force=True)
    except Exception as e:
        send(chat_id, f"Couldn't list models: {e}")
        return
    if not items:
        send(chat_id, "Venice returned no text models.")
        return
    cur = current_model(state)
    q = query.lower()
    # Numbers always refer to the full list, so /model <number> works after filtering too
    lines = [f"{i}. {mid}" + (f" - {name}" if name and name != mid else "") + (" (current)" if mid == cur else "")
             for i, (mid, name) in enumerate(items, 1) if not q or q in mid.lower() or q in name.lower()]
    if not lines:
        send(chat_id, f"No models match '{query}'. Send /models for the full list.")
        return
    head = f"Text models matching '{query}':" if q else "Text models (filter with /models <word>):"
    send(chat_id, head + "\n" + "\n".join(lines) + "\n\nSwitch with /model <number or id>")


def cmd_model(chat_id, arg, state):
    if not arg:
        cur = current_model(state)
        send(chat_id, f"Current model: {cur}" if cur else "No model selected. Send /models, then /model <number>.")
        return
    try:
        items = list_models()
    except Exception:
        items = []  # can't validate; accept the id as typed
    ids = [mid for mid, _ in items]
    if arg.isdigit():
        n = int(arg)
        if not 1 <= n <= len(ids):
            send(chat_id, "No model with that number. Send /models to see the list.")
            return
        choice = ids[n - 1]
    elif ids and arg not in ids:
        send(chat_id, "Unknown model id. Send /models to see the list.")
        return
    else:
        choice = arg
    state["model"] = choice
    save_state(state)
    history.pop(chat_id, None)  # a fresh context for the new model
    send(chat_id, f"Model set to {choice}. Conversation cleared.")


def chat_turn(chat_id, text, state):
    model = current_model(state)
    if not model:
        send(chat_id, "No model selected yet. Send /models, then /model <number>.")
        return
    msgs = history.setdefault(chat_id, [])
    msgs.append({"role": "user", "content": text})
    del msgs[:-MAX_HISTORY]
    try:
        reply = with_typing(chat_id, lambda: venice_chat(model, list(msgs)))
    except requests.Timeout:
        msgs.pop()
        send(chat_id, "Venice took too long to answer. Try again, or pick a faster model.")
        return
    except Exception as e:
        msgs.pop()  # keep history consistent with what was actually answered
        send(chat_id, str(e) if isinstance(e, RuntimeError) else "Couldn't reach Venice. Try again in a moment.")
        return
    msgs.append({"role": "assistant", "content": reply})
    del msgs[:-MAX_HISTORY]
    send(chat_id, reply)


def handle(msg, state):
    if not msg:
        return
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return
    chat_id = chat["id"]
    user_id = (msg.get("from") or {}).get("id")

    if not ALLOWED:  # setup mode: reveal only the sender's own ID, never call Venice
        send(chat_id, f"Setup: your Telegram user ID is {user_id}. Add it to the "
                      "ALLOWED_TELEGRAM_USER_IDS secret, then restart the bot.")
        return
    if user_id not in ALLOWED:
        return  # stay silent for strangers

    text = msg.get("text")
    if text is None:
        send(chat_id, "I can only read text messages.")
        return
    text = text.strip()
    if text.startswith("/"):
        head, _, arg = text.partition(" ")
        cmd, arg = head.split("@")[0].lower(), arg.strip()
        if cmd in ("/start", "/help"):
            cur = current_model(state)
            send(chat_id, HELP + (f"\n\nCurrent model: {cur}" if cur else "\n\nNo model selected yet. Send /models."))
        elif cmd == "/models":
            cmd_models(chat_id, state, arg)
        elif cmd == "/model":
            cmd_model(chat_id, arg, state)
        elif cmd == "/reset":
            history.pop(chat_id, None)
            send(chat_id, "Conversation cleared.")
        else:
            send(chat_id, "Unknown command. Send /help.")
    elif text:
        chat_turn(chat_id, text, state)


def main():
    if not TG_TOKEN or not VENICE_KEY:
        print("::error::VENICE_BOT_TOKEN and VENICE_API_KEY must both be set")
        sys.exit(1)
    if not ALLOWED:
        print("::warning::ALLOWED_TELEGRAM_USER_IDS is empty - running in setup mode (no Venice calls)")

    state = load_state()
    deadline = time.monotonic() + POLL_DURATION if POLL_DURATION else None
    offset = None
    handled = 0

    while True:
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 5:
            break
        poll = LONG_POLL_SECONDS if remaining is None else int(min(LONG_POLL_SECONDS, remaining - 5))
        try:
            updates = tg("getUpdates", timeout=poll, offset=offset, allowed_updates=["message"])
        except TelegramError as e:
            print("getUpdates failed:", e.status)
            if e.status == 401:
                print("::error::Telegram rejected the bot token")
                sys.exit(1)
            time.sleep(5)  # includes 409 when another instance is still polling
            continue
        except requests.RequestException as e:
            print("getUpdates network error:", type(e).__name__)
            time.sleep(5)
            continue

        for u in updates:
            offset = u["update_id"] + 1
            try:
                handle(u.get("message"), state)
                handled += 1
            except Exception as e:
                print("handler error:", type(e).__name__)

    if offset is not None:  # confirm what we processed so the next run doesn't replay it
        try:
            tg("getUpdates", offset=offset, timeout=0)
        except Exception:
            pass
    print(f"done, handled {handled} update(s)")


if __name__ == "__main__":
    main()
