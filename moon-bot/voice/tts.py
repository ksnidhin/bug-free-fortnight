import edge_tts
import uuid
import os

async def generate_voice(text: str) -> str:
    """Generates an OGG voice file from text using edge-tts. Returns the filepath."""
    voice = "en-IN-NeerjaNeural"
    filename = f"data/temp_{uuid.uuid4().hex}.ogg"
    
    # edge-tts generates webm/ogg when output format is specified, but by default it's fine.
    # We will let edge_tts handle the default audio format and return the path.
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(filename)
    
    return filename
