"""
Telegram Media Moderation Bot
==============================
Production-ready group moderation bot built with python-telegram-bot v21+.

Detects, logs, and optionally blocks media by Telegram file_unique_id.
Supports stickers, photos, videos, documents, GIFs, voice, video notes,
and audio. Uses JSON files for persistent storage.

Features:
    - Blocklist / whitelist / seen tracking of media by file_unique_id
    - Two lock tiers:
        * /lock  — normal moderator lock, unlockable by any moderator/admin
        * /olock — owner-only lock, unlockable ONLY by the owner
    - Owner-authorized moderators (non-Telegram-admins granted mod rights)
    - /warn always mutes the target user for 60 seconds
    - Locked-media mutes are TEMPORARY ONLY (15 seconds) and are controlled
      by a per-chat toggle (/enp to enable, /dsp to disable). Default is
      disabled for every chat — locked media is deleted + logged only,
      with no mute, until an admin opts in with /enp.
    - Per-user link-deletion blacklist (/bl, /en): any message containing a
      link sent by a blacklisted user is auto-deleted, even if that user is
      a Telegram admin or the group owner. Messages without links from a
      blacklisted user are left untouched.
    - Structured logging to an optional log chat

NOTE: For any mute/restrict functionality to work, the bot must be a group
admin with the "Restrict members" permission enabled. All mutes issued by
this bot are time-limited (via `until_date`) — the bot never issues a
permanent/indefinite restriction.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telegram import ChatPermissions, Message, Update
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
LOCK_META_FILE = DATA_DIR / "lock_meta.json"
AUTHORIZED_MODS_FILE = DATA_DIR / "authorized_mods.json"
CHAT_MUTE_SETTINGS_FILE = DATA_DIR / "chat_mute_settings.json"
LINK_BLACKLIST_FILE = DATA_DIR / "link_blacklist.json"

DEMO_MODE: bool = os.getenv("DEMO_MODE", "false").lower() in ("1", "true", "yes")

MAX_SEEN: int = 500  # cap seen.json entries to avoid unbounded growth
MAX_LIST_DISPLAY: int = 40  # max items shown in /listlocks & /seen

WARN_MUTE_SECONDS: int = 60
LOCKED_MEDIA_MUTE_SECONDS: int = 15

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("modbot")

# ---------------------------------------------------------------------------
# Link detection
# ---------------------------------------------------------------------------

# Matches http(s):// URLs, t.me/ links, www. links, and bare domains with a
# common TLD (e.g. "example.com/page", "bit.ly/xyz"). Case-insensitive.
LINK_REGEX = re.compile(
    r"(?:https?://|t\.me/|www\.)\S+"
    r"|\b[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\."
    r"(?:com|net|org|io|me|co|info|biz|xyz|dev|app|gg|link|club|ru|uk|de|cn|"
    r"us|in|to|ly|gl|tv|shop|online|site|store)\b(?:/\S*)?",
    re.IGNORECASE,
)

_LINK_ENTITY_TYPES = {"url", "text_link"}


def _message_contains_link(msg: Message) -> bool:
    """Return True if *msg* (its text or caption) contains a link, either
    via Telegram-parsed entities or a regex fallback for plain-text URLs.
    """
    entities = list(msg.entities or []) + list(msg.caption_entities or [])
    for ent in entities:
        if ent.type in _LINK_ENTITY_TYPES:
            return True

    text = msg.text or msg.caption or ""
    if not text:
        return False
    return bool(LINK_REGEX.search(text))


# ---------------------------------------------------------------------------
# JSON Storage Helpers
# ---------------------------------------------------------------------------


def _ensure_data_dir() -> None:
    """Create the data directory if it does not exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_json_list(path: Path) -> list[str]:
    """Load a JSON list from *path*, returning [] on missing / corrupt file."""
    _ensure_data_dir()
    if not path.exists():
        _save_json_list(path, [])
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


def _save_json_list(path: Path, data: list[str]) -> None:
    """Atomically write a JSON list *data* to *path*."""
    _ensure_data_dir()
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    tmp.replace(path)


def _load_json_dict(path: Path) -> dict[str, Any]:
    """Load a JSON dict from *path*, returning {} on missing / corrupt file."""
    _ensure_data_dir()
    if not path.exists():
        _save_json_dict(path, {})
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
        logger.warning("Unexpected type in %s – resetting to {}", path)
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read %s (%s) – resetting to {}", path, exc)
        return {}


def _save_json_dict(path: Path, data: dict[str, Any]) -> None:
    """Atomically write a JSON dict *data* to *path*."""
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
lock_meta: dict[str, Any] = {}
authorized_mods: list[int] = []
chat_mute_settings: dict[str, bool] = {}
link_blacklist: dict[str, list[str]] = {}


def load_all() -> None:
    """(Re)load every JSON store from disk into memory."""
    global blocked, seen, whitelist, lock_meta, authorized_mods, chat_mute_settings, link_blacklist  # noqa: PLW0603
    blocked = _load_json_list(BLOCKED_FILE)
    seen = _load_json_list(SEEN_FILE)
    whitelist = _load_json_list(WHITELIST_FILE)
    lock_meta = _load_json_dict(LOCK_META_FILE)
    chat_mute_settings = _load_json_dict(CHAT_MUTE_SETTINGS_FILE)
    link_blacklist = _load_json_dict(LINK_BLACKLIST_FILE)

    raw_mods = _load_json_list(AUTHORIZED_MODS_FILE)
    authorized_mods = []
    for entry in raw_mods:
        try:
            authorized_mods.append(int(entry))
        except (TypeError, ValueError):
            logger.warning("Skipping invalid authorized mod entry: %r", entry)

    logger.info(
        "Loaded %d blocked, %d seen, %d whitelisted, %d lock-meta, "
        "%d authorized mods, %d chat mute settings, %d chats w/ link-blacklist",
        len(blocked),
        len(seen),
        len(whitelist),
        len(lock_meta),
        len(authorized_mods),
        len(chat_mute_settings),
        len(link_blacklist),
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


def _is_owner(user_id: int) -> bool:
    """Return True if *user_id* is the bot owner."""
    return user_id == OWNER_ID


def _is_authorized_mod(user_id: int) -> bool:
    """Return True if *user_id* was granted moderator rights by the owner."""
    return user_id in authorized_mods


async def _is_admin(update: Update, user_id: int) -> bool:
    """Return True if *user_id* is the owner, an owner-authorized moderator,
    or a Telegram group admin. This is the general "can use mod commands"
    check.
    """
    if _is_owner(user_id):
        return True

    if _is_authorized_mod(user_id):
        return True

    chat = update.effective_chat
    if chat is None:
        return False

    # Private chats – only the owner (and authorized mods) can run admin
    # commands; group-admin status doesn't apply outside a group.
    if chat.type == ChatType.PRIVATE:
        return False

    try:
        member = await chat.get_member(user_id)
        return member.status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
    except TelegramError:
        return False


def _is_owner_locked(uid: str) -> bool:
    """Return True if *uid* was locked via /olock (owner-owned lock)."""
    meta = lock_meta.get(uid)
    if not meta:
        return False
    return bool(meta.get("owner_lock", False))


# ---------------------------------------------------------------------------
# Per-chat mute-on-locked-media toggle
# ---------------------------------------------------------------------------


def _is_chat_mute_enabled(chat_id: int) -> bool:
    """Return True if this chat has opted in to muting senders of locked
    media. Default is disabled for every chat.
    """
    return bool(chat_mute_settings.get(str(chat_id), False))


def _set_chat_mute_enabled(chat_id: int, enabled: bool) -> None:
    """Persist the mute-on-locked-media setting for *chat_id*."""
    chat_mute_settings[str(chat_id)] = enabled
    _save_json_dict(CHAT_MUTE_SETTINGS_FILE, chat_mute_settings)


# ---------------------------------------------------------------------------
# Per-chat, per-user link-deletion blacklist
# ---------------------------------------------------------------------------


def _is_link_blacklisted(chat_id: int, user_id: int) -> bool:
    """Return True if *user_id* is on the link-deletion blacklist for
    *chat_id*.
    """
    return str(user_id) in link_blacklist.get(str(chat_id), [])


def _add_link_blacklist(chat_id: int, user_id: int) -> None:
    """Add *user_id* to the link-deletion blacklist for *chat_id* and
    persist it.
    """
    key = str(chat_id)
    members = link_blacklist.setdefault(key, [])
    if str(user_id) not in members:
        members.append(str(user_id))
    _save_json_dict(LINK_BLACKLIST_FILE, link_blacklist)


def _remove_link_blacklist(chat_id: int, user_id: int) -> bool:
    """Remove *user_id* from the link-deletion blacklist for *chat_id*.
    Returns True if the user was present and removed.
    """
    key = str(chat_id)
    members = link_blacklist.get(key, [])
    if str(user_id) not in members:
        return False
    members.remove(str(user_id))
    if not members:
        link_blacklist.pop(key, None)
    _save_json_dict(LINK_BLACKLIST_FILE, link_blacklist)
    return True


async def _enforce_link_blacklist(msg: Message, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """If *msg* was sent by a link-blacklisted user in this chat AND
    contains a link, delete it — regardless of the sender's admin/owner
    status — and log the action. Returns True if the message was deleted.
    """
    chat = msg.chat
    user = msg.from_user
    if chat is None or user is None:
        return False
    if chat.type == ChatType.PRIVATE:
        return False
    if not _is_link_blacklisted(chat.id, user.id):
        return False
    if not _message_contains_link(msg):
        return False

    try:
        await msg.delete()
        logger.info(
            "Deleted link message from blacklisted user %s in chat %s",
            user.id,
            chat.id,
        )
    except (BadRequest, Forbidden) as exc:
        logger.warning("Cannot delete blacklisted-user link message: %s", exc)

    await _log_event(
        context,
        action="LINK_BLACKLIST_DELETE",
        user_id=user.id,
        username=user.username,
        chat_title=chat.title,
        extra="Deleted message containing a link from a link-blacklisted user",
    )
    return True


# ---------------------------------------------------------------------------
# Mute helper (always temporary — never permanent)
# ---------------------------------------------------------------------------


async def _mute_user_temporarily(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    duration_seconds: int,
) -> bool:
    """Restrict *user_id* from sending messages in *chat_id* for exactly
    *duration_seconds*, using Telegram's `until_date` restriction field.
    This always produces a time-limited mute — the bot never calls this
    (or any restriction) without an expiry, so a permanent mute is never
    issued. Returns True on success, False otherwise.

    Requires the bot to be a group admin with "Restrict members" permission.
    """
    if duration_seconds <= 0:
        return False

    until_date = datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)
    permissions = ChatPermissions(
        can_send_messages=False,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
    )
    try:
        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=permissions,
            until_date=until_date,
        )
        return True
    except (BadRequest, Forbidden) as exc:
        logger.warning(
            "Could not mute user %s in chat %s: %s (bot needs 'restrict members' admin rights)",
            user_id,
            chat_id,
            exc,
        )
        return False
    except TelegramError as exc:
        logger.warning("Unexpected error muting user %s: %s", user_id, exc)
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
    _save_json_list(SEEN_FILE, seen)


# ---------------------------------------------------------------------------
# Message handlers: auto-learn + enforce blocklist / link blacklist
# ---------------------------------------------------------------------------


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process every incoming media message for link-blacklist enforcement,
    media tracking, and blocklist enforcement.
    """
    msg = update.effective_message
    if msg is None:
        return

    # Link-deletion blacklist takes priority and applies regardless of
    # admin/owner status. If the message (or its caption) contains a link
    # and the sender is blacklisted for links in this chat, it is deleted
    # here and no further media processing is needed.
    if await _enforce_link_blacklist(msg, context):
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
    chat_id = msg.chat.id if msg.chat else None

    if DEMO_MODE:
        # Warn only — no delete, no mute
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

    # Mute the sender ONLY if this chat has opted in via /enp. The mute is
    # always temporary (LOCKED_MEDIA_MUTE_SECONDS), never permanent.
    mute_note = "Muting disabled for this chat"
    if chat_id is not None and _is_chat_mute_enabled(chat_id) and user_id:
        muted = await _mute_user_temporarily(context, chat_id, user_id, LOCKED_MEDIA_MUTE_SECONDS)
        mute_note = (
            f"🔇 Muted for {LOCKED_MEDIA_MUTE_SECONDS}s"
            if muted
            else "⚠️ Mute failed (check bot admin rights)"
        )

    await _log_event(
        context,
        action="BLOCKED & DELETED",
        user_id=user_id,
        username=username,
        chat_title=chat_title,
        media_type=media_type,
        file_unique_id=uid,
        extra=mute_note,
    )


async def on_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process plain-text (non-media, non-command) messages for
    link-blacklist enforcement.
    """
    msg = update.effective_message
    if msg is None:
        return
    await _enforce_link_blacklist(msg, context)


# ---------------------------------------------------------------------------
# Shared lock implementation (used by /lock and /olock)
# ---------------------------------------------------------------------------


async def _do_lock(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    owner_lock: bool,
) -> None:
    """Shared logic for /lock (owner_lock=False) and /olock (owner_lock=True)."""
    msg = update.effective_message
    if msg is None:
        return
    user = msg.from_user

    if owner_lock:
        if user is None or not _is_owner(user.id):
            await msg.reply_text("🚫 Only the owner can use /olock.")
            return
    else:
        if user is None or not await _is_admin(update, user.id):
            await msg.reply_text("🚫 You don't have permission to use this command.")
            return

    uid: str | None = None
    media_type: str | None = None

    if context.args:
        uid = context.args[0]
        media_type = "manual"
    elif msg.reply_to_message:
        media = _extract_media(msg.reply_to_message)
        if media:
            media_type, uid, _ = media

    cmd_name = "/olock" if owner_lock else "/lock"
    if uid is None:
        await msg.reply_text(
            f"Reply to a media message or pass a unique ID: <code>{cmd_name} &lt;uid&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if uid in whitelist:
        await msg.reply_text("⚠️ That ID is whitelisted. Remove it first with /unwhitelist.")
        return

    if uid in blocked:
        if _is_owner_locked(uid) and owner_lock:
            await msg.reply_text("ℹ️ Already owner-locked.")
        else:
            await msg.reply_text("ℹ️ Already blocked.")
        return

    blocked.append(uid)
    _save_json_list(BLOCKED_FILE, blocked)

    lock_meta[uid] = {
        "locked_by": user.id,
        "username": user.username,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "owner_lock": owner_lock,
    }
    _save_json_dict(LOCK_META_FILE, lock_meta)

    tag = " 🔐 (owner-locked — only the owner can unlock)" if owner_lock else ""
    await msg.reply_text(f"🔒 Blocked <code>{uid}</code>{tag}", parse_mode=ParseMode.HTML)
    await _log_event(
        context,
        action="OLOCK" if owner_lock else "LOCK",
        user_id=user.id,
        username=user.username,
        media_type=media_type,
        file_unique_id=uid,
        chat_title=msg.chat.title if msg.chat else None,
    )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


async def cmd_lock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/lock — normal moderator lock. Unlockable by any moderator/admin
    unless the UID is separately owner-locked via /olock.
    """
    await _do_lock(update, context, owner_lock=False)


async def cmd_olock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/olock — owner-only lock. Only the owner can unlock a UID locked
    this way; other moderators cannot unlock or whitelist it.
    """
    await _do_lock(update, context, owner_lock=True)


async def cmd_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/unlock — remove a UID from the blocklist.

    UIDs locked via /olock can only be unlocked by the owner.
    """
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
        await msg.reply_text(
            "Reply to a media message or pass a unique ID: <code>/unlock &lt;uid&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if uid not in blocked:
        await msg.reply_text("ℹ️ That ID is not currently blocked.")
        return

    if _is_owner_locked(uid) and not _is_owner(user.id):
        await msg.reply_text(
            "🔐 This media was locked with /olock by the owner and cannot be "
            "unlocked by other moderators."
        )
        return

    blocked.remove(uid)
    _save_json_list(BLOCKED_FILE, blocked)

    if uid in lock_meta:
        del lock_meta[uid]
        _save_json_dict(LOCK_META_FILE, lock_meta)

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
    lines = []
    for uid in display:
        tag = " 🔐" if _is_owner_locked(uid) else ""
        lines.append(f"<code>{uid}</code>{tag}")
    header = f"🔒 <b>Blocked IDs</b> ({len(blocked)} total, 🔐 = owner-locked):\n"
    footer = (
        f"\n… and {len(blocked) - MAX_LIST_DISPLAY} more"
        if len(blocked) > MAX_LIST_DISPLAY
        else ""
    )
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

    recent = list(reversed(seen[-MAX_LIST_DISPLAY:]))
    lines = [f"<code>{uid}</code>" for uid in recent]
    header = f"👁 <b>Recently seen IDs</b> ({len(seen)} total):\n"
    await msg.reply_text(header + "\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/whitelist — add a media UID to the whitelist.

    UIDs locked via /olock cannot be whitelisted (and thus indirectly
    unblocked) by anyone other than the owner.
    """
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
        await msg.reply_text(
            "Reply to a media message or pass a unique ID: <code>/whitelist &lt;uid&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if uid in whitelist:
        await msg.reply_text("ℹ️ Already whitelisted.")
        return

    # Prevent whitelist-based bypass of an owner lock
    if uid in blocked and _is_owner_locked(uid) and not _is_owner(user.id):
        await msg.reply_text(
            "🔐 This media was locked with /olock by the owner and cannot be "
            "whitelisted by other moderators."
        )
        return

    whitelist.append(uid)
    _save_json_list(WHITELIST_FILE, whitelist)

    if uid in blocked:
        blocked.remove(uid)
        _save_json_list(BLOCKED_FILE, blocked)
        if uid in lock_meta:
            del lock_meta[uid]
            _save_json_dict(LOCK_META_FILE, lock_meta)
        await msg.reply_text(
            f"✅ Whitelisted <code>{uid}</code> (also removed from blocklist)",
            parse_mode=ParseMode.HTML,
        )
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
        await msg.reply_text(
            "Reply to a media message or pass a unique ID: <code>/unwhitelist &lt;uid&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if uid not in whitelist:
        await msg.reply_text("ℹ️ That ID is not whitelisted.")
        return

    if _is_owner_locked(uid) and not _is_owner(user.id):
        await msg.reply_text(
            "🔐 This media is owner-locked; only the owner can modify its whitelist status."
        )
        return

    whitelist.remove(uid)
    _save_json_list(WHITELIST_FILE, whitelist)

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
        f"🔄 Reloaded: {len(blocked)} blocked, {len(seen)} seen, "
        f"{len(whitelist)} whitelisted, {len(authorized_mods)} authorized mods, "
        f"{len(chat_mute_settings)} chat mute settings, "
        f"{len(link_blacklist)} chats w/ link-blacklist entries."
    )
    await _log_event(
        context,
        action="RELOAD",
        user_id=user.id,
        username=user.username,
        chat_title=msg.chat.title if msg.chat else None,
    )


async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/warn — send a visible warning about blocked content (reply to a
    message) and always mute the target user for WARN_MUTE_SECONDS
    (temporary, regardless of the per-chat /enp /dsp setting).
    """
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
        f"⚠️ <b>Warning:</b> {mention}, this content violates group rules{uid_info}. "
        f"You have been muted for {WARN_MUTE_SECONDS} seconds.",
        parse_mode=ParseMode.HTML,
    )

    muted = False
    chat = msg.chat
    if target_user is not None and chat is not None:
        muted = await _mute_user_temporarily(context, chat.id, target_user.id, WARN_MUTE_SECONDS)

    await _log_event(
        context,
        action="WARN",
        user_id=user.id,
        username=user.username,
        chat_title=msg.chat.title if msg.chat else None,
        file_unique_id=media[1] if media else None,
        media_type=media[0] if media else None,
        extra=(
            f"Target user: {target_user.id if target_user else '?'} — "
            + (f"🔇 Muted for {WARN_MUTE_SECONDS}s" if muted else "⚠️ Mute failed (check bot admin rights)")
        ),
    )


def _resolve_target_user(msg: Message, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Resolve a target user id from a command's reply or first argument."""
    if msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user.id
    if context.args:
        try:
            return int(context.args[0])
        except ValueError:
            return None
    return None


async def cmd_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/auth <user_id> — owner-only. Grant a user moderator rights, even if
    they are not a Telegram group admin. Can also be used by replying to
    the target user's message.
    """
    msg = update.effective_message
    if msg is None:
        return
    user = msg.from_user
    if user is None or not _is_owner(user.id):
        await msg.reply_text("🚫 Only the owner can use this command.")
        return

    target_id = _resolve_target_user(msg, context)
    if target_id is None:
        await msg.reply_text(
            "Reply to a user's message or pass a user ID: <code>/auth &lt;user_id&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if target_id in authorized_mods:
        await msg.reply_text("ℹ️ That user is already an authorized moderator.")
        return

    authorized_mods.append(target_id)
    _save_json_list(AUTHORIZED_MODS_FILE, [str(uid) for uid in authorized_mods])

    await msg.reply_text(f"✅ Authorized <code>{target_id}</code> as a moderator.", parse_mode=ParseMode.HTML)
    await _log_event(
        context,
        action="AUTH",
        user_id=user.id,
        username=user.username,
        chat_title=msg.chat.title if msg.chat else None,
        extra=f"New moderator: {target_id}",
    )


async def cmd_deauth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/deauth <user_id> — owner-only. Revoke a previously authorized
    moderator's rights.
    """
    msg = update.effective_message
    if msg is None:
        return
    user = msg.from_user
    if user is None or not _is_owner(user.id):
        await msg.reply_text("🚫 Only the owner can use this command.")
        return

    target_id = _resolve_target_user(msg, context)
    if target_id is None:
        await msg.reply_text(
            "Reply to a user's message or pass a user ID: <code>/deauth &lt;user_id&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if target_id not in authorized_mods:
        await msg.reply_text("ℹ️ That user is not an authorized moderator.")
        return

    authorized_mods.remove(target_id)
    _save_json_list(AUTHORIZED_MODS_FILE, [str(uid) for uid in authorized_mods])

    await msg.reply_text(f"🗑 Revoked moderator rights from <code>{target_id}</code>.", parse_mode=ParseMode.HTML)
    await _log_event(
        context,
        action="DEAUTH",
        user_id=user.id,
        username=user.username,
        chat_title=msg.chat.title if msg.chat else None,
        extra=f"Removed moderator: {target_id}",
    )


async def cmd_enp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/enp — enable temporary muting of senders of locked media, for THIS
    chat only. Default is disabled; this persists per chat_id.
    """
    msg = update.effective_message
    if msg is None:
        return
    user = msg.from_user
    chat = update.effective_chat
    if user is None or chat is None or not await _is_admin(update, user.id):
        await msg.reply_text("🚫 You don't have permission to use this command.")
        return

    if chat.type == ChatType.PRIVATE:
        await msg.reply_text("This setting only applies to group chats.")
        return

    _set_chat_mute_enabled(chat.id, True)
    await msg.reply_text(
        f"🔇 Locked-media muting is now <b>enabled</b> for this chat "
        f"(temporary, {LOCKED_MEDIA_MUTE_SECONDS}s per offense).",
        parse_mode=ParseMode.HTML,
    )
    await _log_event(
        context,
        action="ENABLE_MUTE",
        user_id=user.id,
        username=user.username,
        chat_title=chat.title,
    )


async def cmd_dsp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/dsp — disable muting of senders of locked media, for THIS chat only.
    Locked media will still be deleted and logged.
    """
    msg = update.effective_message
    if msg is None:
        return
    user = msg.from_user
    chat = update.effective_chat
    if user is None or chat is None or not await _is_admin(update, user.id):
        await msg.reply_text("🚫 You don't have permission to use this command.")
        return

    if chat.type == ChatType.PRIVATE:
        await msg.reply_text("This setting only applies to group chats.")
        return

    _set_chat_mute_enabled(chat.id, False)
    await msg.reply_text(
        "🔈 Locked-media muting is now <b>disabled</b> for this chat "
        "(locked media will still be deleted and logged).",
        parse_mode=ParseMode.HTML,
    )
    await _log_event(
        context,
        action="DISABLE_MUTE",
        user_id=user.id,
        username=user.username,
        chat_title=chat.title,
    )


async def cmd_bl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/bl <user_id> — add a user to this chat's link-deletion blacklist.
    Any future message containing a link sent by this user in this chat
    will be auto-deleted, even if the user is an admin or the owner.
    Can also be used by replying to the target user's message.
    """
    msg = update.effective_message
    if msg is None:
        return
    user = msg.from_user
    chat = update.effective_chat
    if user is None or chat is None or not await _is_admin(update, user.id):
        await msg.reply_text("🚫 You don't have permission to use this command.")
        return

    if chat.type == ChatType.PRIVATE:
        await msg.reply_text("This setting only applies to group chats.")
        return

    target_id = _resolve_target_user(msg, context)
    if target_id is None:
        await msg.reply_text(
            "Reply to a user's message or pass a user ID: <code>/bl &lt;user_id&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if _is_link_blacklisted(chat.id, target_id):
        await msg.reply_text("ℹ️ That user is already link-blacklisted in this chat.")
        return

    _add_link_blacklist(chat.id, target_id)
    await msg.reply_text(
        f"🔗🚫 <code>{target_id}</code> is now link-blacklisted in this chat — "
        f"any message they send containing a link will be auto-deleted.",
        parse_mode=ParseMode.HTML,
    )
    await _log_event(
        context,
        action="LINK_BLACKLIST_ADD",
        user_id=user.id,
        username=user.username,
        chat_title=chat.title,
        extra=f"Blacklisted user: {target_id}",
    )


async def cmd_en(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/en <user_id> — remove a user from this chat's link-deletion
    blacklist. Can also be used by replying to the target user's message.
    """
    msg = update.effective_message
    if msg is None:
        return
    user = msg.from_user
    chat = update.effective_chat
    if user is None or chat is None or not await _is_admin(update, user.id):
        await msg.reply_text("🚫 You don't have permission to use this command.")
        return

    if chat.type == ChatType.PRIVATE:
        await msg.reply_text("This setting only applies to group chats.")
        return

    target_id = _resolve_target_user(msg, context)
    if target_id is None:
        await msg.reply_text(
            "Reply to a user's message or pass a user ID: <code>/en &lt;user_id&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if not _remove_link_blacklist(chat.id, target_id):
        await msg.reply_text("ℹ️ That user is not link-blacklisted in this chat.")
        return

    await msg.reply_text(
        f"✅ <code>{target_id}</code> removed from this chat's link-blacklist.",
        parse_mode=ParseMode.HTML,
    )
    await _log_event(
        context,
        action="LINK_BLACKLIST_REMOVE",
        user_id=user.id,
        username=user.username,
        chat_title=chat.title,
        extra=f"Unblacklisted user: {target_id}",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — show available commands, tailored to the requester's role."""
    msg = update.effective_message
    if msg is None:
        return
    user = msg.from_user
    if user is None:
        return

    is_owner = _is_owner(user.id)
    is_mod = await _is_admin(update, user.id)

    lines = [
        "<b>📖 Available commands</b>",
        "",
        "/id — reply to media to get its file_unique_id / file_id",
    ]

    if is_mod:
        lines += [
            "",
            "<b>Moderation</b>",
            "/lock &lt;uid&gt; — block a media item (reply or pass UID)",
            "/unlock &lt;uid&gt; — unblock a media item (blocked if owner-locked)",
            "/listlocks — list blocked UIDs (🔐 = owner-locked)",
            "/seen — list recently observed UIDs",
            "/whitelist &lt;uid&gt; — exempt a UID from blocking",
            "/unwhitelist &lt;uid&gt; — remove a UID from the whitelist",
            "/warn — reply to a message to warn + mute its sender "
            f"for {WARN_MUTE_SECONDS}s",
            "/enp — enable temporary mute-on-locked-media for this chat",
            "/dsp — disable mute-on-locked-media for this chat (default)",
            "/bl &lt;user_id&gt; — link-blacklist a user (auto-delete their "
            "messages containing links, even if admin/owner)",
            "/en &lt;user_id&gt; — remove a user from the link-blacklist",
            "/reload — reload JSON data from disk",
        ]

    if is_owner:
        lines += [
            "",
            "<b>Owner-only</b>",
            "/olock &lt;uid&gt; — owner-lock a media item (only the owner can unlock it)",
            "/auth &lt;user_id&gt; — grant moderator rights",
            "/deauth &lt;user_id&gt; — revoke moderator rights",
        ]

    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — basic greeting / sanity check."""
    msg = update.effective_message
    if msg is None:
        return
    await msg.reply_text(
        "👋 Media moderation bot is running. Use /help to see available commands."
    )


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log unhandled exceptions raised while processing updates."""
    logger.exception("Unhandled exception while processing update: %s", update, exc_info=context.error)


# ---------------------------------------------------------------------------
# Application bootstrap
# ---------------------------------------------------------------------------


def build_application() -> Application:
    """Construct and configure the Application with all handlers registered."""
    load_all()

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("lock", cmd_lock))
    app.add_handler(CommandHandler("olock", cmd_olock))
    app.add_handler(CommandHandler("unlock", cmd_unlock))
    app.add_handler(CommandHandler("listlocks", cmd_listlocks))
    app.add_handler(CommandHandler("seen", cmd_seen))
    app.add_handler(CommandHandler("whitelist", cmd_whitelist))
    app.add_handler(CommandHandler("unwhitelist", cmd_unwhitelist))
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(CommandHandler("warn", cmd_warn))
    app.add_handler(CommandHandler("auth", cmd_auth))
    app.add_handler(CommandHandler("deauth", cmd_deauth))
    app.add_handler(CommandHandler("enp", cmd_enp))
    app.add_handler(CommandHandler("dsp", cmd_dsp))
    app.add_handler(CommandHandler("bl", cmd_bl))
    app.add_handler(CommandHandler("en", cmd_en))

    # Media tracking / enforcement — catches any message containing
    # supported media types, in groups and supergroups. Also runs
    # link-blacklist enforcement first (covers captions on media messages).
    media_filter = (
        filters.PHOTO
        | filters.VIDEO
        | filters.Sticker.ALL
        | filters.ANIMATION
        | filters.Document.ALL
        | filters.VOICE
        | filters.VIDEO_NOTE
        | filters.AUDIO
    )
    app.add_handler(MessageHandler(media_filter, on_message))

    # Plain-text (non-media, non-command) messages — link-blacklist
    # enforcement only.
    text_filter = filters.TEXT & ~filters.COMMAND
    app.add_handler(MessageHandler(text_filter, on_text_message))

    app.add_error_handler(on_error)

    return app


def main() -> None:
    logger.info("Starting Telegram Media Moderation Bot (demo_mode=%s)", DEMO_MODE)
    app = build_application()
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
