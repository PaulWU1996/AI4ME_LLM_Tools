import logging
import os
from datetime import datetime, timezone
from typing import Optional

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.errors import PyMongoError

logger = logging.getLogger(__name__)

MONGO_HOST = os.environ.get("MONGO_HOST")
MONGO_PORT = os.environ.get("MONGO_PORT", "27017")
MONGO_MACHINE_USER = os.environ.get("MONGO_MACHINE_USER")
MONGO_MACHINE_PASSWORD = os.environ.get("MONGO_MACHINE_PASSWORD")
MONGO_DATABASE = os.environ.get("MONGO_DATABASE", "ai4me_llm_tools")
MONGO_COLLECTION = os.environ.get("MONGO_COLLECTION", "scene_summary")
MONGO_AUTH_SOURCE = os.environ.get("MONGO_AUTH_SOURCE", "admin")

_mongo_client: Optional[MongoClient] = None


def _get_collection() -> Optional[Collection]:
    """Lazily connects. Returns None if MONGO_HOST is unset, so callers
    degrade gracefully when the feature isn't configured."""
    global _mongo_client
    if not MONGO_HOST:
        return None
    if _mongo_client is None:
        if MONGO_MACHINE_USER:
            uri = (
                f"mongodb://{MONGO_MACHINE_USER}:{MONGO_MACHINE_PASSWORD}"
                f"@{MONGO_HOST}:{MONGO_PORT}/{MONGO_DATABASE}?authSource={MONGO_AUTH_SOURCE}"
            )
        else:
            uri = f"mongodb://{MONGO_HOST}:{MONGO_PORT}/{MONGO_DATABASE}"
        _mongo_client = MongoClient(uri, serverSelectionTimeoutMS=1500)
    return _mongo_client[MONGO_DATABASE][MONGO_COLLECTION]


def save_result(document: dict) -> bool:
    """Upserts `document` by its "key" field. Returns True on success,
    False if Mongo is unconfigured or unreachable — never raises."""
    collection = _get_collection()
    if collection is None:
        return False
    try:
        collection.update_one(
            {"key": document["key"]},
            {"$set": document, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        return True
    except PyMongoError as exc:
        logger.warning(
            "mongo write failed | key=%s job_id=%s error=%s",
            document.get("key"), document.get("job_id"), exc,
        )
        return False


def is_ready() -> bool:
    """For /health. True if Mongo is unconfigured (feature off, not
    degraded) OR reachable; False only if configured-but-unreachable."""
    collection = _get_collection()
    if collection is None:
        return True
    try:
        collection.database.client.admin.command("ping")
        return True
    except PyMongoError:
        return False
