"""Singleton Telegram client with public Telegram Desktop API credentials."""

from __future__ import annotations

import os
from pathlib import Path

from telethon import TelegramClient

# Public Telegram Desktop credentials — safe to hardcode.
# Override via environment variables if needed.
DEFAULT_API_ID = 2040
DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"

SESSION_DIR = Path.cwd()
SESSION_NAME = "telegram_scraper"

_client: TelegramClient | None = None


def get_client() -> TelegramClient:
    """Return the singleton TelegramClient (creates it on first call)."""
    global _client
    if _client is None:
        api_id = int(os.environ.get("TELEGRAM_API_ID", DEFAULT_API_ID))
        api_hash = os.environ.get("TELEGRAM_API_HASH", DEFAULT_API_HASH)
        session_path = str(SESSION_DIR / SESSION_NAME)
        _client = TelegramClient(session_path, api_id, api_hash)
    return _client


async def disconnect() -> None:
    """Disconnect the client if it exists."""
    global _client
    if _client is not None:
        await _client.disconnect()
        _client = None
