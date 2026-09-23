# Image Dedup Microservice

CLIP + Qdrant near-duplicate image detection for social posts. Phases 1–3 done. Dockerized app comes in phase 4.

## Stack

- Python 3.11+
- CLIP (`openai/clip-vit-base-patch32`)
- Qdrant (Docker Compose)
- FastAPI (`/embed`, `/dedupe`)

## Quick start on Linux

```bash
git clone https://github.com/SMostafaMajidi/image-dedupe.git
cd image-dedupe
cp .env.example .env

docker compose up -d

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python scripts/generate_samples.py
python scripts/test_clip.py
python scripts/test_qdrant.py

# API (phase 3)
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
# other terminal:
python scripts/test_api.py
```

Qdrant UI: http://localhost:6333/dashboard  
API docs: http://127.0.0.1:8000/docs

## API

### `POST /embed`

Multipart form: `post_uid` + `image` (content-type must be `image/*`). Embeds with CLIP, upserts into Qdrant (same `post_uid` overwrites).

```bash
curl -s -X POST http://127.0.0.1:8000/embed \
  -F post_uid=post_1 \
  -F image=@sample_images/sunset.jpg
```

Response: `{"post_uid":"post_1","status":"stored"}`

### `POST /dedupe`

JSON body with `post_uids`. Pairwise cosine + Union-Find at `SIMILARITY_THRESHOLD`. Missing uids are reported, not ignored.

```bash
curl -s -X POST http://127.0.0.1:8000/dedupe \
  -H 'Content-Type: application/json' \
  -d '{"post_uids":["post_1","post_2","ghost"]}'
```

Response fields: `unique_post_uids`, `removed_post_uids`, `groups`, `missing_post_uids`, `threshold`.

Errors: non-image / corrupt file → `400`; empty list → `400`.

## Project layout

| Path | Role |
|------|------|
| `app/main.py` | FastAPI endpoints |
| `app/embedding.py` | CLIP load + `extract` |
| `app/db.py` | Qdrant upsert / get_vectors |
| `app/dedupe.py` | Union-Find grouping |
| `app/schemas.py` | Pydantic models |
| `scripts/test_*.py` | Phase smoke tests |
| `docker-compose.yml` | Qdrant service |
| `.env.example` | Config template |

## Config (`.env`)

- `CLIP_MODEL_NAME`
- `SIMILARITY_THRESHOLD`
- `QDRANT_HOST` / `QDRANT_PORT`
- `COLLECTION_NAME`
