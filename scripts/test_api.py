"""Phase 3 smoke test: hit /embed and /dedupe against a running API.

Usage:
  uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
  python scripts/test_api.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = ROOT / "sample_images"
BASE = "http://127.0.0.1:8000"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("phase3")

SAMPLES = [
    ("api_post_1", "sunset.jpg"),
    ("api_post_2", "sunset_bright.jpg"),
    ("api_post_3", "ocean.jpg"),
    ("api_post_4", "ocean_crop.jpg"),
    ("api_post_5", "forest.jpg"),
    ("api_post_6", "city.jpg"),
]


def main() -> int:
    if not SAMPLE_DIR.exists():
        log.error("sample images missing — run: python scripts/generate_samples.py")
        return 1

    with httpx.Client(base_url=BASE, timeout=120.0) as client:
        try:
            health = client.get("/health")
            health.raise_for_status()
        except httpx.HTTPError as exc:
            log.error("API not reachable at %s — start uvicorn first (%s)", BASE, exc)
            return 1

        print("health:", health.json())

        for post_uid, filename in SAMPLES:
            path = SAMPLE_DIR / filename
            with path.open("rb") as fh:
                resp = client.post(
                    "/embed",
                    data={"post_uid": post_uid},
                    files={"image": (filename, fh, "image/jpeg")},
                )
            if resp.status_code != 200:
                log.error("embed failed %s: %s %s", post_uid, resp.status_code, resp.text)
                return 2
            body = resp.json()
            print("embed:", body)
            if body.get("status") != "stored":
                log.error("expected status=stored")
                return 2

        # wrong content-type
        bad_ct = client.post(
            "/embed",
            data={"post_uid": "bad_ct"},
            files={"image": ("x.bin", b"not-an-image", "application/octet-stream")},
        )
        print("bad content-type status:", bad_ct.status_code, bad_ct.json())
        if bad_ct.status_code != 400:
            log.error("expected 400 for non-image content-type")
            return 2

        # image/* content-type but corrupt bytes
        bad_img = client.post(
            "/embed",
            data={"post_uid": "bad_img"},
            files={"image": ("x.jpg", b"not-an-image", "image/jpeg")},
        )
        print("corrupt image status:", bad_img.status_code, bad_img.json())
        if bad_img.status_code != 400:
            log.error("expected 400 for corrupt image")
            return 2

        # empty dedupe list
        empty = client.post("/dedupe", json={"post_uids": []})
        print("empty dedupe status:", empty.status_code, empty.json())
        if empty.status_code != 400:
            log.error("expected 400 for empty list")
            return 2

        # missing uid reported (HTTP 200), not 404
        mixed = client.post(
            "/dedupe",
            json={"post_uids": [uid for uid, _ in SAMPLES] + ["no_such_post"]},
        )
        if mixed.status_code != 200:
            log.error("dedupe failed: %s %s", mixed.status_code, mixed.text)
            return 2
        body = mixed.json()
        print("dedupe:", body)

        if body.get("missing_post_uids") != ["no_such_post"]:
            log.error("expected missing_post_uids=['no_such_post'], got %s", body.get("missing_post_uids"))
            return 2

        expected_unique = ["api_post_1", "api_post_3", "api_post_5", "api_post_6"]
        if body["unique_post_uids"] != expected_unique:
            log.error(
                "unique mismatch: expected %s got %s",
                expected_unique,
                body["unique_post_uids"],
            )
            return 2

        expected_removed = {"api_post_2", "api_post_4"}
        if set(body["removed_post_uids"]) != expected_removed:
            log.error(
                "removed mismatch: expected %s got %s",
                expected_removed,
                body["removed_post_uids"],
            )
            return 2

        expected_groups = [
            ["api_post_1", "api_post_2"],
            ["api_post_3", "api_post_4"],
            ["api_post_5"],
            ["api_post_6"],
        ]
        if body["groups"] != expected_groups:
            log.error("groups mismatch: expected %s got %s", expected_groups, body["groups"])
            return 2

    log.info("Phase 3 SUCCESS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
