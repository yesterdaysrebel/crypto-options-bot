"""Typer-based CLI for `bot status`, `bot go-live`, `bot resume`, `bot snapshot`."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from bot.config.settings import Settings

cli = typer.Typer(no_args_is_help=True, add_completion=False)


@cli.command()
def status() -> None:
    """Print a one-screen dashboard (positions, today's PnL, last decisions)."""
    from bot.observability.status import render_status_dashboard
    from bot.storage.db import get_database

    settings = Settings()
    db = get_database(str(settings.db_url))
    asyncio.run(render_status_dashboard(db))
    raise typer.Exit(code=0)


@cli.command()
def snapshot() -> None:
    """Gzipped SQLite snapshot under data/snapshots/."""
    typer.echo("snapshot: not yet implemented (PR #5)")
    raise typer.Exit(code=0)


@cli.command("go-live")
def go_live(
    strategy: str = typer.Option(..., "--strategy", "-s"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the report without flipping enabled_live"),
) -> None:
    """Gated promotion of one strategy from dry to live."""
    from bot.runtime.go_live import GoLiveGate, format_report
    from bot.storage.db import get_database

    settings = Settings()
    db = get_database(str(settings.db_url))
    gate = GoLiveGate(db, config_dir=settings.config_dir)
    report = asyncio.run(gate.evaluate(strategy, dry_run=dry_run))
    typer.echo(format_report(report))
    if not report.passed:
        raise typer.Exit(code=1)


def _db_path_from_settings(settings: Settings) -> Path:
    db_url = str(settings.db_url)
    if db_url.startswith("sqlite:////"):
        return Path("/" + db_url.removeprefix("sqlite:////"))
    if db_url.startswith("sqlite:///"):
        return Path(db_url.removeprefix("sqlite:///"))
    return Path(db_url)


@cli.command("optimize-directional")
def optimize_directional(
    since: str | None = typer.Option(None, help="UTC start, e.g. 2026-05-14"),
    until: str | None = typer.Option(None, help="UTC end (exclusive)"),
    output: str | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Markdown report path (default: stdout)",
    ),
) -> None:
    """Analyze filled directional trades vs Delta prices for strategy tuning (excludes errored)."""
    from bot.analytics.directional_optimize import run_optimization

    settings = Settings()
    db_path = _db_path_from_settings(settings)
    if not db_path.is_file():
        typer.echo(f"DB not found: {db_path}", err=True)
        raise typer.Exit(code=1)
    out_path = Path(output) if output else None
    text = asyncio.run(run_optimization(db_path, since=since, until=until, output=out_path))
    if out_path is None:
        typer.echo(text)
    else:
        typer.echo(f"Wrote {out_path}")
    raise typer.Exit(code=0)


@cli.command("analyze-directional")
def analyze_directional(
    since: str | None = typer.Option(None, help="UTC start date/time, e.g. 2026-05-27"),
    until: str | None = typer.Option(None, help="UTC end date/time (exclusive)"),
    mode: str = typer.Option("live", help="Trade mode filter"),
    status: str | None = typer.Option(None, help="Trade status filter, e.g. errored"),
    dedupe: bool = typer.Option(True, help="Collapse retry-storm duplicates into 15m buckets"),
    max_samples: int = typer.Option(40, help="Max deduped samples to fetch candles for"),
    output: str | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Write markdown report to this path (default: print to stdout)",
    ),
) -> None:
    """Analyze directional attempts vs Delta underlying price action."""
    from bot.analytics.directional_postmortem import run_postmortem

    settings = Settings()
    db_path = _db_path_from_settings(settings)
    if not db_path.is_file():
        typer.echo(f"DB not found: {db_path}", err=True)
        raise typer.Exit(code=1)
    out_path = Path(output) if output else None
    text = asyncio.run(
        run_postmortem(
            db_path,
            since=since,
            until=until,
            mode=mode,
            status=status,
            dedupe=dedupe,
            max_samples=max_samples,
            output=out_path,
        )
    )
    if out_path is None:
        typer.echo(text)
    else:
        typer.echo(f"Wrote {out_path}")
    raise typer.Exit(code=0)


@cli.command("analyze-open")
def analyze_open(
    output: str | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Markdown report path (default: stdout)",
    ),
) -> None:
    """Analyze open Delta positions: entry rationale, errored/orphan status, distance to bot stops."""
    from bot.analytics.directional_postmortem import run_open_positions_analysis

    settings = Settings()
    db_path = _db_path_from_settings(settings)
    if not db_path.is_file():
        typer.echo(f"DB not found: {db_path}", err=True)
        raise typer.Exit(code=1)
    out_path = Path(output) if output else None
    text = asyncio.run(run_open_positions_analysis(db_path, config_dir=settings.config_dir, output=out_path))
    if out_path is None:
        typer.echo(text)
    else:
        typer.echo(f"Wrote {out_path}")
    raise typer.Exit(code=0)


@cli.command()
def resume(confirm: bool = typer.Option(False, "--confirm")) -> None:
    """Clear the lifetime DD circuit breaker after manual review."""
    from bot.runtime.resume import ResumeService, format_report
    from bot.storage.db import get_database

    settings = Settings()
    db = get_database(str(settings.db_url))
    service = ResumeService(db, runtime_dir=settings.runtime_dir)
    report = asyncio.run(service.evaluate(confirm=confirm))
    typer.echo(format_report(report))
    if report.breaker_was_tripped and not report.cleared:
        raise typer.Exit(code=2)
    raise typer.Exit(code=0)


if __name__ == "__main__":
    cli()
