#!/usr/bin/env bash
# Smoke test after a deploy. Waits up to TIMEOUT seconds for the container to be healthy
# and the /health endpoint to return 200.
#
# Inputs (env):
#   TIMEOUT      — total wait in seconds (default: 120)
#   HEALTH_URL   — full URL of the health endpoint (default: http://127.0.0.1:9091/health)
#   CONTAINER    — name of the docker container (default: crypto-options-bot)

set -euo pipefail

TIMEOUT=${TIMEOUT:-120}
HEALTH_URL=${HEALTH_URL:-http://127.0.0.1:9091/health}
CONTAINER=${CONTAINER:-crypto-options-bot}

echo "[health-check] waiting up to ${TIMEOUT}s for ${CONTAINER}"

deadline=$(( $(date +%s) + TIMEOUT ))

while true; do
    now=$(date +%s)
    if (( now >= deadline )); then
        echo "[health-check] FAILED: timed out after ${TIMEOUT}s"
        docker logs --tail 50 "${CONTAINER}" || true
        exit 1
    fi

    state=$(docker inspect -f '{{.State.Status}}' "${CONTAINER}" 2>/dev/null || echo "missing")
    if [[ "${state}" != "running" ]]; then
        sleep 2
        continue
    fi

    health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${CONTAINER}" 2>/dev/null || echo "missing")
    case "${health}" in
        healthy)
            echo "[health-check] container is healthy"
            break
            ;;
        none)
            if curl -fsS "${HEALTH_URL}" >/dev/null 2>&1; then
                echo "[health-check] /health responding (no docker healthcheck configured)"
                break
            fi
            ;;
        starting|unhealthy)
            ;;  # keep waiting
        *)
            ;;  # treat unknown as keep waiting
    esac

    sleep 3
done

_health_ok() {
    curl -fsS "${HEALTH_URL}" >/dev/null 2>&1 && return 0
    # Fallback: in-container listener may be 127.0.0.1 only (host port publish cannot reach it).
    docker exec "${CONTAINER}" curl -fsS "http://127.0.0.1:9091/health" >/dev/null 2>&1
}

if ! _health_ok; then
    echo "[health-check] FAILED: ${HEALTH_URL} did not return 200 (host and docker exec)"
    docker logs --tail 50 "${CONTAINER}" || true
    exit 1
fi

echo "[health-check] OK"
