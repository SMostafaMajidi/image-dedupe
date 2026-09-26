#!/usr/bin/env python3
"""Minimal Elasticsearch-compatible stub for LOCAL REL-DUP experiments.

Use when Docker cannot pull the official ES image (proxy issues). Speaks enough
of the ES HTTP API for our export script + gateway mget filter:

  GET  /                         cluster info
  PUT  /{index}                  create index (ignored body OK)
  GET  /{index}
  POST /{index}/_refresh
  GET  /{index}/_count
  GET  /{index}/_doc/{id}
  POST /{index}/_doc/{id}  / PUT
  POST /_bulk                    NDJSON index/update
  GET|POST /_mget
  GET|POST /{index}/_search      returns stored docs (optional query exists)

Data dir: .tools/local-es-data/  (gitignored)

  .venv/bin/uvicorn scripts.local_es_stub:app --host 127.0.0.1 --port 9201
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

DATA = Path(__file__).resolve().parent.parent / ".tools" / "local-es-data"
DATA.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="local-es-stub", version="0.1.0")


def index_path(index: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", index)
    return DATA / f"{safe}.json"


def load_index(index: str) -> dict[str, dict]:
    path = index_path(index)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_index(index: str, docs: dict[str, dict]) -> None:
    path = index_path(index)
    path.write_text(json.dumps(docs, ensure_ascii=False, indent=None), encoding="utf-8")


@app.get("/")
def root() -> dict:
    return {
        "name": "image-dedupe-es-stub",
        "cluster_name": "image-dedupe-local-stub",
        "version": {"number": "8.13.4-stub"},
        "tagline": "You Know, for Local Tests",
    }


@app.put("/{index}")
async def create_index(index: str, request: Request) -> dict:
    _ = await request.body()
    docs = load_index(index)
    save_index(index, docs)
    return {"acknowledged": True, "shards_acknowledged": True, "index": index}


@app.get("/{index}")
def get_index(index: str) -> Response:
    path = index_path(index)
    if not path.exists():
        return JSONResponse({"error": {"type": "index_not_found_exception", "reason": index}}, status_code=404)
    return JSONResponse(
        {
            index: {
                "aliases": {},
                "mappings": {"properties": {"image_phash": {"type": "keyword"}}},
                "settings": {"index": {"number_of_shards": "1", "number_of_replicas": "0"}},
            }
        }
    )


@app.post("/{index}/_refresh")
@app.get("/{index}/_refresh")
def refresh(index: str) -> dict:
    return {"_shards": {"total": 1, "successful": 1, "failed": 0}}


@app.get("/{index}/_count")
@app.post("/{index}/_count")
async def count(index: str, request: Request) -> dict:
    docs = load_index(index)
    body: dict[str, Any] = {}
    if request.method == "POST":
        raw = await request.body()
        if raw:
            body = json.loads(raw)
    q = body.get("query") or {}
    if "exists" in q:
        field = q["exists"]["field"]
        n = sum(1 for d in docs.values() if d.get(field) not in (None, ""))
    else:
        n = len(docs)
    return {"count": n, "_shards": {"total": 1, "successful": 1, "skipped": 0, "failed": 0}}


@app.get("/{index}/_doc/{doc_id}")
def get_doc(index: str, doc_id: str) -> Response:
    docs = load_index(index)
    if doc_id not in docs:
        return JSONResponse({"_index": index, "_id": doc_id, "found": False}, status_code=404)
    return JSONResponse(
        {
            "_index": index,
            "_id": doc_id,
            "_version": 1,
            "found": True,
            "_source": docs[doc_id],
        }
    )


@app.put("/{index}/_doc/{doc_id}")
@app.post("/{index}/_doc/{doc_id}")
async def put_doc(index: str, doc_id: str, request: Request) -> dict:
    src = json.loads(await request.body())
    docs = load_index(index)
    docs[doc_id] = src
    save_index(index, docs)
    return {"_index": index, "_id": doc_id, "result": "created", "_shards": {"total": 1, "successful": 1, "failed": 0}}


@app.post("/_bulk")
@app.post("/{index}/_bulk")
async def bulk(request: Request, index: str | None = None) -> dict:
    raw = (await request.body()).decode()
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    items = []
    buffers: dict[str, dict[str, dict]] = {}
    i = 0
    while i < len(lines):
        meta = json.loads(lines[i])
        i += 1
        action = next(iter(meta))
        info = meta[action]
        idx = info.get("_index") or index
        doc_id = str(info.get("_id"))
        if action in ("index", "create", "update"):
            body = json.loads(lines[i]) if i < len(lines) else {}
            i += 1
            if idx not in buffers:
                buffers[idx] = load_index(idx)
            if action == "update":
                doc = body.get("doc") or body
                prev = buffers[idx].get(doc_id, {})
                prev.update(doc)
                buffers[idx][doc_id] = prev
            else:
                buffers[idx][doc_id] = body
            items.append({action: {"_index": idx, "_id": doc_id, "status": 201, "result": "created"}})
        else:
            items.append({action: {"_index": idx, "_id": doc_id, "status": 400, "error": "unsupported"}})
    for idx, docs in buffers.items():
        save_index(idx, docs)
    return {"took": 1, "errors": False, "items": items}


@app.get("/_mget")
@app.post("/_mget")
@app.get("/{index}/_mget")
@app.post("/{index}/_mget")
async def mget(request: Request, index: str | None = None) -> dict:
    body: dict[str, Any] = {}
    if request.method == "POST":
        raw = await request.body()
        if raw:
            body = json.loads(raw)
    docs_out = []
    for item in body.get("docs") or []:
        idx = item.get("_index") or index
        doc_id = str(item.get("_id"))
        store = load_index(idx)
        if doc_id in store:
            docs_out.append(
                {
                    "_index": idx,
                    "_id": doc_id,
                    "found": True,
                    "_source": store[doc_id],
                }
            )
        else:
            docs_out.append({"_index": idx, "_id": doc_id, "found": False})
    # also support ids: [] form
    if "ids" in body and index:
        store = load_index(index)
        for doc_id in body["ids"]:
            doc_id = str(doc_id)
            if doc_id in store:
                docs_out.append(
                    {"_index": index, "_id": doc_id, "found": True, "_source": store[doc_id]}
                )
            else:
                docs_out.append({"_index": index, "_id": doc_id, "found": False})
    return {"docs": docs_out}


@app.get("/{index}/_search")
@app.post("/{index}/_search")
async def search(index: str, request: Request) -> dict:
    docs = load_index(index)
    size = 10
    body: dict[str, Any] = {}
    if request.method == "POST":
        raw = await request.body()
        if raw:
            body = json.loads(raw)
            size = int(body.get("size") or size)
    q = body.get("query") or {}
    items = list(docs.items())
    if "exists" in q:
        field = q["exists"]["field"]
        items = [(i, d) for i, d in items if d.get(field) not in (None, "")]
    hits = []
    for doc_id, src in items[:size]:
        hits.append({"_index": index, "_id": doc_id, "_score": 1.0, "_source": src})
    return {
        "took": 1,
        "timed_out": False,
        "_shards": {"total": 1, "successful": 1, "skipped": 0, "failed": 0},
        "hits": {"total": {"value": len(items), "relation": "eq"}, "max_score": 1.0, "hits": hits},
    }


@app.get("/{index}/_mapping/field/{field}")
def mapping_field(index: str, field: str) -> dict:
    return {
        index: {
            "mappings": {
                field: {"full_name": field, "mapping": {field: {"type": "keyword"}}}
            }
        }
    }
