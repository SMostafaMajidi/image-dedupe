#!/usr/bin/env python3
"""Bulk-import Wisgoon post images into Qdrant (CLIP embeddings).

Reads MySQL DSN from the *main* Wisgoon SDK config YAML only
(``Documents/wisgoon/sdk/configs/...``) — does not import or call the SDK.
SQL + HTTP download + embed/upsert are implemented here.

Examples:
  # smoke test (5 posts)
  QDRANT_URL=http://127.0.0.1:6333 \\
    python scripts/import_wisgoon_posts.py --limit 5 --workers 8

  # full run (~500k)
  QDRANT_URL=http://127.0.0.1:6333 \\
    python scripts/import_wisgoon_posts.py --limit 500000 --workers 8
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
import pymysql
import yaml
from dotenv import load_dotenv
from pymysql.cursors import DictCursor

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import VectorDB  # noqa: E402
from app.embedding import InvalidImageError, get_embedder  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("import_wisgoon")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("app.db").setLevel(logging.WARNING)

GO_DSN_RE = re.compile(r"^([^:]+):([^@]+)@tcp\(([^:]+):(\d+)\)/([^?]+)")
DEFAULT_SDK_CONFIG = Path(
    "/home/mostafa/Documents/wisgoon/sdk/configs/production/connections.yaml"
)
DEFAULT_DSN_KEY = "read_db"


@dataclass(frozen=True)
class MysqlConfig:
    host: str
    port: int
    user: str
    password: str
    database: str


@dataclass(frozen=True)
class PostRow:
    id: int
    uid: str
    image: str


def load_mysql_config(config_path: Path, dsn_key: str = "main_db") -> MysqlConfig:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or dsn_key not in raw:
        raise SystemExit(f"missing {dsn_key!r} in {config_path}")
    m = GO_DSN_RE.match(str(raw[dsn_key]).strip())
    if not m:
        raise SystemExit(f"cannot parse Go MySQL DSN for {dsn_key}")
    user, password, host, port, database = m.groups()
    return MysqlConfig(
        host=host,
        port=int(port),
        user=user,
        password=password,
        database=database,
    )


def make_image_url(raw: str) -> str:
    """Mirror sdk/utils/tools/url.go MakeImageURL."""
    if "cdn.wisgoon.com" in raw:
        parsed = urlparse(raw)
        qs = parse_qs(parsed.query)
        bucket = (qs.get("b") or [""])[0]
        file_key = (qs.get("f") or [""])[0]
        return f"https://cdn-tehran.wisgoon.com/{bucket}/{file_key}"
    if "http" in raw:
        return raw
    return urljoin("https://photos01.wisgoon.com/media/", raw)


def connect_mysql(cfg: MysqlConfig) -> pymysql.Connection:
    return pymysql.connect(
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password,
        database=cfg.database,
        charset="utf8mb4",
        connect_timeout=10,
        read_timeout=120,
        write_timeout=60,
        cursorclass=DictCursor,
        autocommit=True,
    )


def iter_image_posts(
    cfg: MysqlConfig,
    *,
    limit: int,
    batch_size: int = 500,
    max_id: int | None = None,
) -> Iterator[PostRow]:
    """Keyset pagination over IMAGE posts (FORCE INDEX for speed)."""
    conn = connect_mysql(cfg)
    fetched = 0
    cursor_id = max_id if max_id is not None else 2**63 - 1
    try:
        with conn.cursor() as cur:
            while fetched < limit:
                need = min(batch_size, limit - fetched)
                cur.execute(
                    """
                    SELECT id, uid, image
                    FROM pin_post FORCE INDEX (pin_post_content_type_IDX)
                    WHERE content_type = 'IMAGE'
                      AND status = 1
                      AND image IS NOT NULL
                      AND image != ''
                      AND id < %s
                    ORDER BY id DESC
                    LIMIT %s
                    """,
                    (cursor_id, need),
                )
                rows = cur.fetchall()
                if not rows:
                    break
                for row in rows:
                    yield PostRow(id=int(row["id"]), uid=str(row["uid"]), image=str(row["image"]))
                    fetched += 1
                    if fetched >= limit:
                        break
                cursor_id = int(rows[-1]["id"])
    finally:
        conn.close()


def resolve_qdrant_url() -> None:
    """Host-side scripts cannot resolve Compose hostname ``qdrant``."""
    url = os.getenv("QDRANT_URL", "")
    if not url:
        os.environ["QDRANT_URL"] = "http://127.0.0.1:6333"
        return
    parsed = urlparse(url)
    if parsed.hostname == "qdrant":
        port = parsed.port or 6333
        fixed = f"http://127.0.0.1:{port}"
        log.warning("QDRANT_URL=%s not reachable from host; using %s", url, fixed)
        os.environ["QDRANT_URL"] = fixed


def download_image(client: httpx.Client, url: str, max_bytes: int) -> bytes:
    with client.stream("GET", url) as resp:
        resp.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_bytes(64 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"image larger than {max_bytes} bytes")
            chunks.append(chunk)
        data = b"".join(chunks)
    if not data:
        raise ValueError("empty image body")
    return data


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Import Wisgoon IMAGE posts into Qdrant")
    p.add_argument("--limit", type=int, default=500_000, help="max posts to import")
    p.add_argument("--workers", type=int, default=8, help="download/embed worker threads")
    p.add_argument("--batch-size", type=int, default=500, help="MySQL page size")
    p.add_argument(
        "--config",
        type=Path,
        default=Path(os.getenv("WISGOON_CONNECTIONS_YAML", str(DEFAULT_SDK_CONFIG))),
        help="path to Wisgoon SDK connections.yaml",
    )
    p.add_argument(
        "--dsn-key",
        default=os.getenv("WISGOON_DSN_KEY", DEFAULT_DSN_KEY),
        help="connections.yaml key (read_db|main_db)",
    )
    p.add_argument("--max-id", type=int, default=None, help="start below this pin_post.id")
    p.add_argument("--dry-run", action="store_true", help="fetch+download only, no embed/upsert")
    p.add_argument(
        "--max-upload-bytes",
        type=int,
        default=int(os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024))),
    )
    return p.parse_args()


def main() -> int:
    load_dotenv(ROOT / ".env")
    args = parse_args()
    resolve_qdrant_url()

    if not args.config.is_file():
        log.error("SDK config not found: %s", args.config)
        return 1

    mysql_cfg = load_mysql_config(args.config, args.dsn_key)
    log.info(
        "mysql=%s:%s/%s key=%s limit=%d workers=%d dry_run=%s",
        mysql_cfg.host,
        mysql_cfg.port,
        mysql_cfg.database,
        args.dsn_key,
        args.limit,
        args.workers,
        args.dry_run,
    )

    db: VectorDB | None = None
    embedder = None
    embed_lock = threading.Lock()
    if not args.dry_run:
        db = VectorDB()
        db.ensure_collection()
        embedder = get_embedder()
        embedder.load()

    ok = 0
    fail = 0
    skip = 0
    lock = threading.Lock()
    t0 = time.time()

    http_client = httpx.Client(
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
        headers={"User-Agent": "image-dedupe-import/0.4"},
        limits=httpx.Limits(max_connections=args.workers * 2, max_keepalive_connections=args.workers),
    )

    def process(post: PostRow) -> str:
        url = make_image_url(post.image)
        try:
            data = download_image(http_client, url, args.max_upload_bytes)
        except Exception as exc:
            log.debug("download fail uid=%s: %s", post.uid, exc)
            return "fail_download"

        if args.dry_run:
            return "ok"

        assert embedder is not None and db is not None
        try:
            with embed_lock:
                vector = embedder.extract(data)
        except InvalidImageError:
            return "skip_invalid"
        except Exception as exc:
            log.debug("embed fail uid=%s: %s", post.uid, exc)
            return "fail_embed"

        try:
            db.upsert_vector(post.uid, vector)
        except Exception as exc:
            log.debug("upsert fail uid=%s: %s", post.uid, exc)
            return "fail_upsert"
        return "ok"

    def handle(result: str) -> None:
        nonlocal ok, fail, skip
        with lock:
            if result == "ok":
                ok += 1
            elif result.startswith("skip"):
                skip += 1
            else:
                fail += 1
            done = ok + fail + skip
            if done % 50 == 0 or done <= 5:
                rate = done / max(time.time() - t0, 1e-6)
                log.info(
                    "progress done=%d ok=%d fail=%d skip=%d rate=%.1f/s",
                    done,
                    ok,
                    fail,
                    skip,
                    rate,
                )

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
    finally:
        http_client.close()

    elapsed = time.time() - t0
    log.info(
        "DONE ok=%d fail=%d skip=%d elapsed=%.1fs (%.2f/s)",
        ok,
        fail,
        skip,
        elapsed,
        (ok + fail + skip) / max(elapsed, 1e-6),
    )
    return 0 if ok > 0 or args.dry_run else 2


if __name__ == "__main__":
    sys.exit(main())
