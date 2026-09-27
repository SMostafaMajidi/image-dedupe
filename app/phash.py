"""Perceptual hash (pHash) for exact-media duplicate detection.

Unlike CLIP embeddings (semantic similarity — used for /similar & future
semantic search), pHash targets "same underlying media, minor edits":
resize, recompression, brightness/contrast tweaks, small corner watermarks.
It is DCT-based (low-frequency), so it is naturally more robust to those
changes than a raw pixel hash.

Usage:
    h = compute_phash(image_bytes)              # -> "a1b2c3..." (hex)
    d = hamming_distance(hash_a, hash_b)         # -> int (0..hash_size**2)
"""

from __future__ import annotations

from io import BytesIO

import imagehash
from PIL import Image, UnidentifiedImageError

DEFAULT_HASH_SIZE = 8  # 8x8 -> 64-bit hash (standard pHash size)


class InvalidImageError(ValueError):
    """Raised when the bytes cannot be decoded as an image."""


def _decode(data: bytes) -> Image.Image:
    try:
        image = Image.open(BytesIO(data))
        image.load()
        return image.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidImageError(f"invalid or unreadable image: {exc}") from exc


def compute_phash(data: bytes, hash_size: int = DEFAULT_HASH_SIZE) -> str:
    """Return a stable hex string perceptual hash for raw image bytes."""
    return str(imagehash.phash(_decode(data), hash_size=hash_size))


def compute_phashes(data: bytes, hash_sizes: tuple[int, ...]) -> dict[int, str]:
    """Decode once, return ``{hash_size: hex}`` for each requested size."""
    image = _decode(data)
    return {size: str(imagehash.phash(image, hash_size=size)) for size in hash_sizes}


def hamming_distance(hash_a: str, hash_b: str) -> int:
    """Bit difference between two hex pHash strings (same hash_size only)."""
    return int(imagehash.hex_to_hash(hash_a) - imagehash.hex_to_hash(hash_b))
