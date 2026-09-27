#!/usr/bin/env python3
"""Pick seed posts (with ``image_phash`` on ES) for the related-dedupe evaluation.

Read-only: ES _search/aggs + MySQL SELECT. Output feeds
``api-gateway-1402/tools/eval_related_dedupe``.

Groups:
  random   — uniform sample of posts that already have a pHash
  dupes    — posts whose exact pHash is shared by >=2 docs (likely reposts)

Example:
  .venv/bin/python scripts/sample_related_eval_seeds.py --random 150 --dupes 50
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.import_wisgoon_posts import (  # noqa: E402
    DEFAULT_DSN_KEY,
    DEFAULT_SDK_CONFIG,
    connect_mysql,
    load_mysql_config,
    make_image_url,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("sample_seeds")
logging.getLogger("httpx").setLevel(logging.WARNING)

load_dotenv(ROOT / ".env")

DEFAULT_ES_URL = os.getenv("ELASTIC_URL", "http://192.168.20.208:9200")
DEFAULT_ES_INDEX = os.getenv("ELASTIC_POST_INDEX", "wis-post-0.0.2-v3")


def es_search(client: httpx.Client, url: str, index: str, body: dict) -> dict:
    resp = client.post(f"{url.rstrip('/')}/{index}/_search", json=body, timeout=60.0)
    resp.raise_for_status()
    return resp.json()


def sample_random(client: httpx.Client, url: str, index: str, n: int, seed: int) -> list[int]:
    body = {
        "size": n,
        "_source": False,
        "query": {
            "function_score": {
                "query": {"exists": {"field": "image_phash"}},
                "random_score": {"seed": seed, "field": "_seq_no"},
            }
        },
    }
    hits = es_search(client, url, index, body)["hits"]["hits"]
    return [int(h["_id"]) for h in hits]


def sample_dupes(client: httpx.Client, url: str, index: str, n: int) -> list[int]:
    body = {
        "size": 0,
        "query": {"exists": {"field": "image_phash"}},
        "aggs": {
            "h": {
                "terms": {"field": "image_phash", "min_doc_count": 2, "size": max(n * 60, 3000)},
                "aggs": {"top": {"top_hits": {"size": 1, "_source": False, "sort": [{"id": "desc"}]}}},
            }
        },
    }
    buckets = es_search(client, url, index, body)["aggregations"]["h"]["buckets"]
    # skip "blank/solid" hashes that match thousands of unrelated images
    buckets = [b for b in buckets if 2 <= b["doc_count"] <= 50]
    random.shuffle(buckets)
    ids = []
    for b in buckets[:n]:
        hits = b["top"]["hits"]["hits"]
        if hits:
            ids.append(int(hits[0]["_id"]))
    log.info("dupe clusters used=%d (of %d candidate buckets)", len(ids), len(buckets))
    return ids


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--random", type=int, default=150)
    p.add_argument("--dupes", type=int, default=50)
    p.add_argument("--seed", type=int, default=20260927)
    p.add_argument("--elastic-url", default=DEFAULT_ES_URL)
    p.add_argument("--index", default=DEFAULT_ES_INDEX)
    p.add_argument("--config", type=Path, default=Path(os.getenv("WISGOON_CONNECTIONS_YAML", str(DEFAULT_SDK_CONFIG))))
    p.add_argument("--dsn-key", default=os.getenv("WISGOON_DSN_KEY", DEFAULT_DSN_KEY))
    p.add_argument("--out", type=Path, default=ROOT / ".tools" / "related_eval_seeds.json")
    args = p.parse_args()
    random.seed(args.seed)

    with httpx.Client() as es:
        groups = {
            "random": sample_random(es, args.elastic_url, args.index, args.random, args.seed),
            "dupes": sample_dupes(es, args.elastic_url, args.index, args.dupes),
        }

    all_ids = sorted({i for ids in groups.values() for i in ids})
    rows: dict[int, dict] = {}
    conn = connect_mysql(load_mysql_config(args.config, args.dsn_key))
    try:
        with conn.cursor() as cur:
            for i in range(0, len(all_ids), 500):
                chunk = all_ids[i : i + 500]
                cur.execute(
                    f"SELECT id, uid, image FROM pin_post WHERE id IN ({','.join(['%s'] * len(chunk))}) AND status = 1",
                    chunk,
                )
                for r in cur.fetchall():
                    rows[int(r["id"])] = r
    finally:
        conn.close()

    seeds = []
    for group, ids in groups.items():
        for pid in ids:
            r = rows.get(pid)
            if not r or not r["uid"]:
                continue
            seeds.append(
                {"id": pid, "uid": str(r["uid"]), "image": make_image_url(str(r["image"] or "")), "group": group}
            )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(seeds, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    log.info("wrote %d seeds (%s) → %s", len(seeds), {g: sum(s["group"] == g for s in seeds) for g in groups}, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
