"""REL-DUP1 validation: pHash alone on "cheap edit" same-media pairs
(brightness tweak, resize/recompress) vs different-content pairs.

pHash is a DCT/low-frequency hash — by design it is NOT expected to survive
a crop (spatial shift) or a corner watermark (local high-contrast patch) on
its own; that is exactly why REL-DUP2 (scripts/test_dedupe_combined.py)
also checks CLIP cosine and merges on EITHER signal. This script only
gates on the "cheap edit" pairs pHash is supposed to be strong at; crop/
watermark pairs are reported for visibility but do not fail the script.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = ROOT / "sample_images"
sys.path.insert(0, str(ROOT))

from app.phash import compute_phash, hamming_distance  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("test_phash")


def main() -> int:
    load_dotenv(ROOT / ".env")
    max_distance = int(os.getenv("DEDUPE_HASH_MAX_DISTANCE", "10"))

    if not SAMPLE_DIR.exists() or not any(SAMPLE_DIR.glob("*.jpg")):
        log.error(
            "sample images missing under %s — run: python scripts/generate_samples.py",
            SAMPLE_DIR.as_posix(),
        )
        return 1

    names = [
        "sunset.jpg",
        "sunset_bright.jpg",
        "sunset_watermark.jpg",
        "sunset_resized.jpg",
        "ocean.jpg",
        "ocean_crop.jpg",
        "forest.jpg",
        "city.jpg",
    ]
    hashes: dict[str, str] = {}
    for name in names:
        path = SAMPLE_DIR / name
        if not path.is_file():
            log.error("missing sample: %s (run generate_samples.py)", path.as_posix())
            return 1
        hashes[name] = compute_phash(path.read_bytes())
        log.info("phash %-22s = %s", name, hashes[name])

    # pHash's sweet spot: same pixels, global recompress/brightness change.
    cheap_edit_pairs = [
        ("sunset.jpg", "sunset_bright.jpg"),
        ("sunset.jpg", "sunset_resized.jpg"),
    ]
    # Known pHash blind spots (spatial shift / localized patch) — informative
    # only here; covered by the combined signal in test_dedupe_combined.py.
    structural_edit_pairs = [
        ("sunset.jpg", "sunset_watermark.jpg"),
        ("ocean.jpg", "ocean_crop.jpg"),
    ]
    different_pairs = [
        ("sunset.jpg", "ocean.jpg"),
        ("sunset.jpg", "forest.jpg"),
        ("sunset.jpg", "city.jpg"),
        ("ocean.jpg", "forest.jpg"),
        ("ocean.jpg", "city.jpg"),
        ("forest.jpg", "city.jpg"),
    ]

    print("\n=== Cheap-edit same-media pairs (pHash should catch) ===")
    same_distances: list[int] = []
    for left, right in cheap_edit_pairs:
        d = hamming_distance(hashes[left], hashes[right])
        same_distances.append(d)
        flag = "PASS" if d <= max_distance else "MISSED"
        print(f"  {left} <-> {right}: hamming={d}  [{flag}]")

    print("\n=== Structural-edit pairs (pHash blind spot by design; see REL-DUP2) ===")
    for left, right in structural_edit_pairs:
        d = hamming_distance(hashes[left], hashes[right])
        flag = "caught-by-phash-too" if d <= max_distance else "needs-CLIP (expected)"
        print(f"  {left} <-> {right}: hamming={d}  [{flag}]")

    print("\n=== Different-content pairs ===")
    diff_distances: list[int] = []
    for left, right in different_pairs:
        d = hamming_distance(hashes[left], hashes[right])
        diff_distances.append(d)
        flag = "ok-gap" if d > max_distance else "FALSE-POSITIVE"
        print(f"  {left} <-> {right}: hamming={d}  [{flag}]")

    worst_same = max(same_distances)
    best_diff = min(diff_distances)
    print("\n=== Summary ===")
    print(f"  worst(cheap-edit same-media) = {worst_same}")
    print(f"  best(different)              = {best_diff}")
    print(f"  DEDUPE_HASH_MAX_DISTANCE     = {max_distance}")

    if worst_same > max_distance:
        log.error("FAILED: a cheap-edit same-media pair exceeds hash_max_distance (regression)")
        return 2
    if best_diff <= max_distance:
        log.error("FAILED: a different-content pair falls within hash_max_distance (false positive)")
        return 2

    log.info(
        "Phase REL-DUP1 SUCCESS: pHash cleanly catches cheap edits and never "
        "false-positives on different content (crop/watermark left to REL-DUP2)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
