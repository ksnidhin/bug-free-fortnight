import os
import base64
from typing import Optional
from groq import AsyncGroq
from .base import BaseProvider

class GroqProvider(BaseProvider):
    def __init__(self):
        self.client = AsyncGroq(api_key=os.environ.get("GROQ_API_KEY"))
        self.text_model = os.environ.get("GROQ_TEXT_MODEL", "llama-3.1-70b-versatile")
        self.vision_model = os.environ.get("GROQ_VISION_MODEL", "llama-3.2-90b-vision-preview")

    async def generate(self, system_prompt: str, messages: list, image_bytes: Optional[bytes] = None) -> str:
        model = self.vision_model if image_bytes else self.text_model
        
        formatted_messages = [{"role": "system", "content": system_prompt}]
        
        for i, msg in enumerate(messages):
            if image_bytes and i == len(messages) - 1:
                base64_image = base64.b64encode(image_bytes).decode("utf-8")
                content = [
                    {"type": "text", "text": msg.get("content", "")},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{base64_image}",
                        },
                    },
                ]
                formatted_messages.append({"role": msg.get("role", "user"), "content": content})
            else:
                formatted_messages.append({"role": msg.get("role", "user"), "content": msg.get("content", "")})
                
        response = await self.client.chat.completions.create(
            model=model,
            messages=formatted_messages,
        )
        return response.choices[0].message.content
