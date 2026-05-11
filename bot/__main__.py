"""Bot entry point: `python -m bot` boots the main loop."""

from __future__ import annotations

import asyncio

from bot.app import run


def app() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    app()
