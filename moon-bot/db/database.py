import aiosqlite
import logging

logger = logging.getLogger(__name__)

DB_PATH = "moon_bot.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                display_name TEXT
            )
        """)
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_relationships (
                user_id INTEGER PRIMARY KEY,
                relationship_level TEXT DEFAULT 'stranger',
                affection INTEGER DEFAULT 0,
                trust INTEGER DEFAULT 0,
                roast_level INTEGER DEFAULT 0,
                kaleshi_level INTEGER DEFAULT 0,
                current_mood TEXT DEFAULT 'neutral',
                argument_state TEXT DEFAULT 'none',
                preferred_style TEXT DEFAULT 'normal',
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            )
        """)
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS conversation_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            )
        """)
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                memory_text TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            )
        """)
        
        await db.commit()
        logger.info("Database initialized.")

async def get_db():
    return await aiosqlite.connect(DB_PATH)
