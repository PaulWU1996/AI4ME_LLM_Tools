import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from app.config import PROMPTS_DIR, current_model
from app.services import mongo_client, ollama_client

logger = logging.getLogger(__name__)

router = APIRouter()


def _shared_path() -> Path:
    return Path(os.environ.get("SHARED_VOLUME_PATH", "/shared"))


def _max_chars() -> int:
    return int(os.environ.get("MAX_TRANSCRIPT_CHARS", 0))


class ProcessRequest(BaseModel):
    job_id: str
    job_type: Optional[str] = None
    prompts: Optional[str] = None
    transcript: Optional[str] = None
    language: str = "en"
    record_key: Optional[str] = None
    metadata: Optional[dict] = None
    model: Optional[str] = None


class ModelInfo(BaseModel):
    name: str
    size: Optional[int] = None
    modified_at: Optional[str] = None


class ModelsResponse(BaseModel):
    models: list[ModelInfo]


class PullModelRequest(BaseModel):
    model: str


class PullModelResponse(BaseModel):
    model: str
    status: str

class SummaryResponse(BaseModel):
    job_id: str
    title: str
    summary: str
    model: str
    processing_time_ms: int

class TaggingResponse(BaseModel):
    job_id: str
    tags: list[str]
    model: str
    processing_time_ms: int


class CustomResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    job_id: str
    model: str
    processing_time_ms: int


# job_types with a fixed, strictly-validated response schema. Any other folder
# found under system_prompts/ at request time is also usable, with a generic
# CustomResponse and no field-specific validation — this is what makes dropping
# a new system_prompts/{name}/ folder on disk work immediately, with no code change.
KNOWN_RESPONSE_CLASSES: dict[str, type[BaseModel]] = {
    "summary": SummaryResponse,
    "tagging": TaggingResponse,
}


def _available_job_types() -> dict[str, Path]:
    if not PROMPTS_DIR.exists():
        return {}
    return {
        p.name: Path(p.name)
        for p in PROMPTS_DIR.iterdir()
        if p.is_dir() and (p / "transcript.txt").exists() and (p / "output_structure.txt").exists()
    }


def validate_result(job_type: str | None, result: dict) -> dict:
    if job_type == "summary":
        if not isinstance(result.get("title"), str) or not result["title"].strip():
            raise ValueError("Model returned no valid 'title'")
        if not isinstance(result.get("summary"), str) or not result["summary"].strip():
            raise ValueError("Model returned no valid 'summary'")
    elif job_type == "tagging":
        tags = result.get("tags")
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise ValueError("Model returned invalid 'tags'; expected a list of strings")
    return result


@router.post("/process", response_model=SummaryResponse | TaggingResponse | CustomResponse)
async def process_transcript(req: ProcessRequest):
    logger.info(
        "job received | job_id=%s job_type=%s language=%s", req.job_id, req.job_type, req.language
    )

    if req.job_type is None and not req.prompts:
        raise HTTPException(
            status_code=422,
            detail="prompts is required when job_type is None (it is used as the complete system prompt)",
        )

    if req.transcript is not None:
        transcript = req.transcript.strip()
        logger.info("transcript provided in request body | job_id=%s", req.job_id)
    else:
        transcript_path = _shared_path() / req.job_id / "transcript.txt"

        if not transcript_path.exists():
            logger.warning("transcript not found | job_id=%s path=%s", req.job_id, transcript_path)
            raise HTTPException(
                status_code=404,
                detail=f"transcript not found for job {req.job_id}",
            )

        transcript = transcript_path.read_text(encoding="utf-8").strip()

    if not transcript:
        logger.warning("transcript is empty | job_id=%s", req.job_id)
        raise HTTPException(status_code=422, detail="transcript is empty")

    max_chars = _max_chars()
    if max_chars and len(transcript) > max_chars:
        logger.warning(
            "transcript too long | job_id=%s chars=%d limit=%d",
            req.job_id,
            len(transcript),
            max_chars,
        )
        raise HTTPException(
            status_code=413,
            detail=f"transcript exceeds {max_chars} characters",
        )

    logger.info("transcript read | job_id=%s chars=%d", req.job_id, len(transcript))

    if not await ollama_client.is_ready():
        logger.error("ollama unavailable | job_id=%s", req.job_id)
        raise HTTPException(status_code=503, detail="Ollama service unavailable")

    if req.model is not None:
        available = {m["name"] for m in await ollama_client.list_models()}
        if req.model not in available:
            raise HTTPException(
                status_code=422,
                detail=f"model '{req.model}' is not available; pull it first via POST /models/pull",
            )

    logger.info("ollama call start | job_id=%s model=%s", req.job_id, req.model or current_model())

    if req.job_type is None:
        prompts_dir, response_cls = None, CustomResponse
    else:
        available_job_types = _available_job_types()
        if req.job_type not in available_job_types:
            raise HTTPException(
                status_code=422,
                detail=f"job_type '{req.job_type}' not found under system_prompts/; available: {sorted(available_job_types)}",
            )
        prompts_dir = available_job_types[req.job_type]
        response_cls = KNOWN_RESPONSE_CLASSES.get(req.job_type, CustomResponse)

    try:
        result = await ollama_client.generate(
            transcript, req.language, custom_prompt=req.prompts, prompts_dir=prompts_dir, model=req.model
        )
        validated = validate_result(req.job_type, result)
    except ValueError as exc:
        logger.error("ollama parse error | job_id=%s error=%s", req.job_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))

    logger.info("ollama call done | job_id=%s ms=%d", req.job_id, result["processing_time_ms"])

    output = {"job_id": req.job_id, **{k: v for k, v in validated.items() if k != "job_id"}}

    output_path = _shared_path() / req.job_id / "output.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("output written | job_id=%s path=%s", req.job_id, output_path)

    mongo_doc = {
        "key": req.record_key or req.job_id,
        "job_id": req.job_id,
        "record_key": req.record_key,
        "job_type": req.job_type,
        "language": req.language,
        **{k: v for k, v in validated.items() if k != "job_id"},
        "model": result["model"],
        "processing_time_ms": result["processing_time_ms"],
        "metadata": req.metadata,
        "updated_at": datetime.now(timezone.utc),
    }
    if mongo_client.save_result(mongo_doc):
        logger.info("mongo write ok | key=%s job_id=%s", mongo_doc["key"], req.job_id)
    else:
        logger.warning("mongo write skipped/failed | key=%s job_id=%s", mongo_doc["key"], req.job_id)

    response = response_cls(
        job_id=req.job_id,
        **{k: v for k, v in validated.items() if k != "job_id"},
    )
    return response


@router.get("/models", response_model=ModelsResponse)
async def list_models():
    if not await ollama_client.is_ready():
        raise HTTPException(status_code=503, detail="Ollama service unavailable")
    models = await ollama_client.list_models()
    return ModelsResponse(
        models=[
            ModelInfo(name=m["name"], size=m.get("size"), modified_at=m.get("modified_at"))
            for m in models
        ]
    )


@router.post("/models/pull", response_model=PullModelResponse)
async def pull_model(req: PullModelRequest):
    if not await ollama_client.is_ready():
        raise HTTPException(status_code=503, detail="Ollama service unavailable")

    logger.info("model pull start | model=%s", req.model)
    try:
        result = await ollama_client.pull_model(req.model)
    except httpx.HTTPStatusError as exc:
        logger.error("model pull failed | model=%s error=%s", req.model, exc)
        raise HTTPException(status_code=502, detail=f"failed to pull model '{req.model}': {exc}")
    logger.info("model pull done | model=%s", req.model)

    return PullModelResponse(model=req.model, status=result.get("status", "success"))


@router.get("/health")
async def health():
    ready = await ollama_client.is_ready()
    model = current_model()
    mongo_ready = mongo_client.is_ready()
    if not ready:
        raise HTTPException(
            status_code=503,
            detail={"status": "degraded", "ollama_ready": False, "model": model, "mongo_ready": mongo_ready},
        )
    return {"status": "ok", "ollama_ready": True, "model": model, "mongo_ready": mongo_ready}
