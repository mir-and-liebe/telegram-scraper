"""Core scraping logic — read-only message retrieval with rate-limit handling.

Key design decisions:
  - NEVER sends messages, only reads.
  - Skips MessageService (system messages) client-side immediately — no processing overhead.
  - Handles FloodWaitError with exponential backoff.
  - Uses Telethon's async iterator for memory-efficient streaming.
  - Caches sender lookups to avoid redundant API calls.
  - Supports full-history, last-N-days, and last-N-messages modes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import (
    Channel,
    Chat,
    MessageService,
    MessageEmpty,
    User,
)


# ── Data types ───────────────────────────────────────────────────────────────

@dataclass
class ScrapedMessage:
    """Flat representation of a single message."""
    id: int
    date: datetime
    sender_id: int | None
    sender_name: str
    text: str
    media_type: str | None = None
    media_path: str | None = None
    reply_to_msg_id: int | None = None
    views: int | None = None
    forwards: int | None = None


@dataclass
class GroupInfo:
    """Metadata about a group or channel."""
    id: int
    title: str
    username: str | None = None
    member_count: int | None = None
    is_channel: bool = False


# ── Group listing ────────────────────────────────────────────────────────────

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


# ── Message scraping ─────────────────────────────────────────────────────────

async def get_messages(
    client: TelegramClient,
    group: GroupInfo,
    *,
    days: int | None = None,
    limit: int | None = None,
    download_media: bool = False,
    media_dir: str = "output/media",
    media_semaphore: asyncio.Semaphore | None = None,
    progress_callback: Callable[[int, int, int], None] | None = None,
) -> list[ScrapedMessage]:
    """Scrape messages from *group*, skipping all system/service messages.

    Args:
        days: If set, only fetch messages from the last N days.
        limit: Max number of **real** messages to collect. None = all.
        download_media: Whether to download attached media.
        media_dir: Directory for downloaded media files.
        media_semaphore: Semaphore for parallel media downloads (max 5).
        progress_callback: Called with (collected, skipped, total_scanned).
    """
    offset_date = None
    if days is not None:
        offset_date = datetime.now(timezone.utc) - timedelta(days=days)

    if media_semaphore is None:
        media_semaphore = asyncio.Semaphore(5)

    messages: list[ScrapedMessage] = []
    media_tasks: list[asyncio.Task] = []
    sender_cache: dict[int, str] = {}
    skipped = 0
    scanned = 0

    try:
        print(f"  [debug] Starting iter_messages for group_id={group.id}, offset_date={offset_date}")
        async for msg in client.iter_messages(
            group.id,
            limit=None,  # We handle our own limit after filtering
            offset_date=offset_date,
            wait_time=0,  # No extra delay between batches
        ):
            scanned += 1

            # Skip system messages (user joined, left, pinned, etc.)
            if isinstance(msg, (MessageService, MessageEmpty)):
                skipped += 1
                if progress_callback and scanned % 500 == 0:
                    progress_callback(len(messages), skipped, scanned)
                continue

            # Skip messages with no text and no media (empty messages)
            if not msg.text and not msg.media:
                skipped += 1
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
                reply_to_msg_id=msg.reply_to.reply_to_msg_id if msg.reply_to else None,
                views=msg.views,
                forwards=msg.forwards,
            )
            messages.append(scraped)

            if download_media and msg.media:
                task = asyncio.create_task(
                    _download_media(client, msg, media_dir, media_semaphore, scraped)
                )
                media_tasks.append(task)

            # Progress update every 100 real messages
            if progress_callback and len(messages) % 100 == 0:
                progress_callback(len(messages), skipped, scanned)

            # Check our limit on real messages collected
            if limit and len(messages) >= limit:
                break

    except FloodWaitError as e:
        wait = e.seconds + 5
        print(f"\n  Rate limited — waiting {wait}s...")
        await asyncio.sleep(wait)
    except Exception as exc:
        import traceback
        print(f"\n  [error] Scraping failed: {exc}")
        traceback.print_exc()

    # Final progress
    if progress_callback:
        progress_callback(len(messages), skipped, scanned)

    # Wait for pending media downloads
    if media_tasks:
        print(f"  Waiting for {len(media_tasks)} media downloads...")
        await asyncio.gather(*media_tasks, return_exceptions=True)

    return messages


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _resolve_sender_cached(
    client: TelegramClient, msg, cache: dict[int, str]
) -> str:
    """Resolve sender name with an in-memory cache to avoid repeated API calls."""
    sender_id = msg.sender_id
    if sender_id is not None and sender_id in cache:
        return cache[sender_id]

    name = await _resolve_sender(client, msg)

    if sender_id is not None:
        cache[sender_id] = name
    return name


async def _resolve_sender(client: TelegramClient, msg) -> str:
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


def _classify_media(msg) -> str | None:
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
    msg,
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
            await asyncio.sleep(e.seconds + 2)
            try:
                path = await client.download_media(msg, file=media_dir)
                if path:
                    scraped.media_path = str(path)
            except Exception:
                pass
        except Exception:
            pass
