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

## Project layout

| Path | Role |
|------|------|
| `app/main.py` | FastAPI endpoints |
| `app/embedding.py` | CLIP `extract` |
| `app/db.py` | Qdrant via `QDRANT_URL` |
| `app/dedupe.py` | Union-Find grouping |
| `app/schemas.py` | Pydantic models |
| `Dockerfile` | FastAPI image |
| `docker-compose.yml` | `app` + `qdrant` |
| `scripts/import_wisgoon_posts.py` | Bulk import Wisgoon posts → Qdrant |
| `.env.example` | Config template |

## Config (`.env`)

| Key | Meaning |
|-----|---------|
| `CLIP_MODEL_NAME` | HuggingFace CLIP id |
| `SIMILARITY_THRESHOLD` | Cosine threshold for duplicates |
| `QDRANT_URL` | e.g. `http://qdrant:6333` in Compose |
| `COLLECTION_NAME` | Qdrant collection |
| `APP_PORT` | Host port for the API (default 3020) |
| `MAX_UPLOAD_BYTES` | Upload size limit for `/embed` |

## Production notes

- Prefer Nginx (or similar) in front of `:3020` if the host is public.
- Logs use the standard `logging` module.
- First app start downloads the CLIP weights into the `hf_cache` volume (can take a few minutes).
