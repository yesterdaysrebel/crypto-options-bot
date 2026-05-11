#!/usr/bin/env bash
# Revert to the previous image SHA recorded in releases/previous-image.txt.
#
# If no previous image is recorded the rollback is a no-op (and exits non-zero so the
# operator notices). Otherwise it swaps current/previous and runs `docker compose up`.

set -euo pipefail

APP_DIR=/opt/crypto-options-bot
COMPOSE_FILE="${APP_DIR}/docker-compose.prod.yml"
RELEASES_DIR="${APP_DIR}/releases"
CURRENT_FILE="${RELEASES_DIR}/current-image.txt"
PREVIOUS_FILE="${RELEASES_DIR}/previous-image.txt"

if [[ ! -f "${PREVIOUS_FILE}" ]]; then
    echo "[rollback] no previous-image.txt to roll back to" >&2
    exit 1
fi

PREV=$(cat "${PREVIOUS_FILE}")
if [[ -z "${PREV}" ]]; then
    echo "[rollback] previous image is empty" >&2
    exit 1
fi

echo "[rollback] reverting to ${PREV}"

if [[ -f "${CURRENT_FILE}" ]]; then
    cp "${CURRENT_FILE}" "${RELEASES_DIR}/failed-image.txt"
fi

echo "${PREV}" > "${CURRENT_FILE}"

IMAGE="${PREV}" docker compose -f "${COMPOSE_FILE}" pull
IMAGE="${PREV}" docker compose -f "${COMPOSE_FILE}" up -d --remove-orphans

echo "[rollback] complete. Active image: ${PREV}"
