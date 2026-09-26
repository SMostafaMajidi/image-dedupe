#!/usr/bin/env python3
"""Stage 1 (REL-DUP4): ensure ``image_phash`` keyword mapping on the post index.

Idempotent. Safe to re-run. Does not write any documents.

  ELASTIC_URL=http://192.168.20.208:9200 \\
    python scripts/put_es_phash_mapping.py

Verify:
  curl -s "$ELASTIC_URL/wis-post-0.0.2-v3/_mapping/field/image_phash" | python3 -m json.tool
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = os.getenv("ELASTIC_URL", "http://192.168.20.208:9200")
DEFAULT_INDEX = os.getenv("ELASTIC_POST_INDEX", "wis-post-0.0.2-v3")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--index", default=DEFAULT_INDEX)
    args = p.parse_args()

    body = json.dumps(
        {"properties": {"image_phash": {"type": "keyword", "doc_values": True}}}
    ).encode()
    req = urllib.request.Request(
        f"{args.url.rstrip('/')}/{args.index}/_mapping",
        data=body,
        method="PUT",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(resp.read().decode())
    except urllib.error.HTTPError as exc:
        print(exc.read().decode(), file=sys.stderr)
        return 1

    check = urllib.request.urlopen(
        f"{args.url.rstrip('/')}/{args.index}/_mapping/field/image_phash",
        timeout=15,
    )
    print(check.read().decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
