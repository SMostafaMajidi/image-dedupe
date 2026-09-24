"""FastAPI entrypoint: /embed and /dedupe."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse

from app.browse import BROWSE_HTML
from app.db import VectorDB
from app.dedupe import dedupe_post_uids
from app.embedding import InvalidImageError, get_embedder
from app.schemas import (
    DedupeRequest,
    DedupeResponse,
    EmbedResponse,
    PointRow,
    PointsPageResponse,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("app")

db = VectorDB()
embedder = get_embedder()


def similarity_threshold() -> float:
    return float(os.getenv("SIMILARITY_THRESHOLD", "0.90"))


def max_upload_bytes() -> int:
    return int(os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))


def _is_image_content_type(content_type: str | None) -> bool:
    if not content_type:
        return False
    main = content_type.split(";", 1)[0].strip().lower()
    return main.startswith("image/")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    db.ensure_collection()
    embedder.load()
    log.info(
        "startup complete threshold=%.3f max_upload=%d qdrant=%s:%s collection=%s",
        similarity_threshold(),
        max_upload_bytes(),
        db.host,
        db.port,
        db.collection_name,
    )
    yield


app = FastAPI(
    title="image-dedupe",
    description="CLIP + Qdrant near-duplicate image detection",
    version="0.4.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "collection": db.collection_name,
        "threshold": similarity_threshold(),
        "max_upload_bytes": max_upload_bytes(),
        "points": db.count(),
    }


@app.get("/browse", response_class=HTMLResponse, include_in_schema=False)
def browse() -> str:
    """Simple HTML table of stored post_uid vectors."""
    return BROWSE_HTML


@app.get("/points", response_model=PointsPageResponse)
def list_points(
    limit: int = Query(50, ge=1, le=200),
    offset: str | None = Query(None, description="Scroll offset from previous page"),
) -> PointsPageResponse:
    rows, next_offset = db.list_points(limit=limit, offset=offset)
    return PointsPageResponse(
        total=db.count(),
        limit=limit,
        collection=db.collection_name,
        offset=offset,
        next_offset=str(next_offset) if next_offset is not None else None,
        points=[PointRow(**row) for row in rows],
    )


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

    limit = max_upload_bytes()
    data = await image.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"image exceeds MAX_UPLOAD_BYTES={limit}",
        )
    if not data:
        raise HTTPException(status_code=400, detail="empty image upload")

    try:
        vector = embedder.extract(data)
    except InvalidImageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
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
