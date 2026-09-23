# Image Dedup Microservice (WIP)

CLIP-based near-duplicate image detection. Phases 1–2 done (local CLIP + Qdrant layer). FastAPI / full Docker app come in later phases.

## Stack

- Python 3.11
- CLIP (`openai/clip-vit-base-patch32`)
- Qdrant (Docker Compose)
- FastAPI (planned, phase 3)

## Quick start on Linux

```bash
git clone <REPO_URL>
cd image   # or whatever the clone folder is named
cp .env.example .env

# Start Qdrant
docker compose up -d

# Python env
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Sample images + smoke tests
python scripts/generate_samples.py
python scripts/test_clip.py
python scripts/test_qdrant.py
```

Qdrant UI (when using the Compose image): http://localhost:6333/dashboard

## Project layout

| Path | Role |
|------|------|
| `db.py` | Qdrant helpers (`upsert` / `get_vectors` / `search_similar`) |
| `scripts/test_clip.py` | Phase 1: CLIP cosine similarity |
| `scripts/test_qdrant.py` | Phase 2: persist + retrieve + NN |
| `scripts/generate_samples.py` | Fixed synthetic test images |
| `docker-compose.yml` | Qdrant service |
| `.env.example` | Config template |

## Config (`.env`)

See `.env.example`. Important keys:

- `CLIP_MODEL_NAME`
- `SIMILARITY_THRESHOLD`
- `QDRANT_HOST` / `QDRANT_PORT`
- `COLLECTION_NAME`
