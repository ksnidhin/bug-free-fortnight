"""
Telegram Media Moderation Bot
==============================
Production-ready group moderation bot built with python-telegram-bot v21+.

Detects, logs, and optionally blocks media by Telegram file_unique_id.
Supports stickers, photos, videos, documents, GIFs, voice, video notes,
and audio. Uses PostgreSQL for persistent storage (via asyncpg), so state
survives container redeploys/recreations and is not tied to any single
host's local filesystem.

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

PERSISTENCE: All moderation state lives in PostgreSQL. Set DATABASE_URL to
your Postgres connection string (Railway's Postgres plugin injects this
automatically). See migrate_json_to_db.py for a one-time import of legacy
data/*.json files into the database.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import sqlite3
from dotenv import load_dotenv
from telegram import ChatPermissions, Message, Update
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from ai_handler import cmd_ai, check_auto_ai
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
DATABASE_URL: str = os.getenv("DATABASE_URL", "")
SQLITE_PATH = "data/bot_data.db"

if not BOT_TOKEN:
    sys.exit("ERROR: BOT_TOKEN is not set. Check your .env file.")
if not OWNER_ID:
    sys.exit("ERROR: OWNER_ID is not set. Check your .env file.")
if False: # if not DATABASE_URL:
    sys.exit(
        "ERROR: DATABASE_URL is not set. Check your .env file. "
        "(Railway's Postgres plugin injects this automatically.)"
    )

DEMO_MODE: bool = os.getenv("DEMO_MODE", "false").lower() in ("1", "true", "yes")

MAX_SEEN: int = 500  # cap seen_media rows to avoid unbounded growth
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

# Matches http(s):// URLs, t.me/telegram.me links, www. links, hxxp-mangled
# schemes, dot-substituted domains ("example dot com"), and bare domains
# with a (deliberately broad) TLD list including ones spam/ad networks
# favor precisely because generic filters don't expect them.
# Case-insensitive. Applied to text that has already been run through
# _normalize_for_link_scan() to defeat invisible-character and homoglyph
# obfuscation — see that function for why.
LINK_REGEX = re.compile(
    r"(?:https?://|hxxps?://|t\.me/|telegram\.me/|www\.)\S+"
    r"|\bt\s*\.\s*me\b(?:/\S*)?"
    r"|\b[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.|\s+dot\s+|\(dot\)|\[dot\])"
    r"(?:com|net|org|io|me|co|info|biz|xyz|dev|app|gg|link|club|ru|uk|de|cn|"
    r"us|in|to|ly|gl|tv|shop|online|site|store|vip|cc|pw|icu|top|cfd|sbs|"
    r"cyou|rest|click|fun|life|mov|zip|bond|win|bar|monster|website)\b(?:/\S*)?",
    re.IGNORECASE,
)

_LINK_ENTITY_TYPES = {"url", "text_link"}

# Invisible/zero-width characters spam bots insert mid-URL to break naive
# string/regex matching while the message still looks normal to a human
# reader (e.g. "t​.​me/xyz" with U+200B between every character).
_INVISIBLE_CHARS_TRANSLATION = str.maketrans(
    "", "", "\u200b\u200c\u200d\u2060\ufeff\u00ad\u2028\u2029"
)

# Cyrillic/Greek/other look-alike letters (and look-alike punctuation)
# commonly swapped in for their Latin equivalents so a link *looks*
# identical to a human but doesn't match a Latin-only regex. The Cyrillic
# "т" (U+0442) in particular is pixel-identical to Latin "t" in most fonts
# and is a classic trick for disguising "t.me" links specifically.
_HOMOGLYPH_TRANSLATION = str.maketrans(
    {
        "а": "a", "А": "A",
        "е": "e", "Е": "E",
        "о": "o", "О": "O",
        "р": "p", "Р": "P",
        "с": "c", "С": "C",
        "у": "y", "У": "Y",
        "х": "x", "Х": "X",
        "т": "t", "Т": "T",
        "м": "m", "М": "M",
        "і": "i", "І": "I",
        "ѕ": "s", "Ѕ": "S",
        "ј": "j", "Ј": "J",
        "ԁ": "d",
        "ɡ": "g",
        "һ": "h", "Һ": "H",
        "ⅼ": "l",
        "∕": "/", "⁄": "/",
        "。": ".", "．": ".", "｡": ".", "·": ".", "・": ".",
    }
)


def _normalize_for_link_scan(text: str) -> str:
    """Strip invisible characters, collapse Unicode compatibility forms
    (e.g. fullwidth punctuation), and map look-alike homoglyphs to their
    plain-Latin equivalents, so the regex below sees the link the way a
    human actually reads it rather than the disguised raw bytes.
    """
    if not text:
        return ""
    text = text.translate(_INVISIBLE_CHARS_TRANSLATION)
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_HOMOGLYPH_TRANSLATION)
    return text


def _message_contains_link(msg: Message) -> bool:
    """Return True if *msg* contains a link — via Telegram-parsed entities,
    a normalized regex scan of the text/caption (defeating invisible-char
    and homoglyph obfuscation), or a URL attached to an inline keyboard
    button. Promotional/ad bots frequently place their invite link only on
    a button (e.g. "Join Channel") with no link in the visible text, so the
    button URLs must be checked too.
    """
    entities = list(msg.entities or []) + list(msg.caption_entities or [])
    for ent in entities:
        if ent.type in _LINK_ENTITY_TYPES:
            return True

    raw_text = msg.text or msg.caption or ""
    text = _normalize_for_link_scan(raw_text)
    if text and LINK_REGEX.search(text):
        return True

    markup = msg.reply_markup
    if markup and markup.inline_keyboard:
        for row in markup.inline_keyboard:
            for button in row:
                if getattr(button, "url", None):
                    return True

    return False


# ---------------------------------------------------------------------------
# Database layer (SQLite via aiosqlite)
# ---------------------------------------------------------------------------

db_conn: aiosqlite.Connection | None = None

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS blocked_media (
    uid TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS seen_media (
    uid TEXT PRIMARY KEY,
    seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS whitelisted_media (
    uid TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS lock_meta (
    uid TEXT PRIMARY KEY REFERENCES blocked_media(uid) ON DELETE CASCADE,
    locked_by INTEGER,
    username TEXT,
    locked_at TIMESTAMP,
    owner_lock INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS authorized_mods (
    user_id INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS chat_mute_settings (
    chat_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS link_blacklist (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
"""


async def db_connect() -> None:
    """Open the SQLite connection."""
    global db_conn  # noqa: PLW0603
    Path("data").mkdir(exist_ok=True)
    db_conn = await aiosqlite.connect(SQLITE_PATH)
    db_conn.row_factory = aiosqlite.Row
    import logging
    logging.getLogger("modbot").info("Connected to SQLite.")


async def db_close() -> None:
    """Close the SQLite connection."""
    if db_conn is not None:
        await db_conn.close()
        import logging
        logging.getLogger("modbot").info("Closed SQLite connection.")


async def db_init_schema() -> None:
    """Create all tables if they don't already exist."""
    assert db_conn is not None
    await db_conn.executescript(_SCHEMA_SQL)
    await db_conn.commit()
    import logging
    logging.getLogger("modbot").info("Database schema ready.")


# Individual mutation helpers

async def _db_add_blocked(uid: str) -> None:
    await db_conn.execute("INSERT OR IGNORE INTO blocked_media (uid) VALUES (?)", (uid,))
    await db_conn.commit()


async def _db_remove_blocked(uid: str) -> None:
    await db_conn.execute("DELETE FROM blocked_media WHERE uid = ?", (uid,))
    await db_conn.commit()


async def _db_add_whitelist(uid: str) -> None:
    await db_conn.execute("INSERT OR IGNORE INTO whitelisted_media (uid) VALUES (?)", (uid,))
    await db_conn.commit()


async def _db_remove_whitelist(uid: str) -> None:
    await db_conn.execute("DELETE FROM whitelisted_media WHERE uid = ?", (uid,))
    await db_conn.commit()


async def _db_set_lock_meta(uid: str, locked_by: int, username: str | None, owner_lock: bool) -> None:
    await db_conn.execute(
        """
        INSERT INTO lock_meta (uid, locked_by, username, locked_at, owner_lock)
        VALUES (?, ?, ?, datetime('now'), ?)
        ON CONFLICT (uid) DO UPDATE SET
            locked_by = EXCLUDED.locked_by,
            username = EXCLUDED.username,
            locked_at = EXCLUDED.locked_at,
            owner_lock = EXCLUDED.owner_lock
        """,
        (uid, locked_by, username, 1 if owner_lock else 0),
    )
    await db_conn.commit()


async def _db_remove_lock_meta(uid: str) -> None:
    await db_conn.execute("DELETE FROM lock_meta WHERE uid = ?", (uid,))
    await db_conn.commit()


async def _db_add_seen(uid: str) -> None:
    await db_conn.execute("INSERT OR IGNORE INTO seen_media (uid) VALUES (?)", (uid,))
    await db_conn.commit()


async def _db_trim_seen(max_seen: int) -> None:
    await db_conn.execute(
        """
        DELETE FROM seen_media
        WHERE uid IN (
            SELECT uid FROM seen_media ORDER BY seen_at ASC LIMIT -1 OFFSET ?
        )
        """,
        (max_seen,),
    )
    await db_conn.commit()


async def _db_add_mod(user_id: int) -> None:
    await db_conn.execute("INSERT OR IGNORE INTO authorized_mods (user_id) VALUES (?)", (user_id,))
    await db_conn.commit()


async def _db_remove_mod(user_id: int) -> None:
    await db_conn.execute("DELETE FROM authorized_mods WHERE user_id = ?", (user_id,))
    await db_conn.commit()


async def _db_set_chat_mute(chat_id: int, enabled: bool) -> None:
    await db_conn.execute(
        """
        INSERT INTO chat_mute_settings (chat_id, enabled) VALUES (?, ?)
        ON CONFLICT (chat_id) DO UPDATE SET enabled = EXCLUDED.enabled
        """,
        (chat_id, 1 if enabled else 0),
    )
    await db_conn.commit()


async def _db_add_link_blacklist(chat_id: int, user_id: int) -> None:
    await db_conn.execute(
        "INSERT OR IGNORE INTO link_blacklist (chat_id, user_id) VALUES (?, ?)",
        (chat_id, user_id),
    )
    await db_conn.commit()


async def _db_remove_link_blacklist(chat_id: int, user_id: int) -> None:
    await db_conn.execute(
        "DELETE FROM link_blacklist WHERE chat_id = ? AND user_id = ?", 
        (chat_id, user_id)
    )
    await db_conn.commit()


# ---------------------------------------------------------------------------
# In-memory data stores (loaded once, synced to the database on mutation)
# ---------------------------------------------------------------------------

blocked: list[str] = []
seen: list[str] = []
whitelist: list[str] = []
lock_meta: dict[str, Any] = {}
authorized_mods: list[int] = []
chat_mute_settings: dict[str, bool] = {}
link_blacklist: dict[str, list[str]] = {}


async def load_all() -> None:
    """(Re)load every store from the database into memory."""
    global blocked, seen, whitelist, lock_meta, authorized_mods, chat_mute_settings, link_blacklist  # noqa: PLW0603
    assert db_conn is not None

    async with db_conn.execute("SELECT uid FROM blocked_media") as cursor:
        blocked = [r["uid"] for r in await cursor.fetchall()]
        
    async with db_conn.execute("SELECT uid FROM seen_media ORDER BY seen_at ASC") as cursor:
        seen = [r["uid"] for r in await cursor.fetchall()]
        
    async with db_conn.execute("SELECT uid FROM whitelisted_media") as cursor:
        whitelist = [r["uid"] for r in await cursor.fetchall()]

    async with db_conn.execute("SELECT uid, locked_by, username, locked_at, owner_lock FROM lock_meta") as cursor:
        lock_meta = {
            r["uid"]: {
                "locked_by": r["locked_by"],
                "username": r["username"],
                "timestamp": r["locked_at"] if r["locked_at"] else None,
                "owner_lock": bool(r["owner_lock"]),
            }
            for r in await cursor.fetchall()
        }

    async with db_conn.execute("SELECT user_id FROM authorized_mods") as cursor:
        authorized_mods = [r["user_id"] for r in await cursor.fetchall()]

    async with db_conn.execute("SELECT chat_id, enabled FROM chat_mute_settings") as cursor:
        chat_mute_settings = {
            str(r["chat_id"]): bool(r["enabled"])
            for r in await cursor.fetchall()
        }

    async with db_conn.execute("SELECT chat_id, user_id FROM link_blacklist") as cursor:
        link_blacklist = {}
        for r in await cursor.fetchall():
            link_blacklist.setdefault(str(r["chat_id"]), []).append(str(r["user_id"]))

    import logging
    logging.getLogger("modbot").info(
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


async def _set_chat_mute_enabled(chat_id: int, enabled: bool) -> None:
    """Persist the mute-on-locked-media setting for *chat_id*."""
    chat_mute_settings[str(chat_id)] = enabled
    await _db_set_chat_mute(chat_id, enabled)


# ---------------------------------------------------------------------------
# Per-chat, per-user link-deletion blacklist
# ---------------------------------------------------------------------------


def _get_sender_identity(msg: Message) -> tuple[int | None, str | None]:
    """Return (sender_id, label) for *msg*, covering both normal senders
    (msg.from_user) and channel/anonymous-identity senders (msg.sender_chat).
    Some bots and linked channels post with from_user unset and sender_chat
    set instead — without this fallback the blacklist check would silently
    skip those messages entirely.
    """
    if msg.from_user is not None:
        return msg.from_user.id, (f"@{msg.from_user.username}" if msg.from_user.username else msg.from_user.full_name)
    if msg.sender_chat is not None:
        return msg.sender_chat.id, (msg.sender_chat.title or msg.sender_chat.username)
    return None, None


def _is_link_blacklisted(chat_id: int, user_id: int) -> bool:
    """Return True if *user_id* is on the link-deletion blacklist for
    *chat_id*.
    """
    return str(user_id) in link_blacklist.get(str(chat_id), [])


async def _add_link_blacklist(chat_id: int, user_id: int) -> None:
    """Add *user_id* to the link-deletion blacklist for *chat_id* and
    persist it.
    """
    key = str(chat_id)
    members = link_blacklist.setdefault(key, [])
    if str(user_id) not in members:
        members.append(str(user_id))
    await _db_add_link_blacklist(chat_id, user_id)


async def _remove_link_blacklist(chat_id: int, user_id: int) -> bool:
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
    await _db_remove_link_blacklist(chat_id, user_id)
    return True


async def _enforce_link_blacklist(msg: Message, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """If *msg* was sent by a link-blacklisted user in this chat AND
    contains a link, delete it — regardless of the sender's admin/owner
    status — and log the action. Returns True if the message was deleted.
    """
    chat = msg.chat
    if chat is None:
        return False
    if chat.type == ChatType.PRIVATE:
        return False

    sender_id, sender_label = _get_sender_identity(msg)
    if sender_id is None:
        return False
    if not _is_link_blacklisted(chat.id, sender_id):
        return False
    if not _message_contains_link(msg):
        return False

    try:
        await msg.delete()
        logger.info(
            "Deleted link message from blacklisted sender %s in chat %s",
            sender_id,
            chat.id,
        )
    except (BadRequest, Forbidden) as exc:
        logger.warning("Cannot delete blacklisted-sender link message: %s", exc)

    await _log_event(
        context,
        action="LINK_BLACKLIST_DELETE",
        user_id=sender_id,
        username=sender_label,
        chat_title=chat.title,
        extra="Deleted message containing a link from a link-blacklisted sender",
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


async def _record_seen(uid: str) -> None:
    """Add *uid* to the seen list (capped at MAX_SEEN)."""
    if uid in seen:
        return
    seen.append(uid)
    # Evict oldest entries when the list grows too large
    trimmed = False
    while len(seen) > MAX_SEEN:
        seen.pop(0)
        trimmed = True
    await _db_add_seen(uid)
    if trimmed:
        await _db_trim_seen(MAX_SEEN)


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
    await _record_seen(uid)

    blocked_uid = uid
    if media_type == "sticker" and getattr(msg, "sticker", None) and msg.sticker.set_name:
        pack_uid = f"stp:{msg.sticker.set_name}"
        if pack_uid in blocked:
            blocked_uid = pack_uid

    # Skip whitelisted IDs
    if blocked_uid in whitelist:
        return

    # Enforce blocklist
    if blocked_uid not in blocked:
        # Check AI for captions if not blocked
        if msg.caption:
            await check_auto_ai(update, context)
        return

    uid = blocked_uid

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
    is_stp_command = context.args and context.args[0].lower() in ("stp", "pack")

    if context.args and not is_stp_command:
        uid = context.args[0]
        media_type = "manual"
    elif msg.reply_to_message:
        media = _extract_media(msg.reply_to_message)
        if media:
            media_type, uid, _ = media
            if is_stp_command:
                if media_type == "sticker" and msg.reply_to_message.sticker and msg.reply_to_message.sticker.set_name:
                    uid = f"stp:{msg.reply_to_message.sticker.set_name}"
                    media_type = "sticker_pack"
                else:
                    await msg.reply_text("⚠️ Reply to a sticker that belongs to a sticker pack to use /lock pack.")
                    return

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
    await _db_add_blocked(uid)

    lock_meta[uid] = {
        "locked_by": user.id,
        "username": user.username,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "owner_lock": owner_lock,
    }
    await _db_set_lock_meta(uid, user.id, user.username, owner_lock)

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
    is_stp_command = context.args and context.args[0].lower() in ("stp", "pack")

    if context.args and not is_stp_command:
        uid = context.args[0]
    elif msg.reply_to_message:
        media = _extract_media(msg.reply_to_message)
        if media:
            media_type, uid, _ = media
            if is_stp_command:
                if media_type == "sticker" and msg.reply_to_message.sticker and msg.reply_to_message.sticker.set_name:
                    uid = f"stp:{msg.reply_to_message.sticker.set_name}"
                else:
                    await msg.reply_text("⚠️ Reply to a sticker that belongs to a sticker pack to use /unlock pack.")
                    return

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
    await _db_remove_blocked(uid)

    if uid in lock_meta:
        del lock_meta[uid]
        await _db_remove_lock_meta(uid)

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
    is_stp_command = context.args and context.args[0].lower() == "stp"

    if context.args and not is_stp_command:
        uid = context.args[0]
    elif msg.reply_to_message:
        media = _extract_media(msg.reply_to_message)
        if media:
            media_type, uid, _ = media
            if is_stp_command:
                if media_type == "sticker" and msg.reply_to_message.sticker and msg.reply_to_message.sticker.set_name:
                    uid = f"stp:{msg.reply_to_message.sticker.set_name}"
                else:
                    await msg.reply_text("⚠️ Reply to a sticker that belongs to a sticker pack to use /unlock stp.")
                    return

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
    await _db_add_whitelist(uid)

    if uid in blocked:
        blocked.remove(uid)
        await _db_remove_blocked(uid)
        if uid in lock_meta:
            del lock_meta[uid]
            await _db_remove_lock_meta(uid)
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
    is_stp_command = context.args and context.args[0].lower() == "stp"

    if context.args and not is_stp_command:
        uid = context.args[0]
    elif msg.reply_to_message:
        media = _extract_media(msg.reply_to_message)
        if media:
            media_type, uid, _ = media
            if is_stp_command:
                if media_type == "sticker" and msg.reply_to_message.sticker and msg.reply_to_message.sticker.set_name:
                    uid = f"stp:{msg.reply_to_message.sticker.set_name}"
                else:
                    await msg.reply_text("⚠️ Reply to a sticker that belongs to a sticker pack to use /unlock stp.")
                    return

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
    await _db_remove_whitelist(uid)

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
    """/reload — reload all persisted state from the database."""
    msg = update.effective_message
    if msg is None:
        return
    user = msg.from_user
    if user is None or not await _is_admin(update, user.id):
        await msg.reply_text("🚫 You don't have permission to use this command.")
        return

    await load_all()
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
    """Resolve a target user id from a command's reply or first argument.
    Falls back to the replied message's sender_chat id when from_user is
    unset (e.g. replying to a channel-identity or anonymous-style post).
    """
    if msg.reply_to_message:
        replied = msg.reply_to_message
        if replied.from_user:
            return replied.from_user.id
        if replied.sender_chat:
            return replied.sender_chat.id
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
    await _db_add_mod(target_id)

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
    await _db_remove_mod(target_id)

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

    await _set_chat_mute_enabled(chat.id, True)
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

    await _set_chat_mute_enabled(chat.id, False)
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

    target_is_bot = False
    if msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.id == target_id:
        target_is_bot = msg.reply_to_message.from_user.is_bot

    await _add_link_blacklist(chat.id, target_id)

    reply_text = (
        f"🔗🚫 <code>{target_id}</code> is now link-blacklisted in this chat — "
        f"any message they send containing a link will be auto-deleted."
    )
    if target_is_bot:
        reply_text += (
            "\n\n⚠️ <i>Note: To delete links from other bots, you MUST enable "
            "\"Bot-to-Bot Communication Mode\" for this bot via @BotFather.</i>"
        )

    await msg.reply_text(
        reply_text,
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

    if not await _remove_link_blacklist(chat.id, target_id):
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
            "/lock pack — block an entire sticker pack (reply to a sticker)",
            "/unlock &lt;uid&gt; — unblock a media item",
            "/unlock pack — unblock an entire sticker pack",
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
            "/reload — reload data from the database",
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


async def _post_init(app: Application) -> None:  # noqa: ARG001
    """Runs once, after the Application is initialized but before polling
    starts. Opens the database pool, ensures the schema exists, and loads
    all persisted state into memory.
    """
    await db_connect()
    await db_init_schema()
    await load_all()


async def _post_shutdown(app: Application) -> None:  # noqa: ARG001
    """Runs once during a clean shutdown — closes the database pool."""
    await db_close()


def build_application() -> Application:
    """Construct and configure the Application with all handlers registered."""
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("lock", cmd_lock))
    app.add_handler(CommandHandler("ai", cmd_ai))
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
