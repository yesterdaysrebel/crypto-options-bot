#!/bin/sh
# As root (production): fix ownership on mounted volumes so SQLite is writable, then drop to `bot`.
# As bot (local dev): delegate straight to the bot entrypoint.
set -eu

BOT_ENTRY=/docker-entrypoint-bot.sh

if [ "$(id -u)" = "0" ]; then
    for d in /app/data /app/reports /app/journals /app/logs /app/runtime; do
        mkdir -p "$d" 2>/dev/null || true
        chown -R bot:bot "$d" 2>/dev/null || true
    done
    exec gosu bot "$BOT_ENTRY" "$@"
fi

exec "$BOT_ENTRY" "$@"
