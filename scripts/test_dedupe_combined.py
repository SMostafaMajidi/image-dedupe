"""REL-DUP2 validation: combined signal (pHash OR strict CLIP cosine).

Pure pHash alone is known to miss two common real-world cases on our
synthetic samples:
  - a corner watermark/logo box (shifts enough low-freq DCT energy on a
    simple/smooth image)
  - a crop (shifts spatial content, which pHash is *not* designed to survive)

CLIP cosine similarity is robust to exactly those two cases (global
semantic content barely changes), which is why ``dedupe_post_uids`` merges
on EITHER signal. This script proves the combined signal recovers both
cases that scripts/test_phash.py (pHash-only) correctly flags as missed,
while still not merging genuinely different content.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = ROOT / "sample_images"
sys.path.insert(0, str(ROOT))

from app.dedupe import dedupe_post_uids  # noqa: E402
from app.embedding import get_embedder  # noqa: E402
from app.phash import compute_phash  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("test_dedupe_combined")

DEDUPE_THRESHOLD = 0.97
HASH_MAX_DISTANCE = 10

# post_uid -> (image file, "expected group" label)
POSTS = {
    "sunset-original": ("sunset.jpg", "sunset"),
    "sunset-bright": ("sunset_bright.jpg", "sunset"),
    "sunset-watermark": ("sunset_watermark.jpg", "sunset"),
    "sunset-resized": ("sunset_resized.jpg", "sunset"),
    "ocean-original": ("ocean.jpg", "ocean"),
    "ocean-crop": ("ocean_crop.jpg", "ocean"),
    "forest": ("forest.jpg", "forest"),
    "city": ("city.jpg", "city"),
}


def main() -> int:
    if not SAMPLE_DIR.exists() or not any(SAMPLE_DIR.glob("*.jpg")):
        log.error("sample images missing — run: python scripts/generate_samples.py")
        return 1

    embedder = get_embedder()
    embedder.load()

    vectors: dict[str, list[float]] = {}
    hashes: dict[str, str] = {}
    for post_uid, (filename, _label) in POSTS.items():
        path = SAMPLE_DIR / filename
        if not path.is_file():
            log.error("missing sample: %s", path.as_posix())
            return 1
        data = path.read_bytes()
        vectors[post_uid] = embedder.extract(data)
        hashes[post_uid] = compute_phash(data)

    post_uids = list(POSTS.keys())
    unique, removed, groups, missing = dedupe_post_uids(
        post_uids,
        vectors,
        DEDUPE_THRESHOLD,
        hashes=hashes,
        hash_max_distance=HASH_MAX_DISTANCE,
    )

    print(f"\nthreshold={DEDUPE_THRESHOLD}  hash_max_distance={HASH_MAX_DISTANCE}")
    print(f"unique  ({len(unique)}): {unique}")
    print(f"removed ({len(removed)}): {removed}")
    print("groups:")
    for group in groups:
        labels = {POSTS[uid][1] for uid in group}
        print(f"  {group}  -> expected label(s): {labels}")
    if missing:
        print(f"missing: {missing}")

    # Expected: exactly 4 groups (sunset x4, ocean x2, forest x1, city x1),
    # and every group is internally consistent (single expected label).
    ok = True
    if len(groups) != 4:
        log.error("FAILED: expected 4 groups, got %d", len(groups))
        ok = False
    for group in groups:
        labels = {POSTS[uid][1] for uid in group}
        if len(labels) != 1:
            log.error("FAILED: group %s mixes labels %s (false positive merge)", group, labels)
            ok = False
    sunset_group = next((g for g in groups if POSTS[g[0]][1] == "sunset"), [])
    if len(sunset_group) != 4:
        log.error(
            "FAILED: sunset group should have 4 members (incl. watermark+resize), got %s",
            sunset_group,
        )
        ok = False

    if not ok:
        return 2

    log.info(
        "Phase REL-DUP2 SUCCESS: combined signal groups watermark/resize/crop as duplicates "
        "without merging different content"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
