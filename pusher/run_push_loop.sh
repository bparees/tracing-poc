#!/usr/bin/env bash
# Long-running wrapper that pushes disk traces to Langfuse on a fixed interval.
set -euo pipefail

INTERVAL="${PUSH_INTERVAL_SECONDS:-60}"
TRACES_DIR="${TRACES_DIR:-/tmp/traces}"
HEALTH_FILE="/tmp/healthy"

mkdir -p "${TRACES_DIR}"

if [[ "${PUSH_RESET_CHECKPOINT:-}" == "true" ]]; then
    echo "PUSH_RESET_CHECKPOINT=true: running one-shot full re-push."
    python3 push_to_langfuse.py "${TRACES_DIR}" --reset-checkpoint || true
fi

echo "Trace pusher started: dir=${TRACES_DIR} interval=${INTERVAL}s"
while true; do
    touch "${HEALTH_FILE}"
    python3 push_to_langfuse.py "${TRACES_DIR}" || true
    sleep "${INTERVAL}"
done
