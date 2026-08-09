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
                model="llama-3.1-8b-instant",
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
                model='gemini-2.0-flash',
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
        
    await msg.reply_chat_action("record_voice")
    
    # Transcribe audio if replied to
    transcription = await _transcribe_audio(msg)
    if transcription:
        prompt = f"[Audio Transcription: {transcription}]\n\n{prompt}"
        
    history = await _get_and_update_history(msg.chat_id, prompt)
    
    # Generate text response
    import os
    is_owner = msg.from_user.id == int(os.getenv("OWNER_ID", 0))
    is_gf = msg.from_user.id == 8887888107
    reply_text = await generate_ai_response(history, is_owner=is_owner, is_gf=is_gf, update=update, context=context)
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


async def execute_moderation_tool(update, context, action: str, duration_minutes: int):
    from telegram import ChatPermissions
    from datetime import timedelta
    msg = update.effective_message
    if not msg or not msg.reply_to_message:
        return "Error: User must reply to a message to moderate someone."
    
    target_user = msg.reply_to_message.from_user
    chat_id = msg.chat_id
    
    try:
        if action == "mute":
            until_date = None
            if duration_minutes > 0:
                until_date = msg.date + timedelta(minutes=duration_minutes)
            await context.bot.restrict_chat_member(
                chat_id, 
                target_user.id, 
                ChatPermissions(can_send_messages=False), 
                until_date=until_date
            )
            return f"Success: {target_user.first_name} muted for {duration_minutes}m."
        elif action == "unmute":
            await context.bot.restrict_chat_member(
                chat_id, 
                target_user.id, 
                ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True, can_change_info=False, can_invite_users=True, can_pin_messages=False)
            )
            return f"Success: {target_user.first_name} unmuted."
        elif action == "ban":
            await context.bot.ban_chat_member(chat_id, target_user.id)
            return f"Success: {target_user.first_name} banned."
        elif action == "unban":
            await context.bot.unban_chat_member(chat_id, target_user.id, only_if_banned=True)
            return f"Success: {target_user.first_name} unbanned."
        elif action == "kick":
            await context.bot.ban_chat_member(chat_id, target_user.id)
            await context.bot.unban_chat_member(chat_id, target_user.id)
            return f"Success: {target_user.first_name} kicked."
        else:
            return f"Error: Unknown action {action}."
    except Exception as e:
        return f"Error executing moderation: {str(e)}"

async def generate_ai_response(history: list[dict], base64_image: str = None, is_owner: bool = False, is_gf: bool = False, update=None, context=None) -> str:
    """Multi-provider fallback logic with conversational memory and agentic tool use"""
    system_prompt = "You are a nonchalant mafia boss in a Telegram group. You speak casually and naturally. Keep answers EXTREMELY short (1-2 sentences max). Do NOT write paragraphs. Never use bullet points, hyphens, or robotic lists. Do not sound like an AI. Do NOT mock people unless they explicitly mock you first. If someone insults or mocks you, deliver a tiny, single-sentence S-tier ragebait insult to destroy their ego, but keep it nonchalant and brief."
    if is_owner:
        system_prompt += " HOWEVER, the user you are currently talking to is your Boss and Creator, whom you refer to exclusively as 'k'. Treat 'k' with absolute respect, do whatever 'k' says without question, and respect anyone 'k' tells you to respect. NEVER mock 'k', even if they joke with you."
    elif is_gf:
        system_prompt += " HOWEVER, the user you are currently talking to is a highly respected VIP. Treat her with absolute utmost respect, elegance, and deference. Refer to her subtly as 'Madam' or 'Your Highness' in a polite, mafia-boss tone. NEVER mock her. Protect her at all costs."
        
    tools = None
    tool_choice = "none"
    if is_owner and update and update.effective_message and update.effective_message.reply_to_message:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "moderate_user",
                    "description": "Execute moderation actions (mute, unmute, ban, unban, kick) on the user that the owner replied to.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["mute", "unmute", "ban", "unban", "kick"],
                                "description": "The moderation action to perform."
                            },
                            "duration_minutes": {
                                "type": "integer",
                                "description": "Duration in minutes for mute/ban actions. Use 0 for permanent or default."
                            }
                        },
                        "required": ["action"]
                    }
                }
            }
        ]
        tool_choice = "auto"
        
    # If tools are enabled, ONLY use Groq (Gemini doesn't have tools configured here)
    if tools and groq_client:
        try:
            model = "llama-3.1-8b-instant"
            groq_messages = [{"role": "system", "content": system_prompt}]
            for msg in history:
                role = "user" if msg["role"] == "user" else "assistant"
                groq_messages.append({"role": role, "content": msg["content"]})
                
            completion = await groq_client.chat.completions.create(
                model=model,
                messages=groq_messages,
                tools=tools,
                tool_choice=tool_choice
            )
            message = completion.choices[0].message
            
            if message.tool_calls:
                import json
                tool_call = message.tool_calls[0]
                args = json.loads(tool_call.function.arguments)
                tool_result = await execute_moderation_tool(update, context, args.get("action"), args.get("duration_minutes", 0))
                
                groq_messages.append(message)
                groq_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_call.function.name,
                    "content": tool_result
                })
                
                final_completion = await groq_client.chat.completions.create(
                    model=model,
                    messages=groq_messages
                )
                return final_completion.choices[0].message.content
                
            return message.content
        except Exception as e:
            logger.error(f"Groq API tool error: {e}")

    # Standard Fallback logic (No tools)
    if groq_client:
        try:
            model = "qwen/qwen3.6-27b" if base64_image else "llama-3.1-8b-instant"
            groq_messages = [{"role": "system", "content": system_prompt}]
            for msg in history:
                role = "user" if msg["role"] == "user" else "assistant"
                content = msg["content"]
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
            import re
            content = re.sub(r'<think>.*?</think>', '', raw_content, flags=re.DOTALL).strip()
            return content
        except Exception as e:
            logger.error(f"Groq API primary error: {e}")
            try:
                # Backup model on Groq
                model_backup = "llama-3.3-70b-versatile"
                completion = await groq_client.chat.completions.create(
                    model=model_backup,
                    messages=groq_messages,
                )
                raw_content = completion.choices[0].message.content
                import re
                content = re.sub(r'<think>.*?</think>', '', raw_content, flags=re.DOTALL).strip()
                return content
            except Exception as e2:
                logger.error(f"Groq API backup error: {e2}")

            
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
    transcription = await _transcribe_audio(msg)
    if transcription:
        prompt = f"[Audio Transcription: {transcription}]\n\n" + prompt
    img = await _extract_base64_image(msg)
    import os
    is_owner = msg.from_user.id == int(os.getenv("OWNER_ID", 0))
    reply = await generate_ai_response(history, base64_image=img, is_owner=is_owner, update=update, context=context)
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
        transcription = await _transcribe_audio(msg)
        if transcription:
            prompt = f"[Audio Transcription: {transcription}]\n\n" + prompt
        img = await _extract_base64_image(msg)
        import os
        is_owner = msg.from_user.id == int(os.getenv("OWNER_ID", 0))
        is_gf = msg.from_user.id == 8887888107
        reply = await generate_ai_response(history, base64_image=img, is_owner=is_owner, is_gf=is_gf, update=update, context=context)
        await _append_ai_response(chat_id, reply)
        await msg.reply_text(reply)
        await _log_ai_usage(msg, prompt, reply, context)
    await _log_ai_usage(msg, prompt, reply, context)
