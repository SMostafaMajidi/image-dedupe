# Image Dedup Microservice

CLIP + Qdrant near-duplicate image detection. Phases 1–4 complete (CLIP, Qdrant, FastAPI, Docker).

## Stack

- Python 3.11
- CLIP (`openai/clip-vit-base-patch32`)
- Qdrant
- FastAPI (`/embed`, `/dedupe`)
- Docker Compose

## Quick start (Docker — recommended)

```bash
git clone https://github.com/SMostafaMajidi/image-dedupe.git
cd image-dedupe
cp .env.example .env

docker compose up --build -d
docker compose logs -f app
```

- API docs: http://localhost:3020/docs  
- Health: http://localhost:3020/health  
- **جدول وکتورها:** http://localhost:3020/browse  
- **پیدا کردن مشابه:** http://localhost:3020/find  
- Qdrant UI: http://localhost:6333/dashboard  

Host API port is `APP_PORT` (default **3020**). Inside the container the app listens on `8000`.

### Smoke test from the host

```bash
curl -s http://localhost:3020/health

curl -s -X POST http://localhost:3020/embed \
  -F post_uid=post_1 \
  -F image=@sample_images/sunset.jpg

curl -s -X POST http://localhost:3020/dedupe \
  -H 'Content-Type: application/json' \
  -d '{"post_uids":["post_1","post_2","ghost"]}'
```

Generate samples first if needed: `python scripts/generate_samples.py` (host Python / venv).

### Data persistence

Qdrant uses the named volume `qdrant_storage`. After `docker compose restart` or `down` + `up`, vectors remain. Only `docker compose down -v` wipes them.

### Bulk import from Wisgoon (~500k)

Reads DSN from the main SDK config YAML (not the train SDK). Implements its own SQL.

```bash
# smoke
QDRANT_URL=http://127.0.0.1:6333 \
  python scripts/import_wisgoon_posts.py --limit 5 --workers 8

# full
QDRANT_URL=http://127.0.0.1:6333 \
  python scripts/import_wisgoon_posts.py --limit 500000 --workers 8
```

Config path: `WISGOON_CONNECTIONS_YAML` (default production `connections.yaml`).

### REL-DUP4 — pHash → Elasticsearch (for gateway related filter)

Gateway reads `image_phash` — a 256-bit (16x16) pHash, Hamming ≤ 16 — from
`wis-post-0.0.2-v3` (no request-time Python call). 64-bit pHash could not separate
same-template posts with different text (tweet cards, text screens) from real
reposts; see `sample_images/related_eval/report_256_vs_64.html`. VIDEO posts are
hashed from their thumbnail. Near-flat images get the all-zero sentinel
(`"0" * 64`) and the gateway drops them from related. The gateway ignores values
that are not 64 hex chars (old 64-bit hashes during migration).

New posts are hashed by the search consumer (`wisgoon/ms/search`, Go port of the
same pHash); the backfill below covers everything older than its first start.

```bash
# stage 1 — mapping (idempotent)
python scripts/put_es_phash_mapping.py

# stage 2 — full backfill in Docker (IMAGE + VIDEO, newest -> oldest, resumable)
docker compose -f docker-compose.backfill.yml up -d --build
docker compose -f docker-compose.backfill.yml exec phash-backfill python scripts/phash_backfill_worker.py status
docker compose -f docker-compose.backfill.yml logs --tail 20 phash-backfill   # one line / 5 min
# state + small rotating log: ./backfill-data/{state.json,backfill.log}

# ad-hoc runs (testable with --limit)
.venv/bin/python scripts/backfill_phash_es.py --limit 20 --workers 8
LIMIT=1000000 ./scripts/run_overnight_phash_prod.sh                      # IMAGE, in screen
CONTENT_TYPE=VIDEO LIMIT=1000000 ./scripts/run_overnight_phash_prod.sh   # video thumbnails
./scripts/run_overnight_phash_prod.sh --resume                           # continue older posts

# verify coverage
curl -s "$ELASTIC_URL/wis-post-0.0.2-v3/_count" \
  -H 'Content-Type: application/json' \
  -d '{"query":{"exists":{"field":"image_phash"}}}'
```

Measured ~20–25 posts/s with 32 workers on the lab network (download-bound).
CLIP import is much slower; this path is **pHash-only**.

## Local Python (optional, without app container)

```bash
# .env for host-side scripts (Qdrant still via Compose):
# QDRANT_URL=http://localhost:6333

docker compose up -d qdrant
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## API

### `POST /embed`

Multipart: `post_uid` + `image` (`image/*`). Max size: `MAX_UPLOAD_BYTES`.

### `POST /dedupe`

JSON: `{ "post_uids": [...] }` → `unique_post_uids`, `removed_post_uids`, `groups`, `missing_post_uids`.

Vectors must already exist in Qdrant (embed via `/embed` or `/similar` first — the
latter auto-ingests from Wisgoon if missing). Two post_uids are merged into the
same group if **either**:
- CLIP cosine similarity ≥ `DEDUPE_SIMILARITY_THRESHOLD` (default `0.97`,
  stricter than `SIMILARITY_THRESHOLD` used by `/similar`), or
- pHash Hamming distance ≤ `DEDUPE_HASH_MAX_DISTANCE` (default `10`, 0–64 range)

This "exact media regardless of edits/logo" mode (REL-DUP) is intentionally
narrower than the general semantic-similarity mode (`/similar`).

## Project layout

| Path | Role |
|------|------|
| `app/main.py` | FastAPI endpoints |
| `app/embedding.py` | CLIP `extract` |
| `app/db.py` | Qdrant via `QDRANT_URL` |
| `app/dedupe.py` | Union-Find grouping (cosine OR pHash) |
| `app/phash.py` | Perceptual hash (pHash) for exact-media dedupe |
| `app/schemas.py` | Pydantic models |
| `Dockerfile` | FastAPI image |
| `docker-compose.yml` | `app` + `qdrant` |
| `scripts/import_wisgoon_posts.py` | Bulk import Wisgoon posts → Qdrant (CLIP) |
| `scripts/put_es_phash_mapping.py` | REL-DUP4: ensure `image_phash` ES mapping |
| `scripts/backfill_phash_es.py` | REL-DUP4: pHash → ES (no CLIP) |
| `.env.example` | Config template |

## Config (`.env`)

| Key | Meaning |
|-----|---------|
| `CLIP_MODEL_NAME` | HuggingFace CLIP id |
| `SIMILARITY_THRESHOLD` | Cosine threshold for `/similar` (broad, semantic) |
| `DEDUPE_SIMILARITY_THRESHOLD` | Stricter cosine threshold for `/dedupe` (exact media), default `0.97` |
| `DEDUPE_HASH_MAX_DISTANCE` | pHash Hamming distance bound for `/dedupe`, default `10` |
| `QDRANT_URL` | e.g. `http://qdrant:6333` in Compose |
| `COLLECTION_NAME` | Qdrant collection |
| `APP_PORT` | Host port for the API (default 3020) |
| `MAX_UPLOAD_BYTES` | Upload size limit for `/embed` |

## Production notes

- Prefer Nginx (or similar) in front of `:3020` if the host is public.
- Logs use the standard `logging` module.
- First app start downloads the CLIP weights into the `hf_cache` volume (can take a few minutes).
