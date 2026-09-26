#!/usr/bin/env python3
"""Build a near-dup fixture on LOCAL ES only (REL-DUP4 filter test).

Takes a real IMAGE doc, downloads its image, adds a tiny corner mark,
writes a synthetic sibling doc with the new phash, then prints the IDs
for the Go filter smoke test.

Never writes to production.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.phash import compute_phash, hamming_distance  # noqa: E402

LOCAL = "http://127.0.0.1:9201"
INDEX = "wis-post-0.0.2-v3"
SYNTH_ID = 999000001
SOURCE_ID = 82361745  # known local IMAGE with phash


def http_json(method: str, url: str, body: dict | None = None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    if "192.168.20.208" in LOCAL:
        print("refuse prod", file=sys.stderr)
        return 2

    doc = http_json("GET", f"{LOCAL}/{INDEX}/_doc/{SOURCE_ID}")
    if not doc.get("found"):
        print(f"source {SOURCE_ID} missing in local ES", file=sys.stderr)
        return 1
    src = doc["_source"]
    image_url = src.get("image")
    base_phash = src.get("image_phash")
    print(f"source id={SOURCE_ID} uid={src.get('uid')} phash={base_phash}")
    print(f"image={image_url}")

    raw = urllib.request.urlopen(image_url, timeout=30).read()
    h0 = compute_phash(raw)
    print(f"recomputed_phash={h0} (stored={base_phash})")

    im = Image.open(BytesIO(raw)).convert("RGB")

    # Prefer a realistic tiny logo; fall back to recompress / identical hash
    # so the Go filter path can still be exercised on local ES.
    chosen_label = ""
    h_variant = ""
    dist = 99
    variant_bytes = b""

    wm = im.copy()
    d = ImageDraw.Draw(wm)
    d.rectangle([wm.width - 24, wm.height - 24, wm.width - 4, wm.height - 4], fill=(255, 0, 0))
    buf = BytesIO()
    wm.save(buf, "JPEG", quality=90)
    variant_bytes = buf.getvalue()
    h_variant = compute_phash(variant_bytes)
    dist = hamming_distance(h0, h_variant)
    chosen_label = "tiny_corner_logo"

    if dist > 10:
        buf = BytesIO()
        im.save(buf, "JPEG", quality=40)
        variant_bytes = buf.getvalue()
        h_variant = compute_phash(variant_bytes)
        dist = hamming_distance(h0, h_variant)
        chosen_label = "recompress_q40"

    if dist > 10:
        h_variant = h0
        dist = 0
        chosen_label = "identical_hash_fallback"
        variant_bytes = raw

    print(f"variant={chosen_label} phash={h_variant} hamming={dist} expect_merge={dist <= 10}")

    # Also load an unrelated post for KEEP check
    unrelated = http_json(
        "POST",
        f"{LOCAL}/{INDEX}/_search",
        {
            "size": 1,
            "query": {
                "bool": {
                    "must": [{"exists": {"field": "image_phash"}}],
                    "must_not": [{"ids": {"values": [str(SOURCE_ID)]}}],
                }
            },
            "_source": ["id", "uid", "image_phash"],
        },
    )
    uhit = unrelated["hits"]["hits"][0]
    unrelated_id = int(uhit["_id"])
    unrelated_phash = uhit["_source"]["image_phash"]
    print(
        f"unrelated id={unrelated_id} phash={unrelated_phash} "
        f"hamming_vs_source={hamming_distance(h0, unrelated_phash)}"
    )

    synth = {
        "id": SYNTH_ID,
        "uid": "LOCALNEARDUP1",
        "content_type": "IMAGE",
        "status": 1,
        "image": image_url,
        "image_phash": h_variant,
        "text": f"synthetic near-dup ({chosen_label}) for REL-DUP4 local filter test",
        "title": "",
        "user_id": src.get("user_id", 0),
    }
    http_json("PUT", f"{LOCAL}/{INDEX}/_doc/{SYNTH_ID}?refresh=true", synth)
    print(f"wrote synthetic doc id={SYNTH_ID}")

    out = {
        "seed_id": SOURCE_ID,
        "near_dup_id": SYNTH_ID,
        "unrelated_id": unrelated_id,
        "variant": chosen_label,
        "hamming": dist,
        "base_phash": h0,
        "variant_phash": h_variant,
    }
    fixture_path = ROOT / ".tools" / "local_neardup_fixture.json"
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    fixture_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"fixture={fixture_path}")
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
