import base64
import os
import time
import logging
from telegram import Update
from telegram.ext import ContextTypes
from google import genai
from groq import AsyncGroq
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("aibot")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
groq_client = AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# Cooldown tracking: chat_id -> timestamp
last_auto_reply = {}
AUTO_REPLY_COOLDOWN = 60

# Conversational memory: chat_id -> list of message dicts
chat_histories = {}
MAX_HISTORY = 10

async def _get_and_update_history(chat_id: int, prompt: str) -> list[dict]:
    if chat_id not in chat_histories:
        chat_histories[chat_id] = []
    
    chat_histories[chat_id].append({"role": "user", "content": prompt})
    
    if len(chat_histories[chat_id]) > MAX_HISTORY:
        chat_histories[chat_id] = chat_histories[chat_id][-MAX_HISTORY:]
        
    return chat_histories[chat_id]

async def _append_ai_response(chat_id: int, response: str):
    if chat_id in chat_histories:
        chat_histories[chat_id].append({"role": "assistant", "content": response})


async def is_disrespectful(text: str) -> bool:
    """Use AI to determine if a message is highly disrespectful or toxic."""
    prompt = f"Analyze the following message. Is it highly disrespectful, toxic, or directly mocking? Reply ONLY with the exact word YES or the exact word NO. Do not explain. Message: {text}"
    
    if groq_client:
        try:
            completion = await groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_completion_tokens=5,
            )
            response = completion.choices[0].message.content.strip().upper()
            return "YES" in response
        except Exception as e:
            logger.error(f"Groq Moderation error: {e}")
            
    if gemini_client:
        try:
            response = await gemini_client.aio.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
            )
            return "YES" in response.text.strip().upper()
        except Exception as e:
            logger.error(f"Gemini Moderation error: {e}")
            
    return False


async def _extract_base64_image(msg) -> str | None:
    try:
        if msg.photo:
            file = await msg.photo[-1].get_file()
            byte_array = await file.download_as_bytearray()
            return base64.b64encode(byte_array).decode('utf-8')
        elif msg.reply_to_message and msg.reply_to_message.photo:
            file = await msg.reply_to_message.photo[-1].get_file()
            byte_array = await file.download_as_bytearray()
            return base64.b64encode(byte_array).decode('utf-8')
    except Exception:
        pass
    return None

async def generate_ai_response(history: list[dict], base64_image: str = None) -> str:
    """Multi-provider fallback logic with conversational memory"""
    # 1. Try Gemini
    if gemini_client:
        try:
            # Convert history to Gemini format
            gemini_contents = []
            for msg in history:
                role = "user" if msg["role"] == "user" else "model"
                parts = [{"text": msg["content"]}]
                if base64_image and msg == history[-1] and role == "user":
                    parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64_image}})
                    
                gemini_contents.append({"role": role, "parts": parts})
                
            response = await gemini_client.aio.models.generate_content(
                model='gemini-2.5-flash', 
                contents=gemini_contents,
                config={"system_instruction": "You are a helpful and concise group chat assistant. Keep answers under 3 sentences."}
            )
            return response.text
        except Exception as e:
            logger.error(f"Gemini API error: {e}")
            
    # 2. Fallback to Groq
    if groq_client:
        try:
            model = "llama-3.3-70b-versatile"
            groq_messages = [{"role": "system", "content": "You are a helpful and concise group chat assistant. Keep answers under 3 sentences."}]
            for msg in history:
                role = "user" if msg["role"] == "user" else "assistant"
                # Strip out any images if passed, Groq currently has no vision models available
                groq_messages.append({"role": role, "content": msg["content"]})
                
            completion = await groq_client.chat.completions.create(
                model=model,
                messages=groq_messages,
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
    history = await _get_and_update_history(msg.chat_id, prompt)
    img = await _extract_base64_image(msg)
    reply = await generate_ai_response(history, base64_image=img)
    await _append_ai_response(msg.chat_id, reply)
    await msg.reply_text(reply)

async def check_auto_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check if we should auto-reply to a question."""
    msg = update.effective_message
    if not msg or not (msg.text or msg.caption):
        return
        
    text = msg.text.strip() if msg.text else (msg.caption.strip() if msg.caption else "")
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
            
        history = await _get_and_update_history(chat_id, prompt)
        img = await _extract_base64_image(msg)
        reply = await generate_ai_response(history, base64_image=img)
        await _append_ai_response(chat_id, reply)
        await msg.reply_text(reply)
