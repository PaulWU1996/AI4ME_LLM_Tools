import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/app/config"))
MODEL_CONFIG_PATH = CONFIG_DIR / "model.txt"

PROMPTS_DIR = Path(os.environ.get("PROMPTS_DIR", "/app/prompts"))


def current_model() -> str:
    """Active Ollama model tag, re-read on every call so a mounted
    config/model.txt can be edited to switch models without a restart."""
    model = MODEL_CONFIG_PATH.read_text(encoding="utf-8").strip()
    if not model:
        raise RuntimeError(f"{MODEL_CONFIG_PATH} is empty — set it to a model tag")
    return model
