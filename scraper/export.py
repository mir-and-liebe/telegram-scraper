"""Export scraped messages to JSON, CSV, and Obsidian-flavoured Markdown."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .scrape import GroupInfo, ScrapedMessage


# ── JSON ─────────────────────────────────────────────────────────────────────

def export_json(
    messages: list[ScrapedMessage],
    group: GroupInfo,
    output_dir: str = "output",
) -> Path:
    """Write messages to a JSON file. Returns the output path."""
    out = _prepare_dir(output_dir)
    path = out / f"{_safe_name(group.title)}.json"

    data = {
        "group": {
            "id": group.id,
            "title": group.title,
            "username": group.username,
            "is_channel": group.is_channel,
        },
        "scraped_at": _now_iso(),
        "message_count": len(messages),
        "messages": [_msg_dict(m) for m in messages],
    }

    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path


# ── CSV ──────────────────────────────────────────────────────────────────────

CSV_FIELDS = [
    "id", "date", "sender_id", "sender_name", "text",
    "media_type", "media_path", "reply_to_msg_id", "views", "forwards",
]


def export_csv(
    messages: list[ScrapedMessage],
    group: GroupInfo,
    output_dir: str = "output",
) -> Path:
    """Write messages to a CSV file. Returns the output path."""
    out = _prepare_dir(output_dir)
    path = out / f"{_safe_name(group.title)}.csv"

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for m in messages:
            writer.writerow(_msg_dict(m))

    return path


# ── Markdown (Obsidian) ─────────────────────────────────────────────────────

def export_markdown(
    messages: list[ScrapedMessage],
    group: GroupInfo,
    output_dir: str = "output",
) -> Path:
    """Write messages as an Obsidian-ready Markdown file. Returns the output path."""
    out = _prepare_dir(output_dir)
    path = out / f"{_safe_name(group.title)}.md"

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Group messages by date (newest first)
    by_date: dict[str, list[ScrapedMessage]] = defaultdict(list)
    for m in messages:
        day = m.date.strftime("%Y-%m-%d") if m.date else "unknown"
        by_date[day].append(m)

    sorted_dates = sorted(by_date.keys(), reverse=True)

    lines: list[str] = []

    # YAML front matter
    lines.append("---")
    lines.append("tags:")
    lines.append("  - area/capture")
    lines.append("  - type/notes")
    lines.append("  - source/telegram")
    lines.append(f'source_group: "{group.title}"')
    lines.append(f'scraped_at: "{today}"')
    lines.append(f"message_count: {len(messages)}")
    lines.append("---")
    lines.append("")
    lines.append(f"# Messages from: {group.title}")
    lines.append("")

    for date_str in sorted_dates:
        lines.append(f"## {date_str}")
        lines.append("")

        # Sort messages within each day newest-first
        day_msgs = sorted(by_date[date_str], key=lambda m: m.date, reverse=True)

        for m in day_msgs:
            time_str = m.date.strftime("%H:%M") if m.date else "??:??"
            lines.append(f"### {time_str} — {m.sender_name}")
            lines.append("")

            text = m.text.strip() if m.text else ""
            if text:
                lines.append(text)
            if m.media_type:
                lines.append(f"*[{m.media_type}]*")
            if not text and not m.media_type:
                lines.append("*(empty message)*")

            lines.append("")
            lines.append("---")
            lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ── Batch export ─────────────────────────────────────────────────────────────

def export_all(
    messages: list[ScrapedMessage],
    group: GroupInfo,
    output_dir: str = "output",
    formats: list[str] | None = None,
) -> dict[str, Path]:
    """Export to all requested formats. Returns {format: path}."""
    if formats is None:
        formats = ["json", "csv", "markdown"]

    results: dict[str, Path] = {}
    exporters = {
        "json": export_json,
        "csv": export_csv,
        "markdown": export_markdown,
    }

    for fmt in formats:
        fn = exporters.get(fmt)
        if fn:
            results[fmt] = fn(messages, group, output_dir)

    return results


# ── Helpers ──────────────────────────────────────────────────────────────────

def _prepare_dir(output_dir: str) -> Path:
    p = Path(output_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_name(title: str) -> str:
    """Sanitise a group title for use as a filename."""
    return "".join(c if c.isalnum() or c in " _-" else "_" for c in title).strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _msg_dict(m: ScrapedMessage) -> dict:
    return {
        "id": m.id,
        "date": m.date.isoformat() if m.date else None,
        "sender_id": m.sender_id,
        "sender_name": m.sender_name,
        "text": m.text,
        "media_type": m.media_type,
        "media_path": m.media_path,
        "reply_to_msg_id": m.reply_to_msg_id,
        "views": m.views,
        "forwards": m.forwards,
    }
