#!/usr/bin/env python3
"""Entry point for the Telegram scraper."""

from __future__ import annotations

import asyncio
import logging
import sys

from scraper.cli import run_cli
from scraper.client import disconnect

logger = logging.getLogger(__name__)


async def main() -> None:
    try:
        await run_cli()
    except KeyboardInterrupt:
        print("\nInterrupted.")
    except Exception as exc:
        logger.critical("Fatal error: %s", exc, exc_info=True)
        print(f"\nFatal error: {exc}")
    finally:
        await disconnect()


def main_sync() -> None:
    """Synchronous wrapper for use as a console_scripts entry point."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)


if __name__ == "__main__":
    main_sync()
