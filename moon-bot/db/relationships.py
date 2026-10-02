import aiosqlite
from .database import get_db

async def get_or_create_user(telegram_id: int, display_name: str = None) -> int:
    async with await get_db() as db:
        async with db.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return row[0]
            
        await db.execute(
            "INSERT INTO users (telegram_id, display_name) VALUES (?, ?)",
            (telegram_id, display_name)
        )
        await db.commit()
        
        async with db.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)) as cursor:
            row = await cursor.fetchone()
            user_id = row[0]
            
        await db.execute(
            "INSERT INTO user_relationships (user_id) VALUES (?)",
            (user_id,)
        )
        await db.commit()
        return user_id

async def get_user_relationship(user_id: int) -> dict:
    async with await get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM user_relationships WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return None

async def update_argument_state(user_id: int, state: str) -> None:
    async with await get_db() as db:
        await db.execute(
            "UPDATE user_relationships SET argument_state = ? WHERE user_id = ?",
            (state, user_id)
        )
        await db.commit()

async def increase_affection(user_id: int, amount: int) -> None:
    async with await get_db() as db:
        await db.execute(
            "UPDATE user_relationships SET affection = affection + ? WHERE user_id = ?",
            (amount, user_id)
        )
        await db.commit()

async def update_mood(user_id: int, mood: str) -> None:
    async with await get_db() as db:
        await db.execute(
            "UPDATE user_relationships SET current_mood = ? WHERE user_id = ?",
            (mood, user_id)
        )
        await db.commit()
