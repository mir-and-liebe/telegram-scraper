"""Core scraping logic — read-only message retrieval with rate-limit handling.

Key design decisions:
  - NEVER sends messages, only reads.
  - Handles FloodWaitError with exponential backoff.
  - Targets ~3 000 messages/minute throughput (Telegram's practical ceiling).
  - Supports full-history and last-N-days modes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import Channel, Chat, User


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
    progress_callback=None,
) -> list[ScrapedMessage]:
    """Scrape messages from *group*.

    Args:
        days: If set, only fetch messages from the last N days.
        limit: Max number of messages to fetch. None = all.
        download_media: Whether to download attached media.
        media_dir: Directory for downloaded media files.
        media_semaphore: Semaphore for parallel media downloads (max 5).
        progress_callback: Called with (fetched_count, total_estimate) periodically.
    """
    offset_date = None
    if days is not None:
        offset_date = datetime.now(timezone.utc) - timedelta(days=days)

    if media_semaphore is None:
        media_semaphore = asyncio.Semaphore(5)

    messages: list[ScrapedMessage] = []
    media_tasks: list[asyncio.Task] = []
    count = 0
    batch_size = 100  # Telegram returns up to 100 per request

    while True:
        try:
            batch: list = []
            async for msg in client.iter_messages(
                group.id,
                limit=min(batch_size, limit - count) if limit else batch_size,
                offset_date=offset_date if not messages else None,
                min_id=0,
                reverse=False,
            ):
                # If we're doing a date-bounded scrape, stop at the boundary
                if offset_date and msg.date and msg.date < offset_date:
                    break

                sender_name = await _resolve_sender(client, msg)
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
                batch.append(scraped)

                if download_media and msg.media:
                    task = asyncio.create_task(
                        _download_media(client, msg, media_dir, media_semaphore, scraped)
                    )
                    media_tasks.append(task)

                count += 1
                if limit and count >= limit:
                    break

            if progress_callback:
                progress_callback(count, limit)

            # If batch was empty or we hit limit, we're done
            if not batch or (limit and count >= limit):
                break

            # Use the oldest message date in this batch as offset for next
            offset_date = batch[-1].date

        except FloodWaitError as e:
            wait = e.seconds + 5  # small buffer
            print(f"\n  Rate limited — waiting {wait}s (FloodWaitError)...")
            await asyncio.sleep(wait)
            continue

    # Wait for any pending media downloads
    if media_tasks:
        print(f"  Waiting for {len(media_tasks)} media downloads...")
        await asyncio.gather(*media_tasks, return_exceptions=True)

    return messages


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _resolve_sender(client: TelegramClient, msg) -> str:
    """Best-effort sender name from the message."""
    if msg.sender is None:
        try:
            sender = await msg.get_sender()
        except Exception:
            sender = None
    else:
        sender = msg.sender

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
