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



async def enforce_moderation(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """Check text for toxicity and mute if necessary. Returns True if muted."""
    if not await is_disrespectful(text):
        return False
        
    msg = update.effective_message
    user = msg.from_user
    user_id = user.id if user else 0
    
    try:
        member = await msg.chat.get_member(user_id)
        if member.status in ["administrator", "creator"]:
            return False
    except Exception:
        pass
        
    from datetime import datetime, timedelta, timezone
    from telegram import ChatPermissions
    
    try:
        await msg.delete()
    except Exception:
        pass
        
    try:
        await context.bot.restrict_chat_member(
            chat_id=msg.chat_id,
            user_id=user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=datetime.now(timezone.utc) + timedelta(seconds=60),
        )
        await context.bot.send_message(
            chat_id=msg.chat_id,
            text=f"🚫 {user.mention_html() if user else 'User'} has been muted for 60s for toxic/disrespectful behavior.",
            parse_mode="HTML"
        )
    except Exception:
        pass
        
    return True

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



async def _transcribe_audio(msg) -> str | None:
    try:
        # Check if the message or reply has audio/voice
        target = msg.reply_to_message if msg.reply_to_message else msg
        if getattr(target, 'voice', None):
            file = await target.voice.get_file()
            byte_array = await file.download_as_bytearray()
            filename = "audio.ogg"
        elif getattr(target, 'audio', None):
            file = await target.audio.get_file()
            byte_array = await file.download_as_bytearray()
            filename = "audio.mp3"
        else:
            return None
            
        if groq_client:
            transcription = await groq_client.audio.transcriptions.create(
                file=(filename, bytes(byte_array)),
                model="whisper-large-v3-turbo",
            )
            return transcription.text
    except Exception as e:
        logger.error(f"STT error: {e}")
    return None


async def cmd_speak(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/speak <prompt> command"""
    msg = update.effective_message
    if not msg:
        return
        
    prompt = " ".join(context.args) if context.args else ""
    if not prompt:
        await msg.reply_text("Please provide a prompt: `/speak tell me a joke`", parse_mode="Markdown")
        return
        
    if await enforce_moderation(update, context, prompt):
        return
        
    await msg.reply_chat_action("record_voice")
    
    # Transcribe audio if replied to
    transcription = await _transcribe_audio(msg)
    if transcription:
        prompt = f"[Audio Transcription: {transcription}]\n\n{prompt}"
        
    history = await _get_and_update_history(msg.chat_id, prompt)
    
    # Generate text response
    import os
    is_owner = msg.from_user.id == int(os.getenv("OWNER_ID", 0))
    reply_text = await generate_ai_response(history, is_owner=is_owner)
    await _append_ai_response(msg.chat_id, reply_text)
    
    # Convert to speech
    if not groq_client:
        await msg.reply_text("TTS requires Groq API which is currently unavailable.")
        return
        
    try:
        import httpx
        import io
        import os
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/audio/speech",
                headers={"Authorization": f"Bearer {os.environ.get('GROQ_API_KEY')}"},
                json={
                    "model": "canopylabs/orpheus-v1-english",
                    "input": reply_text,
                    "voice": "diana",
                    "response_format": "wav"
                },
                timeout=30.0
            )
            if resp.status_code == 200:
                audio_stream = io.BytesIO(resp.content)
                audio_stream.name = "voice.wav"
                await msg.reply_voice(voice=audio_stream)
                await _log_ai_usage(msg, prompt, f"[Voice Note Generated]\n{reply_text}", context)
            else:
                raise Exception(f"API Error {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.error(f"TTS error: {e}")
        await msg.reply_text(f"Voice generation failed: {e}\n\nHere is the text instead:\n{reply_text}")


async def _log_ai_usage(msg, prompt, reply, context):
    import os
    from telegram.constants import ParseMode
    log_chat_id = os.getenv("LOG_CHAT_ID")
    if not log_chat_id:
        return
    try:
        log_chat_id = int(log_chat_id)
        user = msg.from_user
        username = user.username or '?'
        clean_reply = reply.replace('<', '&lt;').replace('>', '&gt;')
        clean_prompt = prompt.replace('<', '&lt;').replace('>', '&gt;')
        log_text = f"🤖 <b>AI Usage Log</b>\n👤 User: <code>{user.id}</code> (@{username})\n💬 Prompt: {clean_prompt}\n📝 Answer: {clean_reply}"
        if len(log_text) > 4000:
            log_text = log_text[:4000] + "... (truncated)"
        await context.bot.send_message(chat_id=log_chat_id, text=log_text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Failed to log AI usage: {e}")

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

async def generate_ai_response(history: list[dict], base64_image: str = None, is_owner: bool = False) -> str:
    """Multi-provider fallback logic with conversational memory"""
    system_prompt = "You are a nonchalant mafia boss in a Telegram group. You speak casually, naturally, and with quiet authority. You never use bullet points, hyphens, or robotic lists. Keep it short, human, slightly intimidating but relaxed. Do not sound like an AI. If someone makes a joke about you, mocks you, or plays around, instantly deliver an S-tier ragebait insult to mock them based on what they just said. Be ruthless, condescending, and deeply insulting in a calm, nonchalant way. Destroy their ego effortlessly without breaking character."
    if is_owner:
        system_prompt += " HOWEVER, the user you are currently talking to is your Boss and Creator. Treat them with absolute respect and loyalty. NEVER mock them, even if they joke with you."
        
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
                config={"system_instruction": system_prompt}
            )
            return response.text
        except Exception as e:
            logger.error(f"Gemini API error: {e}")
            
    # 2. Fallback to Groq
    if groq_client:
        try:
            model = "qwen/qwen3.6-27b" if base64_image else "llama-3.3-70b-versatile"
            groq_messages = [{"role": "system", "content": system_prompt}]
            for msg in history:
                role = "user" if msg["role"] == "user" else "assistant"
                content = msg["content"]
                
                # Attach image to the most recent user prompt
                if base64_image and msg == history[-1] and role == "user":
                    content = [
                        {"type": "text", "text": msg["content"]},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                    ]
                    
                groq_messages.append({"role": role, "content": content})
                
            completion = await groq_client.chat.completions.create(
                model=model,
                messages=groq_messages,
            )
            raw_content = completion.choices[0].message.content
            # Remove <think>...</think> blocks from reasoning models like Qwen
            import re
            content = re.sub(r'<think>.*?</think>', '', raw_content, flags=re.DOTALL).strip()
            return content
        except Exception as e:
            logger.error(f"Groq API error: {e}")
            
    return "⚠️ I'm sorry, all AI providers are currently unavailable or experiencing rate limits."

async def cmd_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ai <prompt> command"""
    msg = update.effective_message
    if not msg:
        return
        
    prompt = " ".join(context.args) if context.args else ""
    if await enforce_moderation(update, context, prompt):
        return
    if not prompt:
        await msg.reply_text("Please provide a prompt: `/ai what is 2+2?`", parse_mode="Markdown")
        return
        
    await msg.reply_chat_action("typing")
    history = await _get_and_update_history(msg.chat_id, prompt)
    transcription = await _transcribe_audio(msg)
    if transcription:
        prompt = f"[Audio Transcription: {transcription}]\n\n" + prompt
    img = await _extract_base64_image(msg)
    import os
    is_owner = msg.from_user.id == int(os.getenv("OWNER_ID", 0))
    reply = await generate_ai_response(history, base64_image=img, is_owner=is_owner)
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
        if await enforce_moderation(update, context, text):
            return
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
        transcription = await _transcribe_audio(msg)
        if transcription:
            prompt = f"[Audio Transcription: {transcription}]\n\n" + prompt
        img = await _extract_base64_image(msg)
        import os
        is_owner = msg.from_user.id == int(os.getenv("OWNER_ID", 0))
        reply = await generate_ai_response(history, base64_image=img, is_owner=is_owner)
        await _append_ai_response(chat_id, reply)
        await msg.reply_text(reply)
        await _log_ai_usage(msg, prompt, reply, context)
    await _log_ai_usage(msg, prompt, reply, context)
