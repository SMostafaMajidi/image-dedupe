#!/usr/bin/env python3
"""Stage 2 (REL-DUP4): compute pHash for IMAGE posts and write to Elasticsearch.

No CLIP — download + pHash + ES partial update only. Much faster than
``import_wisgoon_posts.py``.

Examples (testable stages):
  # smoke — 20 newest IMAGE posts
  python scripts/backfill_phash_es.py --limit 20 --workers 8

  # ~1% of ~60M ≈ 600k (order-of-magnitude hours, see README)
  python scripts/backfill_phash_es.py --limit 600000 --workers 32

  # dry-run (download+hash, no ES write)
  python scripts/backfill_phash_es.py --limit 50 --dry-run

Verify one doc:
  curl -s "$ELASTIC_URL/wis-post-0.0.2-v3/_doc/<POST_ID>?_source_includes=image_phash,id"
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.phash import InvalidImageError, compute_phash  # noqa: E402
from scripts.import_wisgoon_posts import (  # noqa: E402
    DEFAULT_DSN_KEY,
    DEFAULT_SDK_CONFIG,
    MysqlConfig,
    PostRow,
    download_image,
    iter_image_posts,
    load_mysql_config,
    make_image_url,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backfill_phash_es")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

load_dotenv(ROOT / ".env")

DEFAULT_ES_URL = os.getenv("ELASTIC_URL", "http://192.168.20.208:9200")
DEFAULT_ES_INDEX = os.getenv("ELASTIC_POST_INDEX", "wis-post-0.0.2-v3")


def es_bulk_update(
    client: httpx.Client,
    base_url: str,
    index: str,
    items: list[tuple[int, str]],
) -> tuple[int, int]:
    """Partial-update ``image_phash`` via ES _bulk. Returns (ok, fail)."""
    if not items:
        return 0, 0
    lines: list[str] = []
    for post_id, phash in items:
        lines.append(json.dumps({"update": {"_index": index, "_id": str(post_id)}}))
        # doc-only update: preserves other fields; skip if doc missing
        lines.append(
            json.dumps(
                {
                    "doc": {"image_phash": phash},
                    "doc_as_upsert": False,
                }
            )
        )
    payload = "\n".join(lines) + "\n"
    resp = client.post(
        f"{base_url.rstrip('/')}/_bulk",
        content=payload.encode(),
        headers={"Content-Type": "application/x-ndjson"},
        timeout=60.0,
    )
    resp.raise_for_status()
    body: dict[str, Any] = resp.json()
    ok = 0
    fail = 0
    for item in body.get("items", []):
        upd = item.get("update", {})
        status = int(upd.get("status", 500))
        # 404 = post not in ES index (MySQL-only) — count as skip/fail soft
        if 200 <= status < 300:
            ok += 1
        else:
            fail += 1
            if fail <= 5:
                log.warning(
                    "bulk update fail id=%s status=%s result=%s",
                    upd.get("_id"),
                    status,
                    upd.get("result") or upd.get("error"),
                )
    return ok, fail


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=20, help="max posts to process")
    p.add_argument("--workers", type=int, default=16, help="download/hash threads")
    p.add_argument("--batch-size", type=int, default=500, help="MySQL page size")
    p.add_argument("--flush-size", type=int, default=100, help="ES bulk flush size")
    p.add_argument("--max-id", type=int, default=None, help="start below this post id")
    p.add_argument("--dry-run", action="store_true", help="hash only; do not write ES")
    p.add_argument(
        "--elastic-url",
        default=DEFAULT_ES_URL,
        help="Elasticsearch base URL",
    )
    p.add_argument("--index", default=DEFAULT_ES_INDEX, help="post index name")
    p.add_argument(
        "--config",
        type=Path,
        default=Path(os.getenv("WISGOON_CONNECTIONS_YAML", str(DEFAULT_SDK_CONFIG))),
    )
    p.add_argument(
        "--dsn-key",
        default=os.getenv("WISGOON_DSN_KEY", DEFAULT_DSN_KEY),
    )
    p.add_argument(
        "--max-upload-bytes",
        type=int,
        default=int(os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024))),
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    # Safety: never write phash to production cluster from this script unless
    # explicitly overridden (local experiments default).
    es_url = args.elastic_url.rstrip("/")
    prod_markers = ("192.168.20.208", "192.168.20.209", "192.168.20.210")
    allow_prod = os.getenv("ALLOW_PROD_ES_WRITE", "0") == "1"
    if any(m in es_url for m in prod_markers) and not allow_prod:
        log.error(
            "refusing to write to production ES (%s). "
            "Pass --elastic-url http://127.0.0.1:9201 for local, "
            "or set ALLOW_PROD_ES_WRITE=1 only when intentionally backfilling prod.",
            es_url,
        )
        return 2

    mysql_cfg = load_mysql_config(args.config, args.dsn_key)
    log.info(
        "mysql=%s:%s/%s es=%s index=%s limit=%d workers=%d dry_run=%s",
        mysql_cfg.host,
        mysql_cfg.port,
        mysql_cfg.database,
        args.elastic_url,
        args.index,
        args.limit,
        args.workers,
        args.dry_run,
    )

    ok = 0
    fail = 0
    skip = 0
    lock = threading.Lock()
    pending: list[tuple[int, str]] = []
    t0 = time.time()

    http_client = httpx.Client(
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
        headers={"User-Agent": "image-dedupe-phash-es/0.1"},
        limits=httpx.Limits(
            max_connections=args.workers * 2,
            max_keepalive_connections=args.workers,
        ),
    )
    es_client = httpx.Client(
        timeout=httpx.Timeout(60.0, connect=10.0),
        headers={"User-Agent": "image-dedupe-phash-es/0.1"},
    )

    def flush_pending(force: bool = False) -> None:
        nonlocal ok, fail, pending
        with lock:
            if args.dry_run or not pending:
                return
            if not force and len(pending) < args.flush_size:
                return
            batch, pending = pending, []
        try:
            b_ok, b_fail = es_bulk_update(
                es_client, args.elastic_url, args.index, batch
            )
        except Exception as exc:
            log.warning("bulk flush error (%d items): %s", len(batch), exc)
            with lock:
                fail += len(batch)
            return
        with lock:
            ok += b_ok
            fail += b_fail

    def process(post: PostRow) -> tuple[str, int | None, str | None]:
        url = make_image_url(post.image)
        try:
            data = download_image(http_client, url, args.max_upload_bytes)
        except Exception:
            return "fail_download", None, None
        try:
            phash = compute_phash(data)
        except InvalidImageError:
            return "skip_invalid", None, None
        except Exception:
            return "fail_hash", None, None
        return "hashed", post.id, phash

    def handle(result: tuple[str, int | None, str | None]) -> None:
        nonlocal ok, fail, skip, pending
        status, post_id, phash = result
        should_flush = False
        with lock:
            if status.startswith("skip"):
                skip += 1
            elif status.startswith("fail"):
                fail += 1
            elif status == "hashed" and post_id is not None and phash is not None:
                if args.dry_run:
                    ok += 1
                else:
                    pending.append((post_id, phash))
                    should_flush = len(pending) >= args.flush_size
            done = ok + fail + skip
            if done % 50 == 0 or done <= 5 or (done + len(pending)) <= 5:
                rate = max(done, 1) / max(time.time() - t0, 1e-6)
                log.info(
                    "progress hashed_or_terminal=%d ok=%d fail=%d skip=%d pending=%d rate≈%.1f/s",
                    done + len(pending),
                    ok,
                    fail,
                    skip,
                    len(pending),
                    rate,
                )
        if should_flush:
            flush_pending(force=True)

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            inflight: set = set()
            for post in iter_image_posts(
                mysql_cfg,
                limit=args.limit,
                batch_size=args.batch_size,
                max_id=args.max_id,
            ):
                inflight.add(pool.submit(process, post))
                if len(inflight) >= args.workers * 4:
                    done_set, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                    for fut in done_set:
                        handle(fut.result())
            while inflight:
                done_set, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                for fut in done_set:
                    handle(fut.result())
        flush_pending(force=True)
    finally:
        http_client.close()
        es_client.close()

    elapsed = time.time() - t0
    total = ok + fail + skip
    log.info(
        "DONE ok=%d fail=%d skip=%d elapsed=%.1fs (%.2f/s)",
        ok,
        fail,
        skip,
        elapsed,
        total / max(elapsed, 1e-6),
    )
    # Print ETA helpers for planning larger runs
    if total > 0 and elapsed > 0:
        rate = total / elapsed
        for label, n in (("1%", 600_000), ("10%", 6_000_000), ("43M ES", 43_000_000)):
            hours = n / rate / 3600
            log.info("ETA at this rate for %s (~%s posts): %.1f hours", label, f"{n:,}", hours)
    return 0 if ok > 0 or args.dry_run else 2


if __name__ == "__main__":
    raise SystemExit(main())
