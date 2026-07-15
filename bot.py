"""
Telegram Media Moderation Bot
==============================
Production-ready group moderation bot built with python-telegram-bot v21+.

Detects, logs, and optionally blocks media by Telegram file_unique_id.
Supports stickers, photos, videos, documents, GIFs, voice, video notes,
and audio. Uses JSON files for persistent storage.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telegram import Message, Update
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
OWNER_ID: int = int(os.getenv("OWNER_ID", "0"))
LOG_CHAT_ID: int | None = (
    int(os.getenv("LOG_CHAT_ID")) if os.getenv("LOG_CHAT_ID") else None
)

if not BOT_TOKEN:
    sys.exit("ERROR: BOT_TOKEN is not set. Check your .env file.")
if not OWNER_ID:
    sys.exit("ERROR: OWNER_ID is not set. Check your .env file.")

DATA_DIR = Path("data")
BLOCKED_FILE = DATA_DIR / "blocked.json"
SEEN_FILE = DATA_DIR / "seen.json"
WHITELIST_FILE = DATA_DIR / "whitelist.json"

DEMO_MODE: bool = os.getenv("DEMO_MODE", "false").lower() in ("1", "true", "yes")

MAX_SEEN: int = 500  # cap seen.json entries to avoid unbounded growth
MAX_LIST_DISPLAY: int = 40  # max items shown in /listlocks & /seen

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("modbot")

# ---------------------------------------------------------------------------
# JSON Storage Helpers
# ---------------------------------------------------------------------------


def _ensure_data_dir() -> None:
    """Create the data directory if it does not exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path) -> list[str]:
    """Load a JSON list from *path*, returning [] on missing / corrupt file."""
    _ensure_data_dir()
    if not path.exists():
        _save_json(path, [])
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return data
        logger.warning("Unexpected type in %s – resetting to []", path)
        return []
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read %s (%s) – resetting to []", path, exc)
        return []


def _save_json(path: Path, data: list[str]) -> None:
    """Atomically write *data* as JSON to *path*."""
    _ensure_data_dir()
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    tmp.replace(path)


# ---------------------------------------------------------------------------
# In-memory data stores (loaded once, synced to disk on mutation)
# ---------------------------------------------------------------------------

blocked: list[str] = []
seen: list[str] = []
whitelist: list[str] = []


def load_all() -> None:
    """(Re)load every JSON store from disk into memory."""
    global blocked, seen, whitelist  # noqa: PLW0603
    blocked = _load_json(BLOCKED_FILE)
    seen = _load_json(SEEN_FILE)
    whitelist = _load_json(WHITELIST_FILE)
    logger.info(
        "Loaded %d blocked, %d seen, %d whitelisted IDs",
        len(blocked),
        len(seen),
        len(whitelist),
    )


# ---------------------------------------------------------------------------
# Media extraction helpers
# ---------------------------------------------------------------------------

# Media type name → attribute on telegram.Message
_MEDIA_ATTRS: list[tuple[str, str]] = [
    ("sticker", "sticker"),
    ("photo", "photo"),
    ("video", "video"),
    ("animation", "animation"),
    ("document", "document"),
    ("voice", "voice"),
    ("video_note", "video_note"),
    ("audio", "audio"),
]


def _extract_media(msg: Message) -> tuple[str, str, str] | None:
    """Return (media_type, file_unique_id, file_id) or None."""
    for media_type, attr in _MEDIA_ATTRS:
        obj = getattr(msg, attr, None)
        if obj is None:
            continue
        # msg.photo is a list; pick the largest resolution
        if media_type == "photo":
            if not obj:
                continue
            best = obj[-1]
            return media_type, best.file_unique_id, best.file_id
        return media_type, obj.file_unique_id, obj.file_id
    return None


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------


async def _is_admin(update: Update, user_id: int) -> bool:
    """Return True if *user_id* is the owner or a group admin."""
    if user_id == OWNER_ID:
        return True

    chat = update.effective_chat
    if chat is None:
        return False

    # Private chats – only the owner can run admin commands
    if chat.type == ChatType.PRIVATE:
        return user_id == OWNER_ID

    try:
        member = await chat.get_member(user_id)
        return member.status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
    except TelegramError:
        return False


# ---------------------------------------------------------------------------
# Logging helper → sends to LOG_CHAT_ID
# ---------------------------------------------------------------------------


async def _log_event(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    action: str,
    user_id: int | None = None,
    username: str | None = None,
    chat_title: str | None = None,
    media_type: str | None = None,
    file_unique_id: str | None = None,
    extra: str | None = None,
) -> None:
    """Send a structured log message to LOG_CHAT_ID (if configured)."""
    if LOG_CHAT_ID is None:
        return

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    parts: list[str] = [f"🔔 <b>{action}</b>", f"🕐 {now}"]

    if user_id is not None:
        parts.append(f"👤 User: <code>{user_id}</code> (@{username or '?'})")
    if chat_title:
        parts.append(f"💬 Chat: {chat_title}")
    if media_type:
        parts.append(f"📎 Type: {media_type}")
    if file_unique_id:
        parts.append(f"🆔 UID: <code>{file_unique_id}</code>")
    if extra:
        parts.append(extra)

    text = "\n".join(parts)
    try:
        await context.bot.send_message(
            chat_id=LOG_CHAT_ID,
            text=text,
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as exc:
        logger.warning("Failed to send log message: %s", exc)


# ---------------------------------------------------------------------------
# Auto-learn: record every new media unique ID
# ---------------------------------------------------------------------------


def _record_seen(uid: str) -> None:
    """Add *uid* to the seen list (capped at MAX_SEEN)."""
    if uid in seen:
        return
    seen.append(uid)
    # Evict oldest entries when the list grows too large
    while len(seen) > MAX_SEEN:
        seen.pop(0)
    _save_json(SEEN_FILE, seen)


# ---------------------------------------------------------------------------
# Message handler: auto-learn + enforce blocklist
# ---------------------------------------------------------------------------


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process every incoming message for media tracking and enforcement."""
    msg = update.effective_message
    if msg is None:
        return

    media = _extract_media(msg)
    if media is None:
        return

    media_type, uid, fid = media
    _record_seen(uid)

    # Skip whitelisted IDs
    if uid in whitelist:
        return

    # Enforce blocklist
    if uid not in blocked:
        return

    user = msg.from_user
    user_id = user.id if user else 0
    username = user.username if user else None
    chat_title = msg.chat.title if msg.chat else None

    if DEMO_MODE:
        # Warn only
        try:
            await msg.reply_text(
                "⚠️ <b>Warning:</b> This media is on the blocklist (demo mode — not deleted).",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
        await _log_event(
            context,
            action="WARN (demo)",
            user_id=user_id,
            username=username,
            chat_title=chat_title,
            media_type=media_type,
            file_unique_id=uid,
        )
        return

    # Delete the message
    try:
        await msg.delete()
        logger.info("Deleted blocked %s (uid=%s) from user %s", media_type, uid, user_id)
    except (BadRequest, Forbidden) as exc:
        logger.warning("Cannot delete message: %s", exc)

    await _log_event(
        context,
        action="BLOCKED & DELETED",
        user_id=user_id,
        username=username,
        chat_title=chat_title,
        media_type=media_type,
        file_unique_id=uid,
    )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


async def cmd_lock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/lock — block a media item by replying to it or passing a UID."""
    msg = update.effective_message
    if msg is None:
        return
    user = msg.from_user
    if user is None or not await _is_admin(update, user.id):
        await msg.reply_text("🚫 You don't have permission to use this command.")
        return

    uid: str | None = None
    media_type: str | None = None

    # Check for a direct UID argument
    if context.args:
        uid = context.args[0]
        media_type = "manual"
    elif msg.reply_to_message:
        media = _extract_media(msg.reply_to_message)
        if media:
            media_type, uid, _ = media

    if uid is None:
        await msg.reply_text("Reply to a media message or pass a unique ID: <code>/lock &lt;uid&gt;</code>",
                             parse_mode=ParseMode.HTML)
        return

    if uid in whitelist:
        await msg.reply_text("⚠️ That ID is whitelisted. Remove it first with /unwhitelist.")
        return

    if uid in blocked:
        await msg.reply_text("ℹ️ Already blocked.")
        return

    blocked.append(uid)
    _save_json(BLOCKED_FILE, blocked)

    await msg.reply_text(f"🔒 Blocked <code>{uid}</code>", parse_mode=ParseMode.HTML)
    await _log_event(
        context,
        action="LOCK",
        user_id=user.id,
        username=user.username,
        media_type=media_type,
        file_unique_id=uid,
        chat_title=msg.chat.title if msg.chat else None,
    )


async def cmd_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/unlock — remove a UID from the blocklist."""
    msg = update.effective_message
    if msg is None:
        return
    user = msg.from_user
    if user is None or not await _is_admin(update, user.id):
        await msg.reply_text("🚫 You don't have permission to use this command.")
        return

    uid: str | None = None

    if context.args:
        uid = context.args[0]
    elif msg.reply_to_message:
        media = _extract_media(msg.reply_to_message)
        if media:
            _, uid, _ = media

    if uid is None:
        await msg.reply_text("Reply to a media message or pass a unique ID: <code>/unlock &lt;uid&gt;</code>",
                             parse_mode=ParseMode.HTML)
        return

    if uid not in blocked:
        await msg.reply_text("ℹ️ That ID is not currently blocked.")
        return

    blocked.remove(uid)
    _save_json(BLOCKED_FILE, blocked)

    await msg.reply_text(f"🔓 Unblocked <code>{uid}</code>", parse_mode=ParseMode.HTML)
    await _log_event(
        context,
        action="UNLOCK",
        user_id=user.id,
        username=user.username,
        file_unique_id=uid,
        chat_title=msg.chat.title if msg.chat else None,
    )


async def cmd_listlocks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/listlocks — list all blocked unique IDs."""
    msg = update.effective_message
    if msg is None:
        return
    user = msg.from_user
    if user is None or not await _is_admin(update, user.id):
        await msg.reply_text("🚫 You don't have permission to use this command.")
        return

    if not blocked:
        await msg.reply_text("📭 Blocklist is empty.")
        return

    display = blocked[:MAX_LIST_DISPLAY]
    lines = [f"<code>{uid}</code>" for uid in display]
    header = f"🔒 <b>Blocked IDs</b> ({len(blocked)} total):\n"
    footer = f"\n… and {len(blocked) - MAX_LIST_DISPLAY} more" if len(blocked) > MAX_LIST_DISPLAY else ""
    await msg.reply_text(header + "\n".join(lines) + footer, parse_mode=ParseMode.HTML)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/id — print file_unique_id and file_id of a replied media message."""
    msg = update.effective_message
    if msg is None:
        return

    target = msg.reply_to_message
    if target is None:
        await msg.reply_text("Reply to a media message to get its IDs.")
        return

    media = _extract_media(target)
    if media is None:
        await msg.reply_text("No supported media found in that message.")
        return

    media_type, uid, fid = media
    text = (
        f"📎 <b>Type:</b> {media_type}\n"
        f"🆔 <b>file_unique_id:</b> <code>{uid}</code>\n"
        f"📄 <b>file_id:</b> <code>{fid}</code>"
    )
    await msg.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_seen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/seen — show recently observed media IDs."""
    msg = update.effective_message
    if msg is None:
        return
    user = msg.from_user
    if user is None or not await _is_admin(update, user.id):
        await msg.reply_text("🚫 You don't have permission to use this command.")
        return

    if not seen:
        await msg.reply_text("📭 No media observed yet.")
        return

    # Show the most recent entries first
    recent = list(reversed(seen[-MAX_LIST_DISPLAY:]))
    lines = [f"<code>{uid}</code>" for uid in recent]
    header = f"👁 <b>Recently seen IDs</b> ({len(seen)} total):\n"
    await msg.reply_text(header + "\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/whitelist — add a media UID to the whitelist."""
    msg = update.effective_message
    if msg is None:
        return
    user = msg.from_user
    if user is None or not await _is_admin(update, user.id):
        await msg.reply_text("🚫 You don't have permission to use this command.")
        return

    uid: str | None = None

    if context.args:
        uid = context.args[0]
    elif msg.reply_to_message:
        media = _extract_media(msg.reply_to_message)
        if media:
            _, uid, _ = media

    if uid is None:
        await msg.reply_text("Reply to a media message or pass a unique ID: <code>/whitelist &lt;uid&gt;</code>",
                             parse_mode=ParseMode.HTML)
        return

    if uid in whitelist:
        await msg.reply_text("ℹ️ Already whitelisted.")
        return

    whitelist.append(uid)
    _save_json(WHITELIST_FILE, whitelist)

    # Auto-unblock if currently blocked
    if uid in blocked:
        blocked.remove(uid)
        _save_json(BLOCKED_FILE, blocked)
        await msg.reply_text(f"✅ Whitelisted <code>{uid}</code> (also removed from blocklist)",
                             parse_mode=ParseMode.HTML)
    else:
        await msg.reply_text(f"✅ Whitelisted <code>{uid}</code>", parse_mode=ParseMode.HTML)

    await _log_event(
        context,
        action="WHITELIST",
        user_id=user.id,
        username=user.username,
        file_unique_id=uid,
        chat_title=msg.chat.title if msg.chat else None,
    )


async def cmd_unwhitelist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/unwhitelist — remove a UID from the whitelist."""
    msg = update.effective_message
    if msg is None:
        return
    user = msg.from_user
    if user is None or not await _is_admin(update, user.id):
        await msg.reply_text("🚫 You don't have permission to use this command.")
        return

    uid: str | None = None

    if context.args:
        uid = context.args[0]
    elif msg.reply_to_message:
        media = _extract_media(msg.reply_to_message)
        if media:
            _, uid, _ = media

    if uid is None:
        await msg.reply_text("Reply to a media message or pass a unique ID: <code>/unwhitelist &lt;uid&gt;</code>",
                             parse_mode=ParseMode.HTML)
        return

    if uid not in whitelist:
        await msg.reply_text("ℹ️ That ID is not whitelisted.")
        return

    whitelist.remove(uid)
    _save_json(WHITELIST_FILE, whitelist)

    await msg.reply_text(f"🗑 Removed <code>{uid}</code> from whitelist", parse_mode=ParseMode.HTML)
    await _log_event(
        context,
        action="UNWHITELIST",
        user_id=user.id,
        username=user.username,
        file_unique_id=uid,
        chat_title=msg.chat.title if msg.chat else None,
    )


async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/reload — reload all JSON data files from disk."""
    msg = update.effective_message
    if msg is None:
        return
    user = msg.from_user
    if user is None or not await _is_admin(update, user.id):
        await msg.reply_text("🚫 You don't have permission to use this command.")
        return

    load_all()
    await msg.reply_text(
        f"🔄 Reloaded: {len(blocked)} blocked, {len(seen)} seen, {len(whitelist)} whitelisted."
    )
    await _log_event(
        context,
        action="RELOAD",
        user_id=user.id,
        username=user.username,
        chat_title=msg.chat.title if msg.chat else None,
    )


async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/warn — send a visible warning about blocked content (reply to a message)."""
    msg = update.effective_message
    if msg is None:
        return
    user = msg.from_user
    if user is None or not await _is_admin(update, user.id):
        await msg.reply_text("🚫 You don't have permission to use this command.")
        return

    target = msg.reply_to_message
    if target is None:
        await msg.reply_text("Reply to a message to warn about it.")
        return

    target_user = target.from_user
    mention = f"@{target_user.username}" if target_user and target_user.username else "User"

    media = _extract_media(target)
    uid_info = f" (UID: <code>{media[1]}</code>)" if media else ""

    await target.reply_text(
        f"⚠️ <b>Warning:</b> {mention}, this content violates group rules{uid_info}.",
        parse_mode=ParseMode.HTML,
    )

    await _log_event(
        context,
        action="WARN",
        user_id=user.id,
        username=user.username,
        chat_title=msg.chat.title if msg.chat else None,
        file_unique_id=media[1] if media else None,
        media_type=media[0] if media else None,
        extra=f"Target user: {target_user.id if target_user else '?'}",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — show all available commands."""
    msg = update.effective_message
    if msg is None:
        return

    text = (
        "🤖 <b>Media Moderation Bot</b>\n\n"
        "<b>Admin commands</b> (owner + group admins):\n"
        "  /lock — reply to media or pass UID to block it\n"
        "  /unlock — remove a UID from blocklist\n"
        "  /listlocks — show all blocked IDs\n"
        "  /whitelist — add a UID to whitelist\n"
        "  /unwhitelist — remove a UID from whitelist\n"
        "  /warn — reply to a message to warn the user\n"
        "  /seen — show recently observed media IDs\n"
        "  /reload — reload JSON data from disk\n\n"
        "<b>General commands:</b>\n"
        "  /id — reply to media to see its unique IDs\n"
        "  /help — show this message\n\n"
        f"Demo mode: <b>{'ON' if DEMO_MODE else 'OFF'}</b>"
    )
    await msg.reply_text(text, parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler – log exceptions without crashing."""
    logger.error("Unhandled exception:", exc_info=context.error)

    if LOG_CHAT_ID and context.error:
        try:
            await context.bot.send_message(
                chat_id=LOG_CHAT_ID,
                text=f"❌ <b>Bot error:</b>\n<pre>{context.error}</pre>",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass


# ---------------------------------------------------------------------------
# Application entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Build and run the bot application."""
    load_all()

    app = Application.builder().token(BOT_TOKEN).build()

    # Command handlers (order doesn't matter for commands)
    app.add_handler(CommandHandler("lock", cmd_lock))
    app.add_handler(CommandHandler("unlock", cmd_unlock))
    app.add_handler(CommandHandler("listlocks", cmd_listlocks))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("seen", cmd_seen))
    app.add_handler(CommandHandler("whitelist", cmd_whitelist))
    app.add_handler(CommandHandler("unwhitelist", cmd_unwhitelist))
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(CommandHandler("warn", cmd_warn))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))

    # Media message handler — runs on every non-command message that contains media
    media_filter = (
        filters.PHOTO
        | filters.Sticker.ALL
        | filters.VIDEO
        | filters.ANIMATION
        | filters.Document.ALL
        | filters.VOICE
        | filters.VIDEO_NOTE
        | filters.AUDIO
    )
    app.add_handler(MessageHandler(media_filter & ~filters.COMMAND, on_message))

    # Global error handler
    app.add_error_handler(error_handler)

    logger.info("Bot starting… (demo_mode=%s, owner=%s)", DEMO_MODE, OWNER_ID)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
