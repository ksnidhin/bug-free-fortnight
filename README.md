# Telegram Media Moderation Bot

A production-ready Telegram bot that moderates media in groups and supergroups. It tracks, blocks, and whitelists stickers, photos, videos, GIFs, documents, voice messages, video notes, and audio by their `file_unique_id`.

## Features

- **Auto-learn** — Every media `file_unique_id` the bot sees is logged in `seen.json`.
- **Blocklist enforcement** — Media matching a blocked UID is deleted instantly (or warned in demo mode).
- **Whitelist** — UIDs on the whitelist are never blocked.
- **Reply-based & direct-ID commands** — `/lock`, `/unlock`, `/whitelist`, `/unwhitelist` all accept either a reply or a UID argument.
- **Admin permissions** — Only the owner (from `.env`) and group admins can run moderation commands.
- **Structured logging** — Actions are logged to a configurable Telegram chat.
- **Demo mode** — Warn instead of deleting blocked media.
- **Termux compatible** — Pure Python, no native dependencies.

## Requirements

- Python 3.13+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- The bot must be added to your group **as an admin** with "Delete messages" permission.

## Quick Start

```bash
# 1. Clone / copy the project
cd telegram-mod-bot

# 2. Create a virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate   # Linux / macOS / Termux
# .venv\Scripts\activate    # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env with your BOT_TOKEN, OWNER_ID, and optionally LOG_CHAT_ID

# 5. Run
python bot.py
```

### Termux

```bash
pkg install python
pip install -r requirements.txt
cp .env.example .env
nano .env          # set your values
python bot.py
```

## Environment Variables

| Variable       | Required | Description                                      |
| -------------- | -------- | ------------------------------------------------ |
| `BOT_TOKEN`    | ✅        | Bot token from @BotFather                        |
| `OWNER_ID`     | ✅        | Your Telegram numeric user ID (global super-admin) |
| `LOG_CHAT_ID`  | ❌        | Chat ID for moderation log messages              |
| `DEMO_MODE`    | ❌        | Set to `true` to warn instead of deleting (default: `false`) |

## Commands

### Admin Commands (owner + group admins)

| Command         | Description                                      |
| --------------- | ------------------------------------------------ |
| `/lock`         | Reply to media **or** `/lock <uid>` to block it  |
| `/unlock`       | Reply to media **or** `/unlock <uid>` to unblock |
| `/listlocks`    | Show all blocked unique IDs                      |
| `/whitelist`    | Reply to media **or** `/whitelist <uid>`          |
| `/unwhitelist`  | Reply to media **or** `/unwhitelist <uid>`        |
| `/warn`         | Reply to a message to warn the user              |
| `/seen`         | Show recently observed media IDs                 |
| `/reload`       | Reload all JSON data from disk                   |

### General Commands

| Command | Description                                  |
| ------- | -------------------------------------------- |
| `/id`   | Reply to media to see its `file_unique_id`   |
| `/help` | Show usage                                   |

## Data Files

All data is stored in the `data/` directory as JSON. Files are auto-created on first run.

| File              | Purpose                                           |
| ----------------- | ------------------------------------------------- |
| `blocked.json`    | List of blocked `file_unique_id` values           |
| `seen.json`       | All observed `file_unique_id` values (capped at 500) |
| `whitelist.json`  | UIDs that must never be blocked                   |

## How It Works

1. **Every media message** the bot can see has its `file_unique_id` recorded in `seen.json`.
2. If that UID is in `blocked.json` **and not** in `whitelist.json`, the message is deleted.
3. In demo mode (`DEMO_MODE=true`), the bot replies with a warning instead of deleting.
4. Admins use `/lock` to add a UID to the blocklist and `/whitelist` to protect one.

## Permissions Model

- The **owner** (set via `OWNER_ID`) always has full access to all commands.
- In groups/supergroups, Telegram **admins** (Administrator or Owner status) can also use admin commands.
- In private chats, only the owner can run admin commands.

## License

MIT
