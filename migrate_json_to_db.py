"""
One-time migration: import existing JSON-based bot data into PostgreSQL.

Usage:
    python migrate_json_to_db.py [--json-dir data] [--yes]

Run this ONCE, with the bot STOPPED, before switching bot.py over to use
the database-backed persistence layer. It is safe to re-run: every insert
uses ON CONFLICT DO NOTHING, so re-running will not create duplicates or
overwrite newer database rows.

Before running:
    - Stop the bot — avoid concurrent writes to the JSON files or the
      database happening at the same time as this migration.
    - Make sure DATABASE_URL is set to the same value bot.py will use
      (use the public/proxy connection string if running this locally
      against a Railway-hosted database).
    - Keep a backup of your `data/` directory. This script only reads the
      JSON files; it never deletes or modifies them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    sys.exit("ERROR: DATABASE_URL is not set. Check your .env file.")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS blocked_media (
    uid TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS seen_media (
    uid TEXT PRIMARY KEY,
    seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS whitelisted_media (
    uid TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS lock_meta (
    uid TEXT PRIMARY KEY REFERENCES blocked_media(uid) ON DELETE CASCADE,
    locked_by BIGINT,
    username TEXT,
    locked_at TIMESTAMPTZ,
    owner_lock BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS authorized_mods (
    user_id BIGINT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS chat_mute_settings (
    chat_id BIGINT PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS link_blacklist (
    chat_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
"""


def _load_json_list(path: Path) -> list:
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as exc:
        print(f"WARNING: could not read {path}: {exc}")
        return []


def _load_json_dict(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        print(f"WARNING: could not read {path}: {exc}")
        return {}


async def migrate(json_dir: Path) -> None:
    pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=3)
    try:
        async with pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)

            # --- blocked media ---------------------------------------------
            blocked = _load_json_list(json_dir / "blocked.json")
            for uid in blocked:
                await conn.execute(
                    "INSERT INTO blocked_media (uid) VALUES ($1) ON CONFLICT DO NOTHING",
                    str(uid),
                )
            print(f"blocked_media: migrated {len(blocked)} row(s)")

            # --- whitelist ---------------------------------------------------
            whitelist = _load_json_list(json_dir / "whitelist.json")
            for uid in whitelist:
                await conn.execute(
                    "INSERT INTO whitelisted_media (uid) VALUES ($1) ON CONFLICT DO NOTHING",
                    str(uid),
                )
            print(f"whitelisted_media: migrated {len(whitelist)} row(s)")

            # --- lock meta (ensure FK target exists first) --------------------
            lock_meta = _load_json_dict(json_dir / "lock_meta.json")
            migrated_locks = 0
            skipped_locks = 0
            for uid, meta in lock_meta.items():
                if not isinstance(meta, dict):
                    skipped_locks += 1
                    continue

                # Guard against JSON drift where lock_meta.json referenced a
                # UID that had fallen out of blocked.json.
                await conn.execute(
                    "INSERT INTO blocked_media (uid) VALUES ($1) ON CONFLICT DO NOTHING",
                    str(uid),
                )

                username = meta.get("username")
                owner_lock = bool(meta.get("owner_lock", False))

                locked_by_raw = meta.get("locked_by")
                try:
                    locked_by = int(locked_by_raw) if locked_by_raw is not None else None
                except (TypeError, ValueError):
                    locked_by = None

                timestamp_raw = meta.get("timestamp")
                try:
                    locked_at = (
                        datetime.fromisoformat(timestamp_raw) if timestamp_raw else datetime.now(timezone.utc)
                    )
                except ValueError:
                    locked_at = datetime.now(timezone.utc)

                await conn.execute(
                    """
                    INSERT INTO lock_meta (uid, locked_by, username, locked_at, owner_lock)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (uid) DO NOTHING
                    """,
                    str(uid), locked_by, username, locked_at, owner_lock,
                )
                migrated_locks += 1
            print(f"lock_meta: migrated {migrated_locks} row(s), skipped {skipped_locks} malformed entr(y/ies)")

            # --- seen media (preserve relative recency order) -----------------
            seen = _load_json_list(json_dir / "seen.json")
            base = datetime.now(timezone.utc)
            skipped_seen = 0
            for i, uid in enumerate(seen):
                # seen.json is oldest-first (index 0 = oldest, due to the
                # bot's pop(0) eviction). Assign strictly increasing
                # timestamps so `ORDER BY seen_at` reproduces that order.
                ts = base + timedelta(milliseconds=i)
                try:
                    await conn.execute(
                        "INSERT INTO seen_media (uid, seen_at) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                        str(uid), ts,
                    )
                except Exception as exc:  # noqa: BLE001 - defensive, log & continue
                    print(f"WARNING: skipped seen uid {uid!r}: {exc}")
                    skipped_seen += 1
            print(f"seen_media: migrated {len(seen) - skipped_seen} row(s), skipped {skipped_seen}")

            # --- authorized mods -----------------------------------------------
            raw_mods = _load_json_list(json_dir / "authorized_mods.json")
            migrated_mods = 0
            skipped_mods = 0
            for entry in raw_mods:
                try:
                    user_id = int(entry)
                except (TypeError, ValueError):
                    skipped_mods += 1
                    continue
                await conn.execute(
                    "INSERT INTO authorized_mods (user_id) VALUES ($1) ON CONFLICT DO NOTHING",
                    user_id,
                )
                migrated_mods += 1
            print(f"authorized_mods: migrated {migrated_mods} row(s), skipped {skipped_mods}")

            # --- chat mute settings ---------------------------------------------
            chat_mute = _load_json_dict(json_dir / "chat_mute_settings.json")
            migrated_mute = 0
            skipped_mute = 0
            for chat_id_str, enabled in chat_mute.items():
                try:
                    chat_id = int(chat_id_str)
                except (TypeError, ValueError):
                    skipped_mute += 1
                    continue
                await conn.execute(
                    """
                    INSERT INTO chat_mute_settings (chat_id, enabled) VALUES ($1, $2)
                    ON CONFLICT (chat_id) DO NOTHING
                    """,
                    chat_id, bool(enabled),
                )
                migrated_mute += 1
            print(f"chat_mute_settings: migrated {migrated_mute} row(s), skipped {skipped_mute}")

            # --- link blacklist ------------------------------------------------
            link_bl = _load_json_dict(json_dir / "link_blacklist.json")
            migrated_bl = 0
            skipped_bl = 0
            for chat_id_str, user_ids in link_bl.items():
                try:
                    chat_id = int(chat_id_str)
                except (TypeError, ValueError):
                    skipped_bl += len(user_ids) if isinstance(user_ids, list) else 0
                    continue
                if not isinstance(user_ids, list):
                    continue
                for uid_str in user_ids:
                    try:
                        user_id = int(uid_str)
                    except (TypeError, ValueError):
                        skipped_bl += 1
                        continue
                    await conn.execute(
                        """
                        INSERT INTO link_blacklist (chat_id, user_id) VALUES ($1, $2)
                        ON CONFLICT DO NOTHING
                        """,
                        chat_id, user_id,
                    )
                    migrated_bl += 1
            print(f"link_blacklist: migrated {migrated_bl} row(s), skipped {skipped_bl}")

    finally:
        await pool.close()

    print("\nMigration complete.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate JSON moderation data into PostgreSQL.")
    parser.add_argument("--json-dir", default="data", help="Directory containing the JSON files (default: data)")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    args = parser.parse_args()

    json_dir = Path(args.json_dir)
    if not json_dir.exists():
        sys.exit(f"ERROR: JSON directory not found: {json_dir}")

    if not args.yes:
        print(f"This will migrate JSON files from '{json_dir}' into the database at DATABASE_URL.")
        print("Make sure the bot is STOPPED before continuing (see the module docstring).")
        confirm = input("Continue? [y/N] ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            return

    asyncio.run(migrate(json_dir))


if __name__ == "__main__":
    main()
