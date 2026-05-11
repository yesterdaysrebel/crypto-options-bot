.PHONY: help install dev test int-test lint format type-check clean \
        build run status snapshot deploy rollback go-live resume

PYTHON ?= python3
PIP ?= $(PYTHON) -m pip
PYTEST ?= $(PYTHON) -m pytest
RUFF ?= $(PYTHON) -m ruff
MYPY ?= $(PYTHON) -m mypy

STRATEGY ?=

help:
	@echo "Targets:"
	@echo "  install      Install package + dev deps into the current Python env"
	@echo "  dev          Run bot locally via docker compose (MODE=dry, hot reload)"
	@echo "  test         Run unit tests"
	@echo "  int-test     Run integration tests (live read-only endpoints)"
	@echo "  lint         Ruff lint + format check"
	@echo "  format       Ruff format (in-place)"
	@echo "  type-check   Mypy"
	@echo "  build        docker build"
	@echo "  run          Run the bot directly (no docker)"
	@echo "  status       One-screen dashboard"
	@echo "  snapshot     Gzipped SQLite snapshot under data/snapshots/"
	@echo "  deploy       Manual deploy to VPS via SSH"
	@echo "  rollback     Revert VPS to previous image SHA"
	@echo "  go-live      Gated promotion: make go-live STRATEGY=directional"
	@echo "  resume       Clear lifetime DD circuit breaker (requires --confirm)"
	@echo "  clean        Remove caches"

install:
	$(PIP) install -e ".[dev]"

dev:
	docker compose -f deploy/docker-compose.yml --env-file .env up --build

test:
	$(PYTEST) -m "not integration"

int-test:
	$(PYTEST) -m integration

lint:
	$(RUFF) check bot tests
	$(RUFF) format --check bot tests

format:
	$(RUFF) check --fix bot tests
	$(RUFF) format bot tests

type-check:
	$(MYPY) bot

build:
	docker build -t crypto-options-bot:local -f deploy/Dockerfile .

run:
	$(PYTHON) -m bot

status:
	$(PYTHON) -m bot.cli status

snapshot:
	$(PYTHON) -m bot.cli snapshot

deploy:
	bash deploy/deploy.sh

rollback:
	bash deploy/rollback.sh

go-live:
	@if [ -z "$(STRATEGY)" ]; then echo "Usage: make go-live STRATEGY=<id>"; exit 1; fi
	$(PYTHON) -m bot.cli go-live --strategy $(STRATEGY)

resume:
	$(PYTHON) -m bot.cli resume

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
