#!/usr/bin/env python3
"""Build a single self-contained HTML report: current related (no filter) vs
64-bit pHash vs 256-bit pHash, on the same candidate lists.

Inputs are ``eval_related_dedupe`` outputs run with ``-reuse`` + ``-hashes-file``
(so every config sees identical candidates and 100% hash coverage).

  .venv/bin/python scripts/build_related_eval_report.py
"""

from __future__ import annotations

import base64
import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "sample_images" / "related_eval"
TOOLS = ROOT / ".tools"
OUT = EVAL / "report_256_vs_64.html"

CONFIGS = [
    ("p64_t10", "64 بیتی، آستانه ۱۰", "64", 10),
    ("p64_t2", "64 بیتی، آستانه ۲", "64", 2),
    ("p256_t16", "256 بیتی، آستانه ۱۶", "256", 16),
    ("p256g_t16", "256 بیتی + محافظ کم‌جزئیات (پیشنهادی)", "256", 16),
]
REC = "p256g_t16"
MIN_DETAIL = 25.0
FEW_LEFT = 5


def ham(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def thumb(client: httpx.Client, url: str) -> str | None:
    im = None
    for _ in range(4):
        try:
            resp = client.get(url)
            if resp.status_code == 404:
                return None
            im = Image.open(io.BytesIO(resp.content)).convert("RGB")
            break
        except Exception:
            continue
    if im is None:
        return None
    im.thumbnail((112, 112))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=70, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def main() -> int:
    runs = {k: {r["seed"]["id"]: r for r in json.loads((EVAL / k / "results.json").read_text())} for k, *_ in CONFIGS}
    summ = {k: json.loads((EVAL / k / "summary.json").read_text()) for k, *_ in CONFIGS}
    es64 = json.loads((EVAL / "t2" / "summary.json").read_text())
    h64 = json.loads((TOOLS / "eval_hashes_64.json").read_text())
    h256 = json.loads((TOOLS / "eval_hashes_256.json").read_text())
    detail = json.loads((TOOLS / "low_detail_stats.json").read_text())

    base = runs[REC]
    seeds = []
    images: dict[int, str] = {}
    for sid, r in base.items():
        cands = r.get("candidates") or []
        if not cands:
            continue
        images[sid] = r["seed"]["image"]
        for c in cands:
            images[c["id"]] = c["image"]
        per = {}
        for k, *_ in CONFIGS:
            rr = runs[k][sid]
            per[k] = {
                "removed": {
                    str(m["id"]): {"d": m["distance"], "m": m["match_id"], "k": m["match_kind"]}
                    for m in (rr.get("removed") or [])
                },
            }
        seeds.append(
            {
                "id": sid,
                "uid": r["seed"]["uid"],
                "group": r["seed"]["group"],
                "cands": [c["id"] for c in cands],
                "per": per,
            }
        )

    def diff_pairs(a: str, b: str) -> list[dict]:
        """Pairs removed by config a but kept by config b (unique by id pair)."""
        out, seen = [], set()
        for s in seeds:
            ra, rb = s["per"][a]["removed"], s["per"][b]["removed"]
            for cid, m in ra.items():
                if cid in rb:
                    continue
                key = tuple(sorted((int(cid), m["m"])))
                if key in seen:
                    continue
                seen.add(key)
                x, y = cid, str(m["m"])
                out.append(
                    {
                        "id": int(cid),
                        "match": m["m"],
                        "seed": s["id"],
                        "d64": ham(h64[x], h64[y]) if x in h64 and y in h64 else None,
                        "d256": ham(h256[x], h256[y]) if x in h256 and y in h256 else None,
                    }
                )
        return out

    diffs = {
        "t10_not_256": diff_pairs("p64_t10", REC),
        "t2_not_256": diff_pairs("p64_t2", REC),
        "256_not_t2": diff_pairs(REC, "p64_t2"),
        "guard_fixed": diff_pairs("p256_t16", REC),
    }
    low_detail = sorted(
        ({"id": int(k), "score": round(v, 1)} for k, v in detail.items() if v < MIN_DETAIL and int(k) in images),
        key=lambda x: x["score"],
    )
    client = httpx.Client(timeout=30, follow_redirects=True)
    thumbs: dict[str, str] = {}

    def work(item: tuple[int, str]) -> None:
        pid, url = item
        if url:
            t = thumb(client, url)
            if t:
                thumbs[str(pid)] = t

    with ThreadPoolExecutor(24) as ex:
        list(ex.map(work, images.items()))

    def few_left(key: str) -> int:
        return sum(1 for s in seeds if len(s["cands"]) - len(s["per"][key]["removed"]) < FEW_LEFT) if key else sum(
            1 for s in seeds if len(s["cands"]) < FEW_LEFT
        )

    table = [
        {
            "name": "حالت فعلی پرود (بدون فیلتر)",
            "cov": None,
            "removed": 0,
            "pct": 0.0,
            "seeds": 0,
            "cand": summ[REC]["all"]["candidates"],
            "few": few_left(""),
        },
        {
            "name": "64 بیتی، آستانه ۲ — روی ES فعلی (فقط ۱M پست اخیر هش دارد)",
            "cov": es64["all"]["hash_coverage_pct"],
            "removed": es64["all"]["removed"],
            "pct": es64["all"]["removed_pct"],
            "seeds": es64["all"]["seeds_with_removal"],
            "cand": es64["all"]["candidates"],
            "p95": es64["all"]["filter_p95_ms"],
        },
    ]
    for k, label, *_ in CONFIGS:
        a = summ[k]["all"]
        table.append(
            {
                "name": label + " — پوشش کامل (هش لوکال)",
                "cov": a["hash_coverage_pct"],
                "removed": a["removed"],
                "pct": a["removed_pct"],
                "seeds": a["seeds_with_removal"],
                "cand": a["candidates"],
                "random": summ[k]["random"]["removed_pct"],
                "dupes": summ[k]["dupes"]["removed_pct"],
                "few": few_left(k),
                "rec": k == REC,
            }
        )

    data = {
        "configs": [{"key": k, "label": l} for k, l, *_ in CONFIGS],
        "seeds": seeds,
        "diffs": diffs,
        "table": table,
        "n_seeds_total": len(base),
        "n_seeds": len(seeds),
        "rec": REC,
        "low_detail": low_detail,
        "min_detail": MIN_DETAIL,
        "few_left": FEW_LEFT,
        "n_images": len(images),
    }
    html = TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False)).replace(
        "__THUMBS__", json.dumps(thumbs)
    )
    OUT.write_text(html, encoding="utf-8")
    missing = sorted(set(map(str, images)) - set(thumbs))
    print(f"{OUT} {OUT.stat().st_size / 1e6:.1f} MB, seeds={len(seeds)}, thumbs={len(thumbs)}/{len(images)} missing={missing[:10]}")
    return 0


TEMPLATE = (ROOT / "scripts" / "related_eval_report.html").read_text(encoding="utf-8")

if __name__ == "__main__":
    raise SystemExit(main())
