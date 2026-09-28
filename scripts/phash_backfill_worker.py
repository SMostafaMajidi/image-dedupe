#!/usr/bin/env python3
"""Resumable 256-bit pHash backfill for IMAGE + VIDEO posts (runs in Docker).

Walks ``pin_post`` from the newest id at first start down to the oldest,
hashes the post image (thumbnail for VIDEO) and partial-updates ES
``image_phash``. Posts created after the first start are hashed by the search
consumer, so they are not visited.

Resume: ``$STATE_DIR/state.json`` holds the cursor. It only moves after a whole
batch is written to ES, so a crash / reboot / ``docker stop`` redoes at most
one batch. Network outages (CDN, MySQL, ES) pause the run instead of skipping
posts.

Commands:
  run      (default) backfill until done, then idle
  status   print progress from state.json
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
import pymysql
import yaml
from pymysql.cursors import DictCursor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.phash import (  # noqa: E402
    FLAT_PHASH,
    MIN_DETAIL,
    LowDetailImageError,
    compute_phashes,
)

HASH_SIZE = 16
PROD_ES_MARKERS = ("192.168.20.208", "192.168.20.209", "192.168.20.210")
GO_DSN_RE = re.compile(r"^([^:]+):([^@]+)@tcp\(([^:]+):(\d+)\)/([^?]+)")


def env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


ES_URL = env("ES_URL", "http://192.168.20.208:9200").rstrip("/")
ES_INDEX = env("ES_INDEX", "wis-post-0.0.2-v3")
ES_FIELD = env("ES_FIELD", "image_phash")
MYSQL_CONFIG = Path(env("MYSQL_CONFIG", "/config/connections.yaml"))
MYSQL_DSN_KEY = env("MYSQL_DSN_KEY", "read_db")
STATE_DIR = Path(env("STATE_DIR", "/data"))
WORKERS = int(env("WORKERS", "28"))
BATCH = int(env("BATCH", "1000"))
BULK_CHUNK = int(env("BULK_CHUNK", "500"))
MIN_ID = int(env("MIN_ID", "0"))
START_MAX_ID = env("START_MAX_ID", "")
LOG_EVERY_S = int(env("LOG_EVERY_S", "300"))
TIMEOUT_S = float(env("DOWNLOAD_TIMEOUT_S", "15"))
MAX_IMAGE_BYTES = int(env("MAX_IMAGE_BYTES", str(15 * 1024 * 1024)))
STOP_AFTER_BATCHES = int(env("STOP_AFTER_BATCHES", "0"))  # testing only
# Active IMAGE+VIDEO posts per id (ES: ~40.0M over ~82.4M ids); ETA / percent only.
POST_DENSITY = float(env("POST_DENSITY", "0.486"))

STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "backfill.log"

log = logging.getLogger("phash_backfill")
stop_event = threading.Event()


class Retryable(Exception):
    """Whole batch must be redone later (outage)."""


def setup_logging() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    log.setLevel(logging.INFO)
    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(fmt)
    log.addHandler(out)
    # small on purpose: state.json is the source of truth for progress
    fh = RotatingFileHandler(LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------- state


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_state() -> dict[str, Any] | None:
    if not STATE_FILE.is_file():
        return None
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(STATE_FILE)


def fmt_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "?"
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return f"{d}d {h}h" if d else f"{h}h {m}m"


def progress_line(state: dict[str, Any]) -> str:
    t = state["totals"]
    return (
        f"cursor={state['cursor']:,} (posts from {state.get('cursor_time') or '?'}) "
        f"done={state.get('percent', 0):.2f}% hashed={t['hashed']:,} flat={t['flat']:,} "
        f"not_in_es={t['not_in_es']:,} bad_image={t['bad_image']:,} download_fail={t['download_fail']:,} "
        f"rate={state.get('rate_per_s', 0):.1f}/s eta={fmt_duration(state.get('eta_s'))} "
        f"status={state['status']}"
    )


# ---------------------------------------------------------------- mysql


def mysql_connect() -> pymysql.Connection:
    raw = yaml.safe_load(MYSQL_CONFIG.read_text(encoding="utf-8"))
    m = GO_DSN_RE.match(str(raw[MYSQL_DSN_KEY]).strip())
    if not m:
        raise SystemExit(f"cannot parse DSN {MYSQL_DSN_KEY} in {MYSQL_CONFIG}")
    user, password, host, port, database = m.groups()
    return pymysql.connect(
        host=host, port=int(port), user=user, password=password, database=database,
        charset="utf8mb4", connect_timeout=10, read_timeout=60, write_timeout=60,
        cursorclass=DictCursor, autocommit=True,
    )


class Posts:
    def __init__(self) -> None:
        self.conn: pymysql.Connection | None = None

    def _cursor(self):
        if self.conn is None:
            self.conn = mysql_connect()
        return self.conn.cursor()

    def _query(self, sql: str, args: tuple) -> list[dict[str, Any]]:
        try:
            with self._cursor() as c:
                c.execute(sql, args)
                return list(c.fetchall())
        except pymysql.MySQLError as exc:
            if self.conn is not None:
                try:
                    self.conn.close()
                except Exception:
                    pass
            self.conn = None
            raise Retryable(f"mysql: {exc}") from exc

    def max_id(self) -> int:
        rows = self._query("SELECT id FROM pin_post ORDER BY id DESC LIMIT 1", ())
        return int(rows[0]["id"]) if rows else 0

    def batch_below(self, cursor: int) -> list[dict[str, Any]]:
        # PRIMARY range scan; IMAGE+VIDEO are nearly all rows
        return self._query(
            """
            SELECT id, image, timestamp FROM pin_post FORCE INDEX (PRIMARY)
            WHERE id < %s AND id >= %s
              AND content_type IN ('IMAGE', 'VIDEO') AND status = 1
              AND image IS NOT NULL AND image != ''
            ORDER BY id DESC LIMIT %s
            """,
            (cursor, MIN_ID, BATCH),
        )


# ---------------------------------------------------------------- hashing


def make_image_url(raw: str) -> str:
    """Mirror sdk/utils/tools/url.go MakeImageURL."""
    if "cdn.wisgoon.com" in raw:
        qs = parse_qs(urlparse(raw).query)
        return f"https://cdn-tehran.wisgoon.com/{(qs.get('b') or [''])[0]}/{(qs.get('f') or [''])[0]}"
    if "http" in raw:
        return raw
    return urljoin("https://photos01.wisgoon.com/media/", raw)


def fetch(client: httpx.Client, url: str) -> tuple[str, bytes | None]:
    """-> ("ok", data) | ("bad", None) permanent | ("transient", None)."""
    for attempt in range(3):
        if attempt:
            time.sleep(2 * attempt)
        try:
            r = client.get(url)
        except httpx.InvalidURL:
            return "bad", None
        except httpx.HTTPError:
            continue
        if r.status_code == 200:
            if len(r.content) > MAX_IMAGE_BYTES:
                return "bad", None
            return "ok", r.content
        if r.status_code == 429 or r.status_code >= 500:
            continue
        return "bad", None
    return "transient", None


def hash_post(client: httpx.Client, row: dict[str, Any]) -> tuple[int, str, str | None]:
    """-> (id, kind, value); kind in hashed | flat | bad_image | transient."""
    pid = int(row["id"])
    try:
        url = make_image_url(str(row["image"]).strip())
    except Exception:
        return pid, "bad_image", None
    status, data = fetch(client, url)
    if status != "ok":
        return pid, ("transient" if status == "transient" else "bad_image"), None
    try:
        h = compute_phashes(data, (HASH_SIZE,), MIN_DETAIL)[HASH_SIZE]
    except LowDetailImageError:
        return pid, "flat", FLAT_PHASH
    except Exception:  # InvalidImageError or decoder bugs
        return pid, "bad_image", None
    return pid, "hashed", h


# ---------------------------------------------------------------- elasticsearch


def es_bulk(es: httpx.Client, docs: list[tuple[int, str]]) -> int:
    """Partial-update image_phash; returns count of docs missing from the index."""
    missing = 0
    for i in range(0, len(docs), BULK_CHUNK):
        chunk = docs[i : i + BULK_CHUNK]
        lines = []
        for pid, value in chunk:
            lines.append(json.dumps({"update": {"_index": ES_INDEX, "_id": str(pid), "retry_on_conflict": 3}}))
            lines.append(json.dumps({"doc": {ES_FIELD: value}}))
        try:
            r = es.post(
                f"{ES_URL}/_bulk",
                content="\n".join(lines) + "\n",
                headers={"Content-Type": "application/x-ndjson"},
            )
        except httpx.HTTPError as exc:
            raise Retryable(f"es: {exc}") from exc
        if r.status_code != 200:
            raise Retryable(f"es bulk http {r.status_code}: {r.text[:200]}")
        body = r.json()
        if not body.get("errors"):
            continue
        for item in body.get("items", []):
            st = item.get("update", {}).get("status", 0)
            if st == 404:
                missing += 1
            elif st >= 300:
                raise Retryable(f"es item status {st}: {str(item)[:200]}")
    return missing


# ---------------------------------------------------------------- main loop


def new_state(start_max_id: int) -> dict[str, Any]:
    return {
        "start_max_id": start_max_id,
        "cursor": start_max_id,
        "cursor_time": None,
        "min_id": MIN_ID,
        "status": "running",
        "started_at": now_iso(),
        "finished_at": None,
        "percent": 0.0,
        "rate_per_s": 0.0,
        "eta_s": None,
        "last_error": None,
        "totals": {"hashed": 0, "flat": 0, "not_in_es": 0, "bad_image": 0, "download_fail": 0},
    }


def post_time(ts: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return None


def idle_forever() -> None:
    while not stop_event.wait(3600):
        pass


def run() -> int:
    setup_logging()
    if any(m in ES_URL for m in PROD_ES_MARKERS) and os.getenv("ALLOW_PROD_ES_WRITE") != "1":
        log.error("refusing to write to production ES %s without ALLOW_PROD_ES_WRITE=1", ES_URL)
        return 2

    posts = Posts()
    state = load_state()
    backoff = 30.0
    while state is None and not stop_event.is_set():
        try:
            start = int(START_MAX_ID) if START_MAX_ID else posts.max_id() + 1
            state = new_state(start)
            save_state(state)
            log.info("first start: walking posts below id %d down to %d", start, MIN_ID)
        except Retryable as exc:
            log.warning("cannot read max id (%s); retry in %ds", exc, backoff)
            stop_event.wait(backoff)
    if state is None:
        return 0

    if state["status"] == "done":
        log.info("already done: %s", progress_line(state))
        idle_forever()
        return 0

    log.info("resume: %s", progress_line(state))
    http = httpx.Client(
        timeout=httpx.Timeout(TIMEOUT_S, connect=10.0),
        follow_redirects=True,
        headers={"User-Agent": "image-dedupe-phash-backfill/1.0"},
        limits=httpx.Limits(max_connections=WORKERS * 2, max_keepalive_connections=WORKERS),
    )
    es = httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0))
    pool = ThreadPoolExecutor(max_workers=WORKERS)

    expected_posts = max((state["start_max_id"] - MIN_ID) * POST_DENSITY, 1.0)
    window: list[tuple[float, int]] = []  # (time, posts) for rate / ETA
    last_log = 0.0
    last_outage_log = 0.0
    batches = 0
    backoff = 30.0

    while not stop_event.is_set():
        try:
            rows = posts.batch_below(state["cursor"])
            if not rows:
                state.update(status="done", finished_at=now_iso(), percent=100.0, eta_s=0, cursor=MIN_ID)
                save_state(state)
                log.info("DONE: %s", progress_line(state))
                break

            results = list(pool.map(lambda r: hash_post(http, r), rows))
            if stop_event.is_set():
                break
            transient = sum(1 for _, k, _ in results if k == "transient")
            if transient >= 20 and transient > len(results) * 0.3:
                raise Retryable(f"image downloads failing ({transient}/{len(results)} in batch)")

            docs = [(pid, v) for pid, k, v in results if k in ("hashed", "flat") and v]
            missing = es_bulk(es, docs)

            t = state["totals"]
            for _, k, _ in results:
                if k == "hashed":
                    t["hashed"] += 1
                elif k == "flat":
                    t["flat"] += 1
                elif k == "bad_image":
                    t["bad_image"] += 1
                else:
                    t["download_fail"] += 1
            t["not_in_es"] += missing
            state["cursor"] = int(rows[-1]["id"])
            state["cursor_time"] = post_time(rows[-1].get("timestamp"))
            processed = sum(t[k] for k in ("hashed", "flat", "bad_image", "download_fail"))
            state["percent"] = round(min(100.0 * processed / expected_posts, 99.99), 2)
            state["status"] = "running"
            state["last_error"] = None

            now = time.time()
            window.append((now, len(rows)))
            window = [w for w in window if now - w[0] <= 1800]
            elapsed = max(now - window[0][0], 1.0) if len(window) > 1 else None
            if elapsed:
                rate = sum(w[1] for w in window[1:]) / elapsed
                state["rate_per_s"] = round(rate, 2)
                remaining = (state["cursor"] - MIN_ID) * POST_DENSITY
                state["eta_s"] = int(remaining / rate) if rate > 0 else None
            save_state(state)
            backoff = 30.0

            if now - last_log >= LOG_EVERY_S:
                log.info(progress_line(state))
                last_log = now
            batches += 1
            if STOP_AFTER_BATCHES and batches >= STOP_AFTER_BATCHES:
                log.info("stop after %d batches: %s", batches, progress_line(state))
                break
        except Retryable as exc:
            state["status"] = "waiting (outage)"
            state["last_error"] = f"{now_iso()} {exc}"
            save_state(state)
            if time.time() - last_outage_log >= 300:
                log.warning("outage, cursor kept at %d: %s; retrying every %ds", state["cursor"], exc, int(backoff))
                last_outage_log = time.time()
            stop_event.wait(backoff)
            backoff = min(backoff * 2, 300.0)

    pool.shutdown(wait=False, cancel_futures=True)
    if state["status"] == "done":
        idle_forever()
    else:
        log.info("stopped: %s", progress_line(state))
    return 0


def status() -> int:
    state = load_state()
    if state is None:
        print(f"no state yet ({STATE_FILE})")
        return 1
    t = state["totals"]
    print(f"status        {state['status']}")
    print(f"started       {state['started_at']}  (from post id {state['start_max_id']:,} down to {state['min_id']:,})")
    print(f"cursor        {state['cursor']:,}  -> hashed back to posts from {state.get('cursor_time') or '?'}")
    print(f"progress      {state.get('percent', 0):.2f}%  rate {state.get('rate_per_s', 0):.1f} posts/s  eta {fmt_duration(state.get('eta_s'))}")
    print(f"hashed        {t['hashed']:,}   flat {t['flat']:,}")
    print(f"skipped       not_in_es {t['not_in_es']:,}  bad_image {t['bad_image']:,}  download_fail {t['download_fail']:,}")
    print(f"updated       {state.get('updated_at')}")
    if state.get("last_error"):
        print(f"last error    {state['last_error']}")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "status":
        return status()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop_event.set())
    return run()


if __name__ == "__main__":
    sys.exit(main())
