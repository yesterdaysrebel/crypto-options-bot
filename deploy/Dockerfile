FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 --shell /bin/bash bot

WORKDIR /app

COPY pyproject.toml README.md ./
COPY bot ./bot

RUN pip install --upgrade pip && pip install -e .

RUN mkdir -p /app/data /app/reports /app/journals /app/logs \
    && chown -R bot:bot /app

USER bot

EXPOSE 9091

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "bot"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:9091/health || exit 1
