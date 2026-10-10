# Venice Telegram bot

`venice_bot.py` lets you text a [Venice AI](https://venice.ai) chat model from a Telegram bot.
It runs on GitHub Actions (`.github/workflows/venice-bot.yml`): each run long-polls Telegram for
~5h40m, then queues its own successor, so the bot is effectively always on. Messages that arrive in
the few seconds between runs are delivered when the next run starts.

## Setup

1. **Create the bot.** In Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot`,
   and copy the token. Use a *new* bot, not the one the watcher uses.
2. **Add repo secrets** (Settings → Secrets and variables → Actions → Secrets):
   - `VENICE_BOT_TOKEN` - the token from BotFather
   - `VENICE_API_KEY` - your Venice API key
   - `ALLOWED_TELEGRAM_USER_IDS` - your numeric Telegram user ID (see step 4)
3. **Start it.** Actions → *Venice Telegram bot* → *Run workflow*.
4. **Find your user ID.** Until `ALLOWED_TELEGRAM_USER_IDS` is set the bot is in setup mode: it
   replies to any message with the sender's user ID and never calls Venice. Message your bot, put that
   ID into the secret, then run the workflow again (the running instance only reads secrets at start;
   cancel it first so the new one starts straight away).
5. **Pick a model.** Send `/models`, then `/model <number>` (see *Models* below). Optionally set a
   repo *variable* `VENICE_DEFAULT_MODEL` to one of those model ids to use it until you choose.

## Models

The bot only offers these five models (the `MODELS` list at the top of `venice_bot.py`; edit it to
change them). `/models` shows exactly this list and nothing else is accepted.

| # | Model id | Name |
| --- | --- | --- |
| 1 | `venice-uncensored-1-2` | Venice Uncensored 1.2 |
| 2 | `venice-uncensored-role-play` | Venice Role Play Uncensored |
| 3 | `olafangensan-glm-4.7-flash-heretic` | GLM 4.7 Flash Heretic |
| 4 | `e2ee-gemma-4-26b-a4b-uncensored-p` | Gemma 4 26B A4B Uncensored (E2EE) |
| 5 | `qwen-3-8-27b` | Qwen 3.8 27B |

## Commands

| Command | What it does |
| --- | --- |
| *(any text)* | Chat with the current model |
| `/models` | List the five models |
| `/model` | Show the current model |
| `/model <number\|id>` | Switch model (clears the conversation) |
| `/reset` | Clear the conversation |

## Privacy and cost

- This repo is public, so Actions logs are public. The bot never logs message text or replies.
- Only the user IDs in `ALLOWED_TELEGRAM_USER_IDS` are served; everyone else is ignored silently.
- Conversation history is held in memory only (last `MAX_HISTORY_MESSAGES`, default 20) and resets
  when a run ends. Only the chosen model id is cached between runs.
- Every message is a billed Venice API call. A longer history means more input tokens per message, so
  use `/reset` when you change topic.

## Running it somewhere else

It is plain Python with no GitHub dependency:

```sh
pip install requests
VENICE_BOT_TOKEN=... VENICE_API_KEY=... ALLOWED_TELEGRAM_USER_IDS=... python venice_bot.py
```

With `POLL_DURATION_SECONDS` unset it runs until stopped. Never run two copies on one bot token.
