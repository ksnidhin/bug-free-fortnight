from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional

class BaseProvider(ABC):
    @abstractmethod
    async def generate(self, system_prompt: str, messages: list, image_bytes: Optional[bytes] = None) -> str:
        """
        Generate a response given a system prompt, message history, and optional image bytes.
        """
        pass
