"""Typer-based CLI for `bot status`, `bot go-live`, `bot resume`, `bot snapshot`."""

from __future__ import annotations

import asyncio

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
