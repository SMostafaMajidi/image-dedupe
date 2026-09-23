"""Qdrant vector store helpers for image embeddings.

Point IDs are deterministic UUID5 values derived from post_uid strings.
post_uid itself is stored in the point payload for reverse lookup.
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Sequence

from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

log = logging.getLogger(__name__)

VECTOR_SIZE = 512
NAMESPACE = uuid.NAMESPACE_URL
ROOT = Path(__file__).resolve().parent


def post_uid_to_point_id(post_uid: str) -> str:
    """Stable UUID string for a given post_uid (same input → same id)."""
    return str(uuid.uuid5(NAMESPACE, post_uid))


class VectorDB:
    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        collection_name: str | None = None,
        vector_size: int = VECTOR_SIZE,
    ) -> None:
        load_dotenv(ROOT / ".env")
        self.host = host or os.getenv("QDRANT_HOST", "localhost")
        self.port = int(port if port is not None else os.getenv("QDRANT_PORT", "6333"))
        self.collection_name = (
            collection_name
            or os.getenv("COLLECTION_NAME")
            or os.getenv("QDRANT_COLLECTION", "image_embeddings")
        )
        self.vector_size = vector_size
        self.client = QdrantClient(
            host=self.host,
            port=self.port,
            check_compatibility=False,
        )
        log.info(
            "Qdrant client ready host=%s port=%s collection=%s",
            self.host,
            self.port,
            self.collection_name,
        )

    def ensure_collection(self) -> None:
        names = {c.name for c in self.client.get_collections().collections}
        if self.collection_name in names:
            log.info("collection already exists: %s", self.collection_name)
            return

        log.info(
            "creating collection=%s size=%d distance=Cosine",
            self.collection_name,
            self.vector_size,
        )
        self.client.create_collection(
            collection_name=self.collection_name,
            vectors_config=qm.VectorParams(
                size=self.vector_size,
                distance=qm.Distance.COSINE,
            ),
        )

    def upsert_vector(self, post_uid: str, vector: Sequence[float]) -> str:
        point_id = post_uid_to_point_id(post_uid)
        self.client.upsert(
            collection_name=self.collection_name,
            points=[
                qm.PointStruct(
                    id=point_id,
                    vector=list(vector),
                    payload={"post_uid": post_uid},
                )
            ],
        )
        log.info("upserted post_uid=%s point_id=%s", post_uid, point_id)
        return point_id

    def get_vectors(self, post_uid_list: Sequence[str]) -> dict[str, list[float]]:
        if not post_uid_list:
            return {}

        id_to_uid = {post_uid_to_point_id(uid): uid for uid in post_uid_list}
        points = self.client.retrieve(
            collection_name=self.collection_name,
            ids=list(id_to_uid.keys()),
            with_payload=True,
            with_vectors=True,
        )

        result: dict[str, list[float]] = {}
        for point in points:
            uid = id_to_uid.get(str(point.id))
            if uid is None and isinstance(point.payload, dict):
                uid = point.payload.get("post_uid")
            if uid is None or point.vector is None:
                continue
            vec = point.vector
            if isinstance(vec, dict):
                # named vectors (not used yet) — take first
                vec = next(iter(vec.values()))
            result[uid] = list(vec)
        return result

    def search_similar(
        self,
        vector: Sequence[float],
        top_k: int = 5,
    ) -> list[dict]:
        # qdrant-client>=1.14 uses query_points instead of deprecated search()
        response = self.client.query_points(
            collection_name=self.collection_name,
            query=list(vector),
            limit=top_k,
            with_payload=True,
        )
        return [
            {
                "post_uid": (hit.payload or {}).get("post_uid"),
                "score": float(hit.score),
                "point_id": str(hit.id),
            }
            for hit in response.points
        ]

    def count(self) -> int:
        return int(self.client.count(collection_name=self.collection_name, exact=True).count)
