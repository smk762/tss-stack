#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export MINIO_ROOT_USER="${MINIO_ROOT_USER:-minioadmin}"
export MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD:-minioadmin}"

wait_for_http() {
  local name="$1"
  local url="$2"
  local attempts="${3:-60}"

  for ((i=1; i<=attempts; i++)); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done

  echo "Timed out waiting for ${name} at ${url}" >&2
  return 1
}

if [[ "${SKIP_STACK_START:-0}" != "1" ]]; then
  echo "Starting provider smoke-test stack..."
  docker compose up -d redis minio xtts whisper
  wait_for_http "MinIO" "http://localhost:9010/minio/health/live"

  if [[ "${SKIP_BUILD:-0}" == "1" ]]; then
    docker compose up -d gateway tts-worker whisper-worker
  else
    docker compose up -d --build gateway tts-worker whisper-worker
  fi
fi

echo "Running provider smoke test..."
python3 scripts/provider_smoke.py
