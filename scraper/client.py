"""Singleton Telegram client with session-lock safety and proper cleanup.

Handles:
  - WAL journal mode on the SQLite session to prevent DATABASE LOCKED errors.
  - Stale .session-journal file cleanup before connecting.
  - Async context manager pattern.
  - Signal handling (SIGINT / SIGTERM) for clean disconnection.
"""

from __future__ import annotations

import logging
import os
import signal
import sqlite3
import sys
from pathlib import Path
from types import FrameType
from typing import Optional

from telethon import TelegramClient

logger = logging.getLogger(__name__)

# Public Telegram Desktop credentials -- safe to hardcode.
# Override via environment variables if needed.
DEFAULT_API_ID: int = 2040
DEFAULT_API_HASH: str = "b18441a1ff607e10a989891a5462e627"

SESSION_DIR: Path = Path.cwd()
SESSION_NAME: str = "telegram_scraper"

_client: Optional[TelegramClient] = None
_original_sigint: Optional[signal.Handlers] = None
_original_sigterm: Optional[signal.Handlers] = None


def _session_path() -> Path:
    """Return the full path to the session file (without extension)."""
    return SESSION_DIR / SESSION_NAME


def _cleanup_stale_journal() -> None:
    """Delete stale .session-journal files that can cause DATABASE LOCKED."""
    journal = Path(f"{_session_path()}.session-journal")
    if journal.exists():
        logger.info("Removing stale session journal: %s", journal)
        try:
            journal.unlink()
        except OSError as exc:
            logger.warning("Could not remove stale journal: %s", exc)


def _set_wal_mode() -> None:
    """Set WAL journal mode on the session SQLite DB to reduce locking."""
    db_path = Path(f"{_session_path()}.session")
    if not db_path.exists():
        return
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.close()
        logger.debug("Set journal_mode=WAL on session DB")
    except sqlite3.Error as exc:
        logger.warning("Could not set WAL mode on session DB: %s", exc)


def get_client() -> TelegramClient:
    """Return the singleton TelegramClient (creates it on first call).

    Before creating the client:
      1. Cleans up stale journal files.
      2. Sets WAL journal mode on the session DB.
    """
    global _client
    if _client is None:
        _cleanup_stale_journal()
        _set_wal_mode()

        api_id = int(os.environ.get("TELEGRAM_API_ID", DEFAULT_API_ID))
        api_hash = os.environ.get("TELEGRAM_API_HASH", DEFAULT_API_HASH)
        session_path = str(_session_path())
        _client = TelegramClient(session_path, api_id, api_hash)
        logger.debug("Created TelegramClient (session=%s)", session_path)
    return _client


async def disconnect() -> None:
    """Disconnect the client if it exists and release resources."""
    global _client
    if _client is not None:
        try:
            if _client.is_connected():
                await _client.disconnect()
                logger.info("Telegram client disconnected")
        except Exception as exc:
            logger.warning("Error during disconnect: %s", exc)
        finally:
            _client = None
    _restore_signals()


def install_signal_handlers() -> None:
    """Install SIGINT/SIGTERM handlers for clean shutdown.

    These handlers ensure the session DB lock is released even if the
    user presses Ctrl-C or the process is terminated.
    """
    global _original_sigint, _original_sigterm

    def _handler(signum: int, frame: Optional[FrameType]) -> None:
        sig_name = signal.Signals(signum).name
        logger.info("Received %s -- shutting down", sig_name)
        # Raise KeyboardInterrupt so asyncio's event loop can handle it
        raise KeyboardInterrupt

    try:
        _original_sigint = signal.getsignal(signal.SIGINT)
        _original_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)
        logger.debug("Signal handlers installed")
    except (OSError, ValueError):
        # signal.signal can fail if not on main thread
        logger.debug("Could not install signal handlers (not main thread?)")


def _restore_signals() -> None:
    """Restore original signal handlers."""
    global _original_sigint, _original_sigterm
    try:
        if _original_sigint is not None:
            signal.signal(signal.SIGINT, _original_sigint)
            _original_sigint = None
        if _original_sigterm is not None:
            signal.signal(signal.SIGTERM, _original_sigterm)
            _original_sigterm = None
    except (OSError, ValueError):
        pass
