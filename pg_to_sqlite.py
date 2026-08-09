import asyncio
import sqlite3
import asyncpg
import sys
import os
from pathlib import Path

# Railway Postgres URL provided by user
PG_URL = "postgresql://postgres:bMCcNTqVeCRZvLKKeWRGXmJMboMcGNOJ@junction.proxy.rlwy.net:50273/railway"
SQLITE_PATH = "data/bot_data.db"

SCHEMA_SQLITE = """
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

async def main():
    print(f"Connecting to Postgres: {PG_URL}...")
    try:
        pg_conn = await asyncpg.connect(PG_URL, timeout=10)
    except Exception as e:
        print(f"Failed to connect to Postgres. The project might be fully shut down on Railway. Error: {e}")
        sys.exit(1)

    print("Connected to Postgres successfully!")

    Path("data").mkdir(exist_ok=True)
    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_conn.executescript(SCHEMA_SQLITE)

    cursor = sqlite_conn.cursor()

    tables = [
        "blocked_media",
        "seen_media",
        "whitelisted_media",
        "lock_meta",
        "authorized_mods",
        "chat_mute_settings",
        "link_blacklist"
    ]

    for table in tables:
        print(f"Migrating table {table}...")
        try:
            rows = await pg_conn.fetch(f"SELECT * FROM {table}")
        except asyncpg.exceptions.UndefinedTableError:
            print(f"  Table {table} does not exist in Postgres. Skipping.")
            continue

        if not rows:
            print(f"  0 rows found.")
            continue

        # Build insert query dynamically
        columns = rows[0].keys()
        placeholders = ", ".join(["?"] * len(columns))
        query = f"INSERT OR IGNORE INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
        
        data = []
        for row in rows:
            # Convert values if needed (e.g., boolean to int for SQLite, datetime to isoformat string)
            vals = []
            for col in columns:
                val = row[col]
                if isinstance(val, bool):
                    val = 1 if val else 0
                elif hasattr(val, "isoformat"):
                    val = val.isoformat()
                vals.append(val)
            data.append(tuple(vals))
            
        cursor.executemany(query, data)
        sqlite_conn.commit()
        print(f"  Migrated {len(data)} rows.")

    await pg_conn.close()
    sqlite_conn.close()
    print("Migration complete! You can now safely switch the bot to use SQLite.")

if __name__ == "__main__":
    asyncio.run(main())
