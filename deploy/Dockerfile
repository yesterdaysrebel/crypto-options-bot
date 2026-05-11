FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gosu tini \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 --shell /bin/bash bot

WORKDIR /app

COPY pyproject.toml README.md alembic.ini ./
COPY bot ./bot
COPY config ./config
COPY deploy/docker-entrypoint.sh /docker-entrypoint.sh
COPY deploy/docker-entrypoint-bot.sh /docker-entrypoint-bot.sh

RUN pip install --upgrade pip && pip install -e . \
    && chmod 0555 /docker-entrypoint.sh /docker-entrypoint-bot.sh \
    && mkdir -p /app/data /app/reports /app/journals /app/logs /app/runtime \
    && chown -R bot:bot /app

# Root at start so we can chown host-mounted volumes on Lightsail; gosu drops to `bot` before Alembic/app.
USER root

EXPOSE 9091

ENTRYPOINT ["/usr/bin/tini", "--", "/docker-entrypoint.sh"]
CMD ["python", "-m", "bot"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:9091/health || exit 1
