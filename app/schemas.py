"""Pydantic request/response models for the API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class EmbedResponse(BaseModel):
    post_uid: str
    status: str = "stored"


class DedupeRequest(BaseModel):
    post_uids: list[str] = Field(..., description="Post identifiers to deduplicate")


class DedupeResponse(BaseModel):
    unique_post_uids: list[str]
    removed_post_uids: list[str]
    groups: list[list[str]]
    missing_post_uids: list[str]
    threshold: float


class PointRow(BaseModel):
    point_id: str
    post_uid: str | None = None


class PointsPageResponse(BaseModel):
    total: int
    limit: int
    collection: str
    offset: str | None = None
    next_offset: str | None = None
    points: list[PointRow]
