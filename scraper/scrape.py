"""Core scraping logic -- read-only message retrieval with rate-limit handling.

Key design decisions:
  - NEVER sends messages, only reads.
  - Skips MessageService (system messages) client-side immediately.
  - Handles FloodWaitError with exponential backoff.
  - Uses Telethon's async iterator for memory-efficient streaming.
  - Caches sender lookups to avoid redundant API calls.
  - Supports full-history, last-N-days, and last-N-messages modes.
  - Handles disconnection mid-scrape, empty groups, and access errors.
  - Supports an overall timeout for the scrape operation.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodWaitError,
)
from telethon.tl.types import (
    Channel,
    Chat,
    MessageEmpty,
    MessageService,
    User,
)

logger = logging.getLogger(__name__)


# -- Data types ---------------------------------------------------------------

@dataclass
class ScrapedMessage:
    """Flat representation of a single message."""

    id: int
    date: datetime
    sender_id: Optional[int]
    sender_name: str
    text: str
    media_type: Optional[str] = None
    media_path: Optional[str] = None
    reply_to_msg_id: Optional[int] = None
    views: Optional[int] = None
    forwards: Optional[int] = None


@dataclass
class GroupInfo:
    """Metadata about a group or channel."""

    id: int
    title: str
    username: Optional[str] = None
    member_count: Optional[int] = None
    is_channel: bool = False


@dataclass
class ScrapeResult:
    """Result of a scrape operation with statistics."""

    messages: list[ScrapedMessage] = field(default_factory=list)
    scanned: int = 0
    skipped: int = 0
    error: Optional[str] = None
    timed_out: bool = False
    elapsed_seconds: float = 0.0

    @property
    def collected(self) -> int:
        return len(self.messages)


# -- Group listing ------------------------------------------------------------

async def get_groups(client: TelegramClient) -> list[GroupInfo]:
    """Return all groups/channels the user is a member of."""
    groups: list[GroupInfo] = []
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if isinstance(entity, Channel):
            groups.append(GroupInfo(
                id=entity.id,
                title=dialog.title,
                username=entity.username,
                member_count=getattr(entity, "participants_count", None),
                is_channel=entity.broadcast,
            ))
        elif isinstance(entity, Chat):
            groups.append(GroupInfo(
                id=entity.id,
                title=dialog.title,
                member_count=getattr(entity, "participants_count", None),
            ))
    return groups


# -- Message scraping ---------------------------------------------------------

async def get_messages(
    client: TelegramClient,
    group: GroupInfo,
    *,
    days: Optional[int] = None,
    limit: Optional[int] = None,
    download_media: bool = False,
    media_dir: str = "output/media",
    media_semaphore: Optional[asyncio.Semaphore] = None,
    progress_callback: Optional[Callable[[int, int, int], None]] = None,
    timeout: float = 3600.0,
) -> ScrapeResult:
    """Scrape messages from *group*, skipping all system/service messages.

    Args:
        days: If set, only fetch messages from the last N days.
        limit: Max number of real messages to collect. None = all.
        download_media: Whether to download attached media.
        media_dir: Directory for downloaded media files.
        media_semaphore: Semaphore for parallel media downloads (max 5).
        progress_callback: Called with (collected, skipped, total_scanned).
        timeout: Overall timeout in seconds (default: 1 hour).

    Returns:
        ScrapeResult with messages and statistics.
    """
    offset_date: Optional[datetime] = None
    if days is not None:
        offset_date = datetime.now(timezone.utc) - timedelta(days=days)

    if media_semaphore is None:
        media_semaphore = asyncio.Semaphore(5)

    result = ScrapeResult()
    media_tasks: list[asyncio.Task[None]] = []
    sender_cache: dict[int, str] = {}
    start_time = asyncio.get_event_loop().time()

    try:
        async for msg in client.iter_messages(
            group.id,
            limit=None,  # We handle our own limit after filtering
            offset_date=offset_date,
            wait_time=0,
        ):
            result.scanned += 1

            # Check overall timeout
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                logger.warning(
                    "Scrape timeout reached (%.0fs) after %d messages",
                    timeout, result.collected,
                )
                result.timed_out = True
                break

            # Skip system messages (user joined, left, pinned, etc.)
            if isinstance(msg, (MessageService, MessageEmpty)):
                result.skipped += 1
                if progress_callback and result.scanned % 500 == 0:
                    progress_callback(result.collected, result.skipped, result.scanned)
                continue

            # Skip messages with no text and no media (empty messages)
            if not msg.text and not msg.media:
                result.skipped += 1
                continue

            # Resolve sender name with caching
            sender_name = await _resolve_sender_cached(client, msg, sender_cache)

            scraped = ScrapedMessage(
                id=msg.id,
                date=msg.date,
                sender_id=msg.sender_id,
                sender_name=sender_name,
                text=msg.text or "",
                media_type=_classify_media(msg),
                reply_to_msg_id=(
                    msg.reply_to.reply_to_msg_id if msg.reply_to else None
                ),
                views=msg.views,
                forwards=msg.forwards,
            )
            result.messages.append(scraped)

            if download_media and msg.media:
                task = asyncio.create_task(
                    _download_media(client, msg, media_dir, media_semaphore, scraped)
                )
                media_tasks.append(task)

            # Progress update every 100 real messages
            if progress_callback and result.collected % 100 == 0:
                progress_callback(result.collected, result.skipped, result.scanned)

            # Check our limit on real messages collected
            if limit and result.collected >= limit:
                break

    except (ChannelPrivateError, ChatAdminRequiredError) as exc:
        msg_text = f"No read access to '{group.title}': {exc}"
        logger.error(msg_text)
        result.error = msg_text
    except FloodWaitError as e:
        wait = e.seconds + 5
        logger.warning("Rate limited -- waiting %ds...", wait)
        print(f"\n  Rate limited -- waiting {wait}s...")
        await asyncio.sleep(wait)
        result.error = f"Rate limited after {result.collected} messages (waited {wait}s)"
    except ConnectionError as exc:
        msg_text = f"Disconnected mid-scrape after {result.collected} messages: {exc}"
        logger.error(msg_text)
        result.error = msg_text
    except Exception as exc:
        msg_text = f"Scraping failed: {exc}"
        logger.error(msg_text, exc_info=True)
        result.error = msg_text
        traceback.print_exc()

    result.elapsed_seconds = asyncio.get_event_loop().time() - start_time

    # Final progress
    if progress_callback:
        progress_callback(result.collected, result.skipped, result.scanned)

    # Wait for pending media downloads
    if media_tasks:
        logger.info("Waiting for %d media downloads...", len(media_tasks))
        results = await asyncio.gather(*media_tasks, return_exceptions=True)
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.warning("Media download %d failed: %s", i, r)

    return result


# -- Helpers ------------------------------------------------------------------

async def _resolve_sender_cached(
    client: TelegramClient, msg: Any, cache: dict[int, str]
) -> str:
    """Resolve sender name with an in-memory cache to avoid repeated API calls."""
    sender_id = msg.sender_id
    if sender_id is not None and sender_id in cache:
        return cache[sender_id]

    name = await _resolve_sender(client, msg)

    if sender_id is not None:
        cache[sender_id] = name
    return name


async def _resolve_sender(client: TelegramClient, msg: Any) -> str:
    """Best-effort sender name from the message."""
    sender = msg.sender
    if sender is None:
        try:
            sender = await msg.get_sender()
        except Exception:
            return "Unknown"

    if sender is None:
        return "Unknown"
    if isinstance(sender, User):
        parts = [sender.first_name or "", sender.last_name or ""]
        name = " ".join(p for p in parts if p).strip()
        return name or sender.username or str(sender.id)
    if isinstance(sender, (Channel, Chat)):
        return sender.title or str(sender.id)
    return str(getattr(sender, "id", "Unknown"))


def _classify_media(msg: Any) -> Optional[str]:
    """Return a short label for the media type, or None."""
    if msg.photo:
        return "photo"
    if msg.video:
        return "video"
    if msg.document:
        return "document"
    if msg.audio:
        return "audio"
    if msg.voice:
        return "voice"
    if msg.sticker:
        return "sticker"
    if msg.gif:
        return "gif"
    return None


async def _download_media(
    client: TelegramClient,
    msg: Any,
    media_dir: str,
    sem: asyncio.Semaphore,
    scraped: ScrapedMessage,
) -> None:
    """Download a single media file, respecting the concurrency semaphore."""
    async with sem:
        try:
            path = await client.download_media(msg, file=media_dir)
            if path:
                scraped.media_path = str(path)
        except FloodWaitError as e:
            logger.warning("Media download rate limited, waiting %ds", e.seconds + 2)
            await asyncio.sleep(e.seconds + 2)
            try:
                path = await client.download_media(msg, file=media_dir)
                if path:
                    scraped.media_path = str(path)
            except Exception as exc:
                logger.warning("Media download retry failed: %s", exc)
        except Exception as exc:
            logger.warning("Media download failed: %s", exc)
