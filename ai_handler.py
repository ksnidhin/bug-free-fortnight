import os
import time
import logging
from telegram import Update
from telegram.ext import ContextTypes
from google import genai
from groq import AsyncGroq

logger = logging.getLogger("aibot")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
groq_client = AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# Cooldown tracking: chat_id -> timestamp
last_auto_reply = {}
AUTO_REPLY_COOLDOWN = 60

async def generate_ai_response(prompt: str) -> str:
    """Multi-provider fallback logic"""
    # 1. Try Gemini
    if gemini_client:
        try:
            response = await gemini_client.aio.models.generate_content(
                model='gemini-2.5-flash', 
                contents=prompt,
                config={"system_instruction": "You are a helpful and concise group chat assistant. Keep answers under 3 sentences."}
            )
            return response.text
        except Exception as e:
            logger.error(f"Gemini API error: {e}")
            
    # 2. Fallback to Groq
    if groq_client:
        try:
            completion = await groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": "You are a helpful and concise group chat assistant. Keep answers under 3 sentences."},
                    {"role": "user", "content": prompt}
                ],
            )
            return completion.choices[0].message.content
        except Exception as e:
            logger.error(f"Groq API error: {e}")
            
    return "⚠️ I'm sorry, all AI providers are currently unavailable or experiencing rate limits."

async def cmd_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ai <prompt> command"""
    msg = update.effective_message
    if not msg:
        return
        
    prompt = " ".join(context.args) if context.args else ""
    if not prompt:
        await msg.reply_text("Please provide a prompt: `/ai what is 2+2?`", parse_mode="Markdown")
        return
        
    await msg.reply_chat_action("typing")
    reply = await generate_ai_response(prompt)
    await msg.reply_text(reply)

async def check_auto_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check if we should auto-reply to a question."""
    msg = update.effective_message
    if not msg or not msg.text:
        return
        
    text = msg.text.strip()
    chat_id = msg.chat_id
    
    # Simple question heuristic
    is_question = text.endswith("?") and len(text) > 5 and any(w in text.lower() for w in ["how", "what", "why", "when", "where", "who", "is", "can", "do", "does"])
    
    # Mention heuristic
    is_mention = context.bot.username and f"@{context.bot.username}" in text
    
    # Reply heuristic
    is_reply_to_bot = msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.id == context.bot.id

    if is_mention or is_reply_to_bot or is_question:
        # Check cooldown
        now = time.time()
        if chat_id in last_auto_reply and now - last_auto_reply[chat_id] < AUTO_REPLY_COOLDOWN:
            # Don't auto-reply to random questions if on cooldown.
            # But if we are explicitly mentioned or replied to, we bypass cooldown!
            if not (is_mention or is_reply_to_bot):
                return
                
        if not (is_mention or is_reply_to_bot):
            last_auto_reply[chat_id] = now
            
        await msg.reply_chat_action("typing")
        
        # Clean prompt
        prompt = text
        if context.bot.username:
            prompt = prompt.replace(f"@{context.bot.username}", "").strip()
            
        reply = await generate_ai_response(prompt)
        await msg.reply_text(reply)
