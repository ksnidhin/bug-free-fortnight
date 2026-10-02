
import os
import asyncio
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

from db.database import init_db
from db.relationships import get_or_create_user, get_user_relationship
from personality import generate_system_prompt
from router import generate_response

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.text:
        return
        
    text = msg.text.lower()
    user = msg.from_user
    
    is_reply_to_moon = msg.reply_to_message and msg.reply_to_message.from_user.id == context.bot.id
    is_mentioned = "moon" in text or (context.bot.username and context.bot.username.lower() in text)
    
    if msg.chat.type in ["group", "supergroup"] and not is_reply_to_moon and not is_mentioned:
        return
        
    await context.bot.send_chat_action(chat_id=msg.chat_id, action="typing")
    
    # DB: Ensure user exists and get relationship
    user_id = await get_or_create_user(user.id, user.first_name or user.username)
    relationship_state = await get_user_relationship(user_id)
    
    # Personality: Build Context
    system_prompt = generate_system_prompt(user.first_name or "there", relationship_state, [])
    
    # Formulate messages for router
    messages = [{"role": "user", "content": msg.text}]
    
    try:
        response_text = await generate_response(system_prompt=system_prompt, messages=messages)
        await msg.reply_text(response_text)
    except Exception as e:
        logger.error(f"Error generating response: {e}")
        await msg.reply_text("ugh my brain just crashed 😭 give me a sec")


async def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("No TELEGRAM_BOT_TOKEN found in .env")
        return
        
    await init_db()
    logger.info("Database initialized.")
        
    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    logger.info("Moon is starting up...")
    await app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(main())

