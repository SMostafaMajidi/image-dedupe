#!/usr/bin/env python3
"""Probe which signal separates real reposts from same-template/different-text pairs.

Input: removed pairs from ``eval_related_dedupe`` results (pHash64 <= 10).
Per pair computes: pHash256 / dHash256 distance, pixel changed-area, and a
per-image "flat background" score. Writes a CSV + a contact sheet sorted by
pHash256 distance for manual labelling.

  .venv/bin/python scripts/probe_template_pairs.py \
    sample_images/related_eval/t10/results.json \
    sample_images/related_eval/baseline_unionfind_t10/results.json
"""

from __future__ import annotations

import csv
import io
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import imagehash
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "sample_images" / "related_eval" / "template_probe"


def flat_score(img: Image.Image) -> float:
    """Share of pixels within ±12 gray levels of the dominant level (0..1)."""
    g = np.asarray(img.convert("L").resize((128, 128)), dtype=np.int16)
    hist = np.bincount(g.ravel(), minlength=256)
    mode = int(hist.argmax())
    return float(((g >= mode - 12) & (g <= mode + 12)).mean())


def changed_area(a: Image.Image, b: Image.Image, size: int = 128, thr: int = 48) -> float:
    ga = np.asarray(a.convert("L").resize((size, size)), dtype=np.int16)
    gb = np.asarray(b.convert("L").resize((size, size)), dtype=np.int16)
    return float((np.abs(ga - gb) > thr).mean())


def main() -> int:
    pairs: dict[tuple[int, int], dict] = {}
    for path in sys.argv[1:]:
        for r in json.load(open(path)):
            for rm in r.get("removed") or []:
                if not rm.get("match_image") or rm["distance"] > 10:
                    continue
                key = tuple(sorted((rm["id"], rm["match_id"])))
                pairs.setdefault(key, rm)
    items = list(pairs.values())
    print("pairs", len(items))

    client = httpx.Client(timeout=20, follow_redirects=True)
    cache: dict[str, Image.Image | None] = {}

    def fetch(u: str) -> None:
        try:
            cache[u] = Image.open(io.BytesIO(client.get(u).content)).convert("RGB")
        except Exception:
            cache[u] = None

    urls = {p["image"] for p in items} | {p["match_image"] for p in items}
    with ThreadPoolExecutor(16) as ex:
        list(ex.map(fetch, urls))

    rows = []
    for p in items:
        a, b = cache.get(p["image"]), cache.get(p["match_image"])
        if a is None or b is None:
            continue
        rows.append(
            {
                "id": p["id"],
                "match_id": p["match_id"],
                "ph64": p["distance"],
                "ph256": int(imagehash.phash(a, 16) - imagehash.phash(b, 16)),
                "dh256": int(imagehash.dhash(a, 16) - imagehash.dhash(b, 16)),
                "changed": round(changed_area(a, b), 4),
                "flat_a": round(flat_score(a), 3),
                "flat_b": round(flat_score(b), 3),
                "image": p["image"],
                "match_image": p["match_image"],
            }
        )
    rows.sort(key=lambda r: (r["ph256"], r["changed"]))

    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "pairs.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # contact sheet: skip exact byte-level reposts (ph256==0) to keep it readable
    show = [r for r in rows if r["ph256"] > 0]
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 11)
    cols, cw, ch = 5, 250, 150
    sheet = Image.new("RGB", (cols * cw, ((len(show) + cols - 1) // cols) * ch), "white")
    d = ImageDraw.Draw(sheet)
    for i, r in enumerate(show):
        x, y = (i % cols) * cw, (i // cols) * ch
        for j, u in enumerate((r["image"], r["match_image"])):
            t = cache[u].copy()
            t.thumbnail((118, 118))
            sheet.paste(t, (x + 4 + j * 122, y + 2))
        d.text(
            (x + 4, y + 122),
            f"#{i} p64={r['ph64']} p256={r['ph256']} chg={r['changed']:.2f}\nflat={max(r['flat_a'], r['flat_b']):.2f} {r['id']}",
            fill="black",
            font=font,
        )
    sheet.save(OUT / "pairs_by_ph256.png")
    print("rows", len(rows), "shown", len(show), "->", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
