# crypto-options-bot

Multi-strategy options trading bot for **Delta Exchange India** (BTC/ETH).

Three strategies share one execution stack, risk budget, decision log, and analytics:

- **Strategy A — Directional long-premium** (60% risk budget, ~20-40 trades/3wk)
- **Strategy B — Weekly iron condor** (25% risk budget, ~6 trades/3wk)
- **Strategy C — Vol-breakout long strangle** (15% risk budget, ~2-4 trades/3wk)

## Quick start

```bash
cp .env.example .env             # edit values
make dev                         # docker compose up with hot reload, MODE=dry
make test                        # full pytest suite
make lint                        # ruff + mypy
make status                      # one-screen dashboard
```

## Capital model

- NAV: 50,000 INR (~588 USD at fixed 85 INR/USD on Delta India)
- Risk per trade: 1% NAV (Strategy A, C), 1.5% NAV max-loss (Strategy B)
- Three-tier loss caps: -3% daily / -6% weekly / **-15% lifetime peak-to-trough (circuit breaker)**

## Modes

- `BOT_MODE=dry` — simulated fills via `DryExecutor` (default)
- `BOT_MODE=live` — real orders via `LiveExecutor` (requires `DELTA_API_KEY` / `DELTA_API_SECRET` and per-strategy `enabled_live` via `make go-live`)

## Per-strategy go-live

Each strategy has its own gate. Dry-run alone does not promote anything:

```bash
make go-live STRATEGY=directional   # 10 days, 20+ trades, integrity check, kill-switch self-test
make go-live STRATEGY=iron_condor   # 28 days, 8+ trades
make go-live STRATEGY=vol_strangle  # 28 days, 4+ trades (or filter eval >= 100 times)
```

## Recovery

```bash
make resume --confirm    # clear lifetime DD circuit breaker after manual review
```

## Architecture

See the implementation plan for the full design (strategy registry, execution router with DRY_RUN shim,
atomic multi-leg helper, three-tier risk caps, two-layer journaling).

## Deployment

AWS Lightsail Mumbai (~$5/mo). Docker + systemd. CI on every PR, CD on merge to `main` with auto-rollback.
See `deploy/` for the full pipeline.
