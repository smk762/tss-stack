#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export MINIO_ROOT_USER="${MINIO_ROOT_USER:-minioadmin}"
export MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD:-minioadmin}"

if [[ "${SKIP_COMPOSE_VALIDATE:-0}" != "1" ]]; then
  echo "Validating docker compose configuration..."
  docker compose config -q
fi

echo "Running local pytest suite..."
python3 -m pytest "$@"

if [[ "${RUN_PROVIDER_SMOKE:-0}" == "1" ]]; then
  echo "Running provider smoke suite..."
  ./scripts/run-provider-smoke.sh
fi
