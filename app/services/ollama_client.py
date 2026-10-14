import json
import logging
import time
from pathlib import Path

import httpx

from app.config import PROMPTS_DIR, current_model

OLLAMA_BASE_URL = "http://localhost:11434"

logger = logging.getLogger(__name__)

async def is_ready() -> bool:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            return r.status_code == 200
    except Exception:
        logger.exception("Error occurred while checking Ollama readiness")
        return False


async def list_models() -> list[dict]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
        response.raise_for_status()
    return response.json().get("models", [])


async def pull_model(name: str) -> dict:
    payload = {"model": name, "stream": False}
    async with httpx.AsyncClient(timeout=None) as client:
        response = await client.post(f"{OLLAMA_BASE_URL}/api/pull", json=payload)
        response.raise_for_status()
    return response.json()

def _build_system_prompt(language: str, custom_instructions: str | None, prompts_dir: Path | None) -> str:
    if prompts_dir is None:
        if not custom_instructions:
            raise ValueError("custom_instructions (prompts) is required when prompts_dir is None")
        # No predefined prompt folder: custom_instructions is the complete system prompt as-is.
        return custom_instructions.format_map({"language": language})

    prompts_dir = PROMPTS_DIR / prompts_dir
    default_requirements = (prompts_dir / "instructions.txt").read_text()
    output_structure = (prompts_dir / "output_structure.txt").read_text()

    requirements = default_requirements
    if custom_instructions:
        requirements = f"{default_requirements}\n\n{custom_instructions}"

    rendered_requirements = requirements.format_map({"language": language})
    return rendered_requirements + "\n" + output_structure


async def generate(transcript: str, language: str = "en", custom_prompt: str | None = None, prompts_dir: Path | None = Path("summary"), model: str | None = None) -> dict:
    model = model or current_model()
    system_prompt = _build_system_prompt(language, custom_prompt, prompts_dir)

    payload = {
        "model": model,
        "system": system_prompt,
        "prompt": transcript,
        "format": "json",
        "stream": False,
        "options": {
            "temperature": 0.3,
            "num_predict": 512,
        },
    }

    logger.info(f"Generating with system prompt:\n {system_prompt}")

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
