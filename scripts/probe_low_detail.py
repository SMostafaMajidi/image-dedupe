#!/usr/bin/env python3
"""Measure how much image detail feeds the 256-bit pHash, for every eval image.

pHash thresholds DCT coefficients against their median; on near-flat images
those coefficients are JPEG noise, so unrelated flat images collide. Writes
per-image stats to .tools/low_detail_stats.json and a contact sheet of the
lowest-detail images for picking a threshold.

  .venv/bin/python scripts/probe_low_detail.py sample_images/related_eval/t2/results.json
"""

from __future__ import annotations

import io
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.phash import detail_score  # noqa: E402


def main() -> int:
    images: dict[int, str] = {}
    for r in json.loads(Path(sys.argv[1]).read_text()):
        images[r["seed"]["id"]] = r["seed"]["image"]
        for c in r.get("candidates") or []:
            images[c["id"]] = c["image"]

    client = httpx.Client(timeout=30, follow_redirects=True)
    stats: dict[str, float] = {}
    thumbs: dict[str, Image.Image] = {}

    def work(item: tuple[int, str]) -> None:
        pid, url = item
        for _ in range(3):
            try:
                im = Image.open(io.BytesIO(client.get(url).content)).convert("RGB")
                break
            except Exception:
                im = None
        if im is None:
            return
        stats[str(pid)] = detail_score(im)
        t = im.copy()
        t.thumbnail((120, 120))
        thumbs[str(pid)] = t

    with ThreadPoolExecutor(24) as ex:
        list(ex.map(work, images.items()))

    out = ROOT / ".tools" / "low_detail_stats.json"
    out.write_text(json.dumps(stats))
    vals = np.array(sorted(stats.values()))
    print("n", len(vals), "percentiles 0.5/1/2/5/10/50:", [round(float(np.percentile(vals, p)), 2) for p in (0.5, 1, 2, 5, 10, 50)])

    low = sorted(stats.items(), key=lambda kv: kv[1])[:60]
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
    cols, cw, ch = 10, 130, 145
    sheet = Image.new("RGB", (cols * cw, ((len(low) + cols - 1) // cols) * ch), "white")
    d = ImageDraw.Draw(sheet)
    for i, (pid, v) in enumerate(low):
        x, y = (i % cols) * cw, (i // cols) * ch
        sheet.paste(thumbs[pid], (x + 5, y + 2))
        d.text((x + 5, y + 126), f"{v:.2f}", fill="black", font=font)
    dst = ROOT / "sample_images" / "related_eval" / "low_detail_lowest60.png"
    sheet.save(dst)
    print("sheet", dst)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
