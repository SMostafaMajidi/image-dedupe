"""Fetch Wisgoon posts over HTTP (public gateway API — no SDK import)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

DEFAULT_POST_ITEM_URL = "https://gateway.wisgoon.com/api/v9/post/item/{uid}/"


class WisgoonFetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class WisgoonPost:
    uid: str
    image_url: str
    content_type: str | None = None


def post_item_url(uid: str) -> str:
    template = os.getenv("WISGOON_POST_ITEM_URL", DEFAULT_POST_ITEM_URL)
    return template.format(uid=uid)


def fetch_post(uid: str, *, timeout: float = 20.0) -> WisgoonPost:
    """GET /api/v9/post/item/{uid}/ and return main image URL."""
    url = post_item_url(uid)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": "image-dedupe/0.4"})
    except httpx.HTTPError as exc:
        raise WisgoonFetchError(f"wisgoon request failed: {exc}") from exc

    if resp.status_code == 404:
        raise WisgoonFetchError(f"post not found on wisgoon: {uid}")
    if resp.status_code >= 400:
        raise WisgoonFetchError(f"wisgoon HTTP {resp.status_code} for {uid}")

    try:
        data = resp.json()
    except ValueError as exc:
        raise WisgoonFetchError("wisgoon returned non-JSON body") from exc

    image = (data.get("image") or "").strip()
    if not image:
        images = data.get("images") or {}
        original = images.get("original") or {}
        image = (original.get("url") or "").strip()
    if not image:
        raise WisgoonFetchError(f"post {uid} has no image URL")

    return WisgoonPost(
        uid=str(data.get("uid") or uid),
        image_url=image,
        content_type=data.get("content_type"),
    )


def download_bytes(url: str, *, max_bytes: int, timeout: float = 30.0) -> bytes:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            with client.stream("GET", url, headers={"User-Agent": "image-dedupe/0.4"}) as resp:
                resp.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes(64 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise WisgoonFetchError(f"image exceeds {max_bytes} bytes")
                    chunks.append(chunk)
                data = b"".join(chunks)
    except httpx.HTTPError as exc:
        raise WisgoonFetchError(f"image download failed: {exc}") from exc
    if not data:
        raise WisgoonFetchError("empty image body")
    return data
