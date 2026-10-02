import os
from typing import Optional
import google.generativeai as genai
from .base import BaseProvider

class GeminiProvider(BaseProvider):
    def __init__(self):
        genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
        self.text_model_name = os.environ.get("GEMINI_TEXT_MODEL", "gemini-1.5-flash")
        self.vision_model_name = os.environ.get("GEMINI_VISION_MODEL", "gemini-1.5-flash")
        self.text_model = genai.GenerativeModel(self.text_model_name)
        self.vision_model = genai.GenerativeModel(self.vision_model_name)

    async def generate(self, system_prompt: str, messages: list, image_bytes: Optional[bytes] = None) -> str:
        model = self.vision_model if image_bytes else self.text_model
        
        contents = []
        for i, msg in enumerate(messages):
            role = "user" if msg.get("role") in ["user", "system"] else "model"
            parts = [msg.get("content", "")]
            if image_bytes and i == len(messages) - 1:
                parts.append({"mime_type": "image/jpeg", "data": image_bytes})
            contents.append({"role": role, "parts": parts})
            
        response = await model.generate_content_async(
            contents,
            system_instruction=system_prompt,
        )
        return response.text
