#!/usr/bin/env bash
# Overnight REL-DUP4: pHash recent IMAGE posts → production Elasticsearch.
#
# Default: ~400k newest IMAGE posts (~5–8h at ~15–25/s with 28 workers).
# Requires explicit ALLOW_PROD_ES_WRITE=1 (set below for this launcher only).
#
# Usage:
#   ./scripts/run_overnight_phash_prod.sh              # start (foreground)
#   ./scripts/run_overnight_phash_prod.sh --resume      # continue from checkpoint
#   LIMIT=200000 WORKERS=24 ./scripts/run_overnight_phash_prod.sh
#
# Screen (detach overnight):
#   screen -S phash-overnight -dm bash -lc 'cd …/image-dedupe && ./scripts/run_overnight_phash_prod.sh'
#   screen -r phash-overnight

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

LIMIT="${LIMIT:-400000}"
WORKERS="${WORKERS:-28}"
FLUSH="${FLUSH:-100}"
BATCH="${BATCH:-500}"
CONTENT_TYPE="${CONTENT_TYPE:-IMAGE}"
PY="${PY:-$ROOT/.venv/bin/python}"
STAMP="$(date +%Y%m%d_%H%M%S)"
LOG_DIR="${LOG_DIR:-$ROOT/logs}"
mkdir -p "$LOG_DIR"
TAG="$(echo "$CONTENT_TYPE" | tr '[:upper:]' '[:lower:]')"
LOG_FILE="${LOG_FILE:-$LOG_DIR/phash256_${TAG}_${STAMP}.log}"
STATUS_FILE="${STATUS_FILE:-$LOG_DIR/phash256_${TAG}_status.json}"
CHECKPOINT_FILE="${CHECKPOINT_FILE:-$LOG_DIR/phash256_${TAG}_checkpoint.txt}"
LATEST_LOG_LINK="$LOG_DIR/phash256_${TAG}_latest.log"

RESUME_ARGS=()
if [[ "${1:-}" == "--resume" ]]; then
  RESUME_ARGS=(--resume-from-checkpoint)
fi

export ALLOW_PROD_ES_WRITE=1

if [[ ! -x "$PY" ]]; then
  echo "missing venv python: $PY" >&2
  exit 1
fi

ln -sfn "$(basename "$LOG_FILE")" "$LATEST_LOG_LINK" 2>/dev/null || true

{
  echo "=== overnight phash → prod ES ==="
  echo "started: $(date -Is)"
  echo "type=$CONTENT_TYPE limit=$LIMIT workers=$WORKERS flush=$FLUSH"
  echo "status=$STATUS_FILE"
  echo "checkpoint=$CHECKPOINT_FILE"
  echo "log=$LOG_FILE"
  echo "ALLOW_PROD_ES_WRITE=$ALLOW_PROD_ES_WRITE"
  echo "================================="
} | tee -a "$LOG_FILE"

# shellcheck disable=SC2086
set +e
"$PY" scripts/backfill_phash_es.py \
  --limit "$LIMIT" \
  --workers "$WORKERS" \
  --batch-size "$BATCH" \
  --flush-size "$FLUSH" \
  --content-type "$CONTENT_TYPE" \
  --checkpoint-file "$CHECKPOINT_FILE" \
  --status-file "$STATUS_FILE" \
  "${RESUME_ARGS[@]}" \
  2>&1 | tee -a "$LOG_FILE"
RC=${PIPESTATUS[0]}
set -e

{
  echo "================================="
  echo "finished: $(date -Is) exit=$RC"
  echo "status file: $STATUS_FILE"
  echo "verify count:"
  echo "  curl -s \"\$ELASTIC_URL/wis-post-0.0.2-v3/_count\" -H 'Content-Type: application/json' -d '{\"query\":{\"exists\":{\"field\":\"image_phash\"}}}'"
} | tee -a "$LOG_FILE"

exit "$RC"
