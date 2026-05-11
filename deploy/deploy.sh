#!/usr/bin/env bash
# Apply a new image SHA on the VPS.
#
# Inputs (env):
#   IMAGE        — full ghcr.io image ref (required), e.g. ghcr.io/foo/bar:sha-abc123
#
# Side effects:
#   * Saves the current IMAGE to /opt/crypto-options-bot/releases/previous-image.txt
#   * Pulls the new image
#   * Writes the new IMAGE to /opt/crypto-options-bot/releases/current-image.txt
#   * `docker compose up -d` swaps the running container

set -euo pipefail

APP_DIR=/opt/crypto-options-bot
COMPOSE_FILE="${APP_DIR}/docker-compose.prod.yml"
RELEASES_DIR="${APP_DIR}/releases"
CURRENT_FILE="${RELEASES_DIR}/current-image.txt"
PREVIOUS_FILE="${RELEASES_DIR}/previous-image.txt"

if [[ -z "${IMAGE:-}" ]]; then
    echo "deploy.sh: IMAGE env var is required" >&2
    exit 1
fi

mkdir -p "${RELEASES_DIR}"

if [[ -f "${CURRENT_FILE}" ]]; then
    cp "${CURRENT_FILE}" "${PREVIOUS_FILE}"
fi

echo "[deploy] pulling ${IMAGE}"
IMAGE="${IMAGE}" docker compose -f "${COMPOSE_FILE}" pull

echo "${IMAGE}" > "${CURRENT_FILE}"

echo "[deploy] starting service"
IMAGE="${IMAGE}" docker compose -f "${COMPOSE_FILE}" up -d --remove-orphans

echo "[deploy] active image: $(cat "${CURRENT_FILE}")"
if [[ -f "${PREVIOUS_FILE}" ]]; then
    echo "[deploy] previous image: $(cat "${PREVIOUS_FILE}")"
fi
