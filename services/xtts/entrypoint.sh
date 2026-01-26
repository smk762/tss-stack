#!/usr/bin/env bash
set -euo pipefail

HOST_ADDR="${HOST:-0.0.0.0}"
EXAMPLE_FOLDER="${EXAMPLE_FOLDER:-/app/example}"
TUNNEL_URL="${TUNNEL:-http://localhost:8020}"
CONTAINER_PORT="${CONTAINER_PORT:-8020}"
MODEL_SOURCE="${MODEL_SOURCE:-apiManual}"

# If the user passed a command, run it instead of the default server.
if [[ $# -gt 0 ]]; then
  exec "$@"
fi

exec python -m xtts_api_server \
  -hs "${HOST_ADDR}" \
  -sf "${EXAMPLE_FOLDER}" \
  -t "${TUNNEL_URL}" \
  -p "${CONTAINER_PORT}" \
  -ms "${MODEL_SOURCE}"
