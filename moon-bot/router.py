import logging
from typing import Optional
from providers.groq_provider import GroqProvider
from providers.gemini_provider import GeminiProvider
from providers.llm7_provider import LLM7Provider

logger = logging.getLogger(__name__)

groq_provider = GroqProvider()
gemini_provider = GeminiProvider()
llm7_provider = LLM7Provider()

async def generate_response(system_prompt: str, messages: list, image_bytes: Optional[bytes] = None, force_model: Optional[str] = None) -> str:
    """
    Generate response trying Groq first, then Gemini, then LLM7.
    """
    if force_model == "groq":
        return await groq_provider.generate(system_prompt, messages, image_bytes)
    elif force_model == "gemini":
        return await gemini_provider.generate(system_prompt, messages, image_bytes)
    elif force_model == "llm7":
        return await llm7_provider.generate(system_prompt, messages, image_bytes)

    try:
        logger.info("Attempting Groq generation...")
        return await groq_provider.generate(system_prompt, messages, image_bytes)
    except Exception as e:
        logger.error(f"Groq failed: {e}")
        
    try:
        logger.info("Attempting Gemini fallback...")
        return await gemini_provider.generate(system_prompt, messages, image_bytes)
    except Exception as e:
        logger.error(f"Gemini failed: {e}")
        
    try:
        logger.info("Attempting LLM7 fallback...")
        return await llm7_provider.generate(system_prompt, messages, image_bytes)
    except Exception as e:
        logger.error(f"LLM7 failed: {e}")
        
    raise RuntimeError("All providers failed to generate a response.")
