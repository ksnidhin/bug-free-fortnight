import os
import base64
from typing import Optional
from openai import AsyncOpenAI
from .base import BaseProvider

class LLM7Provider(BaseProvider):
    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=os.environ.get("LLM7_API_KEY"),
            base_url="https://api.llm7.io/v1"
        )
        self.text_model = os.environ.get("LLM7_TEXT_MODEL", "gpt-4o-mini")

    async def generate(self, system_prompt: str, messages: list, image_bytes: Optional[bytes] = None) -> str:
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
            model=self.text_model,
            messages=formatted_messages,
        )
        return response.choices[0].message.content
