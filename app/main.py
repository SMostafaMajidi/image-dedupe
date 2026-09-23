"""FastAPI entrypoint: /embed and /dedupe."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from app.db import VectorDB
from app.dedupe import dedupe_post_uids
from app.embedding import InvalidImageError, get_embedder
from app.schemas import DedupeRequest, DedupeResponse, EmbedResponse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("app")

db = VectorDB()
embedder = get_embedder()


def similarity_threshold() -> float:
    return float(os.getenv("SIMILARITY_THRESHOLD", "0.90"))


def _is_image_content_type(content_type: str | None) -> bool:
    if not content_type:
        return False
    main = content_type.split(";", 1)[0].strip().lower()
    return main.startswith("image/")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    db.ensure_collection()
    embedder.load()
    log.info("startup complete threshold=%.3f", similarity_threshold())
    yield


app = FastAPI(
    title="image-dedupe",
    description="CLIP + Qdrant near-duplicate image detection",
    version="0.3.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "collection": db.collection_name,
        "threshold": similarity_threshold(),
    }


@app.post("/embed", response_model=EmbedResponse)
async def embed(
    post_uid: str = Form(..., min_length=1),
    image: UploadFile = File(...),
) -> EmbedResponse:
    post_uid = post_uid.strip()
    if not post_uid:
        raise HTTPException(status_code=422, detail="post_uid must not be empty")

    if not _is_image_content_type(image.content_type):
        raise HTTPException(
            status_code=400,
            detail=f"file content-type must be image/*, got {image.content_type!r}",
        )

    data = await image.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty image upload")

    try:
        vector = embedder.extract(data)
    except InvalidImageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # model / decode edge cases
        log.exception("extract failed for post_uid=%s", post_uid)
        raise HTTPException(
            status_code=400,
            detail=f"failed to process image: {exc}",
        ) from exc

    db.upsert_vector(post_uid, vector)
    return EmbedResponse(post_uid=post_uid, status="stored")


@app.post("/dedupe", response_model=DedupeResponse)
def dedupe(body: DedupeRequest) -> DedupeResponse:
    raw = [uid for uid in body.post_uids if isinstance(uid, str) and uid.strip()]
    if not raw:
        raise HTTPException(status_code=400, detail="post_uids list is empty")

    # Deduplicate request order for lookup, but keep first-seen order
    seen: set[str] = set()
    ordered: list[str] = []
    for uid in raw:
        uid = uid.strip()
        if uid in seen:
            continue
        seen.add(uid)
        ordered.append(uid)

    vectors = db.get_vectors(ordered)
    threshold = similarity_threshold()
    unique, removed, groups, missing = dedupe_post_uids(ordered, vectors, threshold)
    return DedupeResponse(
        unique_post_uids=unique,
        removed_post_uids=removed,
        groups=groups,
        missing_post_uids=missing,
        threshold=threshold,
    )
