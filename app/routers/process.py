import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import current_model
from app.services import mongo_client, ollama_client

logger = logging.getLogger(__name__)

router = APIRouter()


def _shared_path() -> Path:
    return Path(os.environ.get("SHARED_VOLUME_PATH", "/shared"))


def _max_chars() -> int:
    return int(os.environ.get("MAX_TRANSCRIPT_CHARS", 0))


class ProcessRequest(BaseModel):
    job_id: str
    job_type: Literal["summary", "tagging"] = "summary"
    prompts: Optional[str] = None
    transcript: Optional[str] = None
    language: str = "en"
    record_key: Optional[str] = None
    metadata: Optional[dict] = None

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


JOB_TYPES: dict[str, tuple[Path, type[SummaryResponse] | type[TaggingResponse]]] = {
    "summary": (Path("summary"), SummaryResponse),
    "tagging": (Path("tagging"), TaggingResponse),
}


def validate_result(job_type: str, result: dict) -> dict:
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


@router.post("/process", response_model=SummaryResponse | TaggingResponse)
async def process_transcript(req: ProcessRequest):
    logger.info(
        "job received | job_id=%s job_type=%s language=%s", req.job_id, req.job_type, req.language
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

    logger.info("ollama call start | job_id=%s", req.job_id)

    prompts_dir, response_cls = JOB_TYPES[req.job_type]

    try:
        result = await ollama_client.generate(transcript, req.language, custom_prompt=req.prompts, prompts_dir=prompts_dir)
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
