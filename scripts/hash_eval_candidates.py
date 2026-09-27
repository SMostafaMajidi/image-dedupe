#!/usr/bin/env python3
"""Compute pHash locally for every seed + candidate in an eval results.json.

Lets the gateway eval tool try a hash variant (e.g. 256-bit) without writing
anything to Elasticsearch.

  .venv/bin/python scripts/hash_eval_candidates.py \
    sample_images/related_eval/t2/results.json --hash-size 16 \
    --out .tools/eval_hashes_256.json
"""

from __future__ import annotations

import argparse
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import imagehash
from PIL import Image
import io

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("hash_eval")
logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("results", type=Path)
    p.add_argument("--hash-size", type=int, default=16)
    p.add_argument("--workers", type=int, default=24)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()

    images: dict[int, str] = {}
    for r in json.loads(args.results.read_text()):
        if r["seed"].get("image"):
            images[r["seed"]["id"]] = r["seed"]["image"]
        for c in r.get("candidates") or []:
            if c.get("image"):
                images[c["id"]] = c["image"]
    log.info("images to hash: %d", len(images))

    client = httpx.Client(timeout=30, follow_redirects=True)
    out: dict[str, str] = {}

    def work(item: tuple[int, str]) -> None:
        pid, url = item
        try:
            im = Image.open(io.BytesIO(client.get(url).content)).convert("RGB")
            out[str(pid)] = str(imagehash.phash(im, hash_size=args.hash_size))
        except Exception:
            pass

    with ThreadPoolExecutor(args.workers) as ex:
        list(ex.map(work, images.items()))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out) + "\n")
    log.info("hashed %d/%d → %s", len(out), len(images), args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
