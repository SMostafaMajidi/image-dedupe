#!/usr/bin/env python3
"""Export a small slice of real Wisgoon posts into LOCAL Elasticsearch (read-only prod).

Reads:
  - MySQL (read replica) for IMAGE post rows
  - Production ES (GET by id only) for the live document shape

Writes:
  - ONLY to LOCAL_ES_URL (default http://127.0.0.1:9201)

Never writes to production ES.

Examples:
  # seed + N newest IMAGE posts
  python scripts/export_posts_to_local_es.py --limit 200 --seed-uid JGIXGV3361

  # dry-run (print counts, no write)
  python scripts/export_posts_to_local_es.py --limit 20 --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.import_wisgoon_posts import (  # noqa: E402
    DEFAULT_DSN_KEY,
    DEFAULT_SDK_CONFIG,
    iter_image_posts,
    load_mysql_config,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("export_local_es")
load_dotenv(ROOT / ".env")

LOCAL_ES = os.getenv("LOCAL_ES_URL", "http://127.0.0.1:9201")
PROD_ES = os.getenv("ELASTIC_URL", "http://192.168.20.208:9200")
INDEX = os.getenv("ELASTIC_POST_INDEX", "wis-post-0.0.2-v3")


def http_json(method: str, url: str, body: dict | None = None, timeout: float = 120.0) -> Any:
    data = None if body is None else json.dumps(body).encode()
    req = Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else None


def ensure_local_index(local_url: str, index: str) -> None:
    base = local_url.rstrip("/")
    try:
        http_json("GET", f"{base}/{index}")
        log.info("local index exists: %s", index)
        return
    except HTTPError as exc:
        if exc.code != 404:
            raise
    # Minimal mapping compatible with related + image_phash experiments
    mapping = {
        "settings": {"number_of_shards": 1, "number_of_replicas": 0},
        "mappings": {
            "properties": {
                "id": {"type": "long"},
                "uid": {"type": "keyword"},
                "title": {"type": "text"},
                "text": {"type": "text"},
                "create": {"type": "date"},
                "content_type": {"type": "keyword"},
                "user_id": {"type": "long"},
                "category_id": {"type": "long"},
                "category": {"type": "keyword"},
                "status": {"type": "long"},
                "device": {"type": "long"},
                "hashtags": {"type": "keyword"},
                "username": {"type": "keyword"},
                "like_count": {"type": "long"},
                "choices": {"type": "boolean"},
                "is_shop": {"type": "boolean"},
                "shadow_ban": {"type": "boolean"},
                "banned": {"type": "boolean"},
                "image": {"type": "keyword", "index": False},
                "image_phash": {"type": "keyword"},
            }
        },
    }
    http_json("PUT", f"{base}/{index}", mapping)
    log.info("created local index %s", index)


def fetch_prod_doc(prod_url: str, index: str, post_id: int) -> dict | None:
    url = f"{prod_url.rstrip('/')}/{index}/_doc/{post_id}"
    try:
        doc = http_json("GET", url)
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if not doc or not doc.get("found"):
        return None
    src = doc.get("_source") or {}
    return src if isinstance(src, dict) else None


def fetch_seed_id(mysql_cfg, seed_uid: str) -> int | None:
    import pymysql
    from pymysql.cursors import DictCursor

    conn = pymysql.connect(
        host=mysql_cfg.host,
        port=mysql_cfg.port,
        user=mysql_cfg.user,
        password=mysql_cfg.password,
        database=mysql_cfg.database,
        charset="utf8mb4",
        connect_timeout=10,
        cursorclass=DictCursor,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM pin_post WHERE uid=%s LIMIT 1",
                (seed_uid,),
            )
            row = cur.fetchone()
            return int(row["id"]) if row else None
    finally:
        conn.close()


def bulk_index(local_url: str, index: str, docs: list[tuple[int, dict]]) -> tuple[int, int]:
    if not docs:
        return 0, 0
    lines: list[str] = []
    for post_id, src in docs:
        lines.append(json.dumps({"index": {"_index": index, "_id": str(post_id)}}))
        lines.append(json.dumps(src, default=str))
    payload = ("\n".join(lines) + "\n").encode()
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(
            f"{local_url.rstrip('/')}/_bulk",
            content=payload,
            headers={"Content-Type": "application/x-ndjson"},
        )
        resp.raise_for_status()
        body = resp.json()
    ok = fail = 0
    for item in body.get("items", []):
        st = int(item.get("index", {}).get("status", 500))
        if 200 <= st < 300:
            ok += 1
        else:
            fail += 1
    return ok, fail


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=200, help="max IMAGE posts from MySQL")
    p.add_argument("--seed-uid", default="JGIXGV3361", help="always include this post")
    p.add_argument("--local-es", default=LOCAL_ES)
    p.add_argument("--prod-es", default=PROD_ES, help="read-only source")
    p.add_argument("--index", default=INDEX)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--config",
        type=Path,
        default=Path(os.getenv("WISGOON_CONNECTIONS_YAML", str(DEFAULT_SDK_CONFIG))),
    )
    p.add_argument("--dsn-key", default=os.getenv("WISGOON_DSN_KEY", DEFAULT_DSN_KEY))
    return p.parse_args()


def main() -> int:
    args = parse_args()
    # Safety: refuse to write if local URL looks like the prod cluster
    local = args.local_es.rstrip("/")
    prod = args.prod_es.rstrip("/")
    if "192.168.20.208" in local or "192.168.20.209" in local or "192.168.20.210" in local:
        log.error("refusing to write: --local-es points at production (%s)", local)
        return 2
    if local == prod:
        log.error("refusing to write: local-es == prod-es (%s)", local)
        return 2

    mysql_cfg = load_mysql_config(args.config, args.dsn_key)
    log.info(
        "mysql=%s local_es=%s prod_es(read)=%s index=%s limit=%d dry_run=%s",
        mysql_cfg.host,
        local,
        prod,
        args.index,
        args.limit,
        args.dry_run,
    )

    # Health checks
    try:
        health = http_json("GET", f"{local}/")
        log.info("local ES name=%s version=%s", health.get("name"), health.get("version", {}).get("number"))
    except (HTTPError, URLError, TimeoutError) as exc:
        log.error("local ES not reachable at %s: %s", local, exc)
        log.error("start it: docker compose -f docker-compose.es-local.yml up -d")
        return 1

    if not args.dry_run:
        ensure_local_index(local, args.index)

    wanted: list[tuple[int, str, str]] = []  # id, uid, image
    seed_id = fetch_seed_id(mysql_cfg, args.seed_uid)
    if seed_id:
        log.info("seed %s → id=%s", args.seed_uid, seed_id)
    else:
        log.warning("seed uid %s not found in MySQL", args.seed_uid)

    for post in iter_image_posts(mysql_cfg, limit=args.limit, batch_size=min(200, args.limit)):
        wanted.append((post.id, post.uid, post.image))

    # Ensure seed is included even if outside the newest window
    if seed_id and all(p[0] != seed_id for p in wanted):
        import pymysql
        from pymysql.cursors import DictCursor

        conn = pymysql.connect(
            host=mysql_cfg.host,
            port=mysql_cfg.port,
            user=mysql_cfg.user,
            password=mysql_cfg.password,
            database=mysql_cfg.database,
            charset="utf8mb4",
            connect_timeout=10,
            cursorclass=DictCursor,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, uid, image FROM pin_post WHERE id=%s LIMIT 1",
                    (seed_id,),
                )
                row = cur.fetchone()
                if row:
                    wanted.insert(0, (int(row["id"]), str(row["uid"]), str(row["image"])))
        finally:
            conn.close()

    log.info("collecting %d posts from prod ES (GET only)…", len(wanted))
    batch: list[tuple[int, dict]] = []
    ok = miss = 0
    t0 = time.time()
    for post_id, uid, image in wanted:
        src = fetch_prod_doc(prod, args.index, post_id)
        if src is None:
            # fallback minimal doc from MySQL so local still has something
            src = {
                "id": post_id,
                "uid": uid,
                "image": image,
                "content_type": "IMAGE",
                "status": 1,
                "text": "",
                "title": "",
            }
            miss += 1
        else:
            src = dict(src)
            src.setdefault("uid", uid)
            src.setdefault("image", image)
            src.pop("image_phash", None)  # keep local clean; hash comes later
            ok += 1
        batch.append((post_id, src))
        if not args.dry_run and len(batch) >= 50:
            b_ok, b_fail = bulk_index(local, args.index, batch)
            log.info("flushed bulk ok=%d fail=%d", b_ok, b_fail)
            batch = []

    if args.dry_run:
        log.info("DRY-RUN would index %d docs (prod_hit≈%d mysql_only≈%d)", len(wanted), ok, miss)
        return 0

    if batch:
        b_ok, b_fail = bulk_index(local, args.index, batch)
        log.info("final flush ok=%d fail=%d", b_ok, b_fail)

    # refresh + count
    http_json("POST", f"{local}/{args.index}/_refresh")
    count = http_json("GET", f"{local}/{args.index}/_count")
    log.info(
        "DONE local_count=%s prod_docs=%d mysql_fallback=%d elapsed=%.1fs",
        count.get("count"),
        ok,
        miss,
        time.time() - t0,
    )
    log.info("sample: curl -s %s/%s/_search?size=1 | python3 -m json.tool", local, args.index)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
