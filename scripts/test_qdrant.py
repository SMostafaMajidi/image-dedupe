"""Phase 2: embed sample images with CLIP and verify Qdrant upsert/retrieve.

Checks:
1) stored vectors round-trip with negligible float error
2) re-upsert same post_uid overwrites (no duplicate points)
3) optional nearest-neighbor order looks sensible
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import numpy as np
import torch
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db import VectorDB  # noqa: E402
from scripts.test_clip import SAMPLE_DIR, embed_image, load_clip  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("phase2")

SAMPLES = [
    ("post_1", "sunset.jpg"),
    ("post_2", "sunset_bright.jpg"),
    ("post_3", "ocean.jpg"),
    ("post_4", "ocean_crop.jpg"),
    ("post_5", "forest.jpg"),
    ("post_6", "city.jpg"),
]


def main() -> int:
    load_dotenv(ROOT / ".env")
    model_name = os.getenv("CLIP_MODEL_NAME", "openai/clip-vit-base-patch32")

    if not SAMPLE_DIR.exists() or not any(SAMPLE_DIR.glob("*.jpg")):
        log.error("sample images missing — run: python scripts/generate_samples.py")
        return 1

    db = VectorDB()
    try:
        db.ensure_collection()
    except Exception as exc:
        log.error(
            "cannot reach Qdrant at %s:%s — is docker compose up? (%s)",
            db.host,
            db.port,
            exc,
        )
        return 1

    model, processor, device = load_clip(model_name)

    original: dict[str, list[float]] = {}
    for post_uid, filename in SAMPLES:
        path = SAMPLE_DIR / filename
        if not path.is_file():
            log.error("missing sample: %s", path.as_posix())
            return 1
        vec = embed_image(model, processor, device, path).tolist()
        original[post_uid] = vec
        db.upsert_vector(post_uid, vec)
        log.info("stored %s from %s", post_uid, filename)

    count_after_insert = db.count()
    log.info("point count after insert: %d", count_after_insert)
    if count_after_insert < len(SAMPLES):
        log.error("expected at least %d points, got %d", len(SAMPLES), count_after_insert)
        return 2

    retrieved = db.get_vectors([uid for uid, _ in SAMPLES])
    if set(retrieved.keys()) != {uid for uid, _ in SAMPLES}:
        log.error("retrieve keys mismatch: got %s", sorted(retrieved.keys()))
        return 2

    max_abs_err = 0.0
    for uid, expected in original.items():
        got = retrieved[uid]
        if len(got) != len(expected):
            log.error("%s dim mismatch %d vs %d", uid, len(got), len(expected))
            return 2
        err = float(np.max(np.abs(np.asarray(expected, dtype=np.float32) - np.asarray(got, dtype=np.float32))))
        max_abs_err = max(max_abs_err, err)
        log.info("%s max_abs_err=%.6e", uid, err)

    print("\n=== Round-trip ===")
    print(f"  max abs float error across all vectors: {max_abs_err:.6e}")
    if max_abs_err > 1e-5:
        log.error("FAILED: float error too large (> 1e-5)")
        return 2
    print("  PASS: vectors match within float32 tolerance")

    # Re-upsert post_1 with a different vector (city) and ensure overwrite, not duplicate
    overwrite_vec = original["post_6"]
    db.upsert_vector("post_1", overwrite_vec)
    count_after_overwrite = db.count()
    print("\n=== Upsert overwrite ===")
    print(f"  count before overwrite check baseline: {count_after_insert}")
    print(f"  count after re-upsert post_1:          {count_after_overwrite}")
    if count_after_overwrite != count_after_insert:
        log.error(
            "FAILED: point count changed (%d -> %d); duplicate created?",
            count_after_insert,
            count_after_overwrite,
        )
        return 2

    got_post_1 = db.get_vectors(["post_1"])["post_1"]
    overwrite_err = float(
        np.max(np.abs(np.asarray(overwrite_vec, dtype=np.float32) - np.asarray(got_post_1, dtype=np.float32)))
    )
    print(f"  post_1 now matches city vector err={overwrite_err:.6e}")
    if overwrite_err > 1e-5:
        log.error("FAILED: overwritten vector did not stick")
        return 2
    print("  PASS: upsert overwrote same point_id")

    # Restore original post_1 for similarity search sanity
    db.upsert_vector("post_1", original["post_1"])

    query = original["post_1"]  # sunset
    hits = db.search_similar(query, top_k=4)
    print("\n=== Nearest neighbors for post_1 (sunset) ===")
    for i, hit in enumerate(hits, start=1):
        print(f"  {i}. {hit['post_uid']}  score={hit['score']:.4f}")

    if not hits or hits[0]["post_uid"] != "post_1":
        log.warning("expected self as top-1; got %s", hits[0] if hits else None)
    else:
        print("  PASS: self is nearest neighbor")

    # After self, sunset_bright (post_2) should rank above forest/city
    ranked = [h["post_uid"] for h in hits if h["post_uid"] != "post_1"]
    if ranked and ranked[0] == "post_2":
        print("  PASS: post_2 (sunset_bright) is next nearest")
    else:
        log.warning("unexpected neighbor order after self: %s", ranked)

    log.info("Phase 2 SUCCESS")
    return 0


if __name__ == "__main__":
    # silence unused torch import warning by using it for dtype checks if needed
    _ = torch
    sys.exit(main())
