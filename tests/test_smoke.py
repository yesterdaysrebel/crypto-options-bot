"""Smoke tests verifying the package imports and basic entry points exist."""

from __future__ import annotations


def test_package_import() -> None:
    import bot

    assert bot.__version__


def test_main_entry_exists() -> None:
    from bot import __main__

    assert callable(__main__.app)


def test_app_run_is_coroutine() -> None:
    import inspect

    from bot.app import run

    assert inspect.iscoroutinefunction(run)


def test_cli_imports() -> None:
    from bot.cli import cli

    assert cli is not None
