import json
import logging
import time
from pathlib import Path
import logging

import httpx

from app.config import PROMPTS_DIR, current_model

OLLAMA_BASE_URL = "http://localhost:11434"

<<<<<<< HEAD
=======
_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
_DEFAULT_REQUIREMENTS = (_PROMPTS_DIR / "transcript.txt").read_text()
_OUTPUT_STRUCTURE = (_PROMPTS_DIR / "output_structure.txt").read_text()

>>>>>>> 694131611a093862911d661f23ce1d68c2216695
logger = logging.getLogger(__name__)

async def is_ready() -> bool:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            return r.status_code == 200
    except Exception:
        logger.exception("Error occurred while checking Ollama readiness")
        return False

def _build_prompt(transcript: str, language: str, custom_requirements: str | None, prompts_dir: Path) -> str:
    prompts_dir = PROMPTS_DIR / prompts_dir
    default_requirements = (prompts_dir / "transcript.txt").read_text()
    output_structure = (prompts_dir / "output_structure.txt").read_text()
    
    requirements = default_requirements
    if custom_requirements:
        requirements = f"{default_requirements}\n\n{custom_requirements}"

<<<<<<< HEAD
=======
def _build_prompt(transcript: str, language: str, custom_requirements: str | None) -> str:
    requirements = _DEFAULT_REQUIREMENTS
    if custom_requirements:
        requirements = f"{_DEFAULT_REQUIREMENTS}\n\n{custom_requirements}"

    # format_map on requirements — keeps {language} slot; transcript is concatenated
    # directly to avoid KeyError if transcript text contains { } characters
>>>>>>> 694131611a093862911d661f23ce1d68c2216695
    rendered_requirements = requirements.format_map({"language": language})
    return (
        "<system>\n"
        + rendered_requirements
        + "\n"
        + output_structure
        + "</system>\n\n"
        "<user>\n"
        "Transcript:\n"
        "---\n"
        + transcript
        + "\n---\n\n"
        "Produce the JSON output now.\n"
        "</user>"
    )


async def generate(transcript: str, language: str = "en", custom_prompt: str | None = None, prompts_dir: Path = Path("summary")) -> dict:
    model = current_model()
    prompt = _build_prompt(transcript, language, custom_prompt, prompts_dir)

    payload = {
        "model": model,
        "prompt": prompt,
        "format": "json",
        "stream": False,
        "options": {
            "temperature": 0.3,
            "num_predict": 512,
        },
    }
    
    logger.info(f"Generating with prompt:\n {prompt}")

    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(f"{OLLAMA_BASE_URL}/api/generate", json=payload)
        response.raise_for_status()

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    raw = response.json()["response"]

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model returned malformed JSON: {exc}. Raw: {raw!r}") from exc

    return {
        **data,
        "model": model,
        "processing_time_ms": elapsed_ms,
    }
