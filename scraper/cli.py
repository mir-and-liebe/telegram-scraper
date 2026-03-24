"""Interactive CLI menu for the Telegram scraper."""

from __future__ import annotations

import asyncio
import csv as csv_mod
import sys
from pathlib import Path

from .auth import ensure_authorized
from .client import get_client
from .export import export_all
from .scrape import GroupInfo, get_groups, get_messages


# ── Main menu ────────────────────────────────────────────────────────────────

async def run_cli() -> None:
    """Top-level interactive menu loop."""
    client = get_client()
    await ensure_authorized(client)

    while True:
        print("\n" + "=" * 50)
        print("  Telegram Scraper — Main Menu")
        print("=" * 50)
        print("  1) List groups & channels")
        print("  2) Scrape a group/channel")
        print("  3) Batch scrape from CSV")
        print("  4) Exit")
        print()

        choice = input("Select an option [1-4]: ").strip()

        if choice == "1":
            await _list_groups(client)
        elif choice == "2":
            await _interactive_scrape(client)
        elif choice == "3":
            await _batch_scrape(client)
        elif choice == "4":
            print("Goodbye.")
            break
        else:
            print("Invalid choice.")


# ── List groups ──────────────────────────────────────────────────────────────

async def _list_groups(client) -> list[GroupInfo]:
    """Fetch and display all groups/channels."""
    print("\nFetching groups and channels...")
    groups = await get_groups(client)

    if not groups:
        print("No groups or channels found.")
        return []

    print(f"\nFound {len(groups)} groups/channels:\n")
    print(f"  {'#':<4} {'Title':<40} {'Type':<10} {'Members':<10} {'Username'}")
    print(f"  {'─'*4} {'─'*40} {'─'*10} {'─'*10} {'─'*20}")

    for i, g in enumerate(groups, 1):
        kind = "Channel" if g.is_channel else "Group"
        members = str(g.member_count) if g.member_count else "—"
        uname = f"@{g.username}" if g.username else "—"
        title = g.title[:38] if len(g.title) > 38 else g.title
        print(f"  {i:<4} {title:<40} {kind:<10} {members:<10} {uname}")

    return groups


# ── Interactive single scrape ────────────────────────────────────────────────

async def _interactive_scrape(client) -> None:
    """Let the user pick a group and configure the scrape."""
    groups = await _list_groups(client)
    if not groups:
        return

    # Select group
    raw = input(f"\nEnter group number [1-{len(groups)}]: ").strip()
    try:
        idx = int(raw) - 1
        group = groups[idx]
    except (ValueError, IndexError):
        print("Invalid selection.")
        return

    # Configure scrape
    config = _get_scrape_config()

    # Run scrape
    print(f"\nScraping: {group.title}...")
    messages = await get_messages(
        client,
        group,
        days=config["days"],
        limit=config["limit"],
        download_media=config["download_media"],
        progress_callback=_progress,
    )

    if not messages:
        print("No messages found.")
        return

    print(f"\nFetched {len(messages)} messages.")

    # Export
    results = export_all(messages, group, config["output_dir"], config["formats"])
    _print_export_results(results)


# ── Batch scrape from CSV ────────────────────────────────────────────────────

async def _batch_scrape(client) -> None:
    """Scrape multiple groups listed in a CSV file.

    CSV format: one column named 'group' with usernames or IDs.
    """
    csv_path = input("\nPath to CSV file with group list: ").strip()
    if not csv_path or not Path(csv_path).is_file():
        print("File not found.")
        return

    config = _get_scrape_config()

    # Read group identifiers from CSV
    targets: list[str] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv_mod.DictReader(f)
        for row in reader:
            val = row.get("group", "").strip()
            if val:
                targets.append(val)

    if not targets:
        print("No groups found in CSV.")
        return

    print(f"\nBatch scraping {len(targets)} groups...")

    for target in targets:
        print(f"\n{'─'*50}")
        print(f"Scraping: {target}")
        try:
            entity = await client.get_entity(target)
            group = GroupInfo(
                id=entity.id,
                title=getattr(entity, "title", target),
                username=getattr(entity, "username", None),
                is_channel=getattr(entity, "broadcast", False),
            )
            messages = await get_messages(
                client,
                group,
                days=config["days"],
                limit=config["limit"],
                download_media=config["download_media"],
                progress_callback=_progress,
            )
            if messages:
                results = export_all(messages, group, config["output_dir"], config["formats"])
                _print_export_results(results)
            else:
                print("  No messages found.")
        except Exception as exc:
            print(f"  Error: {exc}")

    print(f"\nBatch scrape complete.")


# ── Config prompts ───────────────────────────────────────────────────────────

def _get_scrape_config() -> dict:
    """Prompt user for scrape parameters."""
    print("\n  Scrape mode:")
    print("    1) Full history")
    print("    2) Last N days")
    print("    3) Last N messages")

    mode = input("  Select mode [1-3] (default: 1): ").strip() or "1"

    days = None
    limit = None
    if mode == "2":
        raw = input("  Number of days: ").strip()
        days = int(raw) if raw.isdigit() else 7
    elif mode == "3":
        raw = input("  Number of messages: ").strip()
        limit = int(raw) if raw.isdigit() else 1000

    # Export formats
    print("\n  Export formats (comma-separated):")
    print("    json, csv, markdown")
    fmt_input = input("  Formats (default: json,csv,markdown): ").strip()
    if fmt_input:
        formats = [f.strip().lower() for f in fmt_input.split(",")]
    else:
        formats = ["json", "csv", "markdown"]

    # Media
    media_input = input("  Download media? [y/N] (default: N): ").strip().lower()
    download_media = media_input in ("y", "yes")

    # Output directory
    output_dir = input("  Output directory (default: output): ").strip() or "output"

    return {
        "days": days,
        "limit": limit,
        "formats": formats,
        "download_media": download_media,
        "output_dir": output_dir,
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _progress(collected: int, skipped: int, scanned: int) -> None:
    print(f"\r  Scanned {scanned} | Collected {collected} messages | Skipped {skipped} system msgs", end="", flush=True)


def _print_export_results(results: dict[str, Path]) -> None:
    print("\n  Exported:")
    for fmt, path in results.items():
        print(f"    {fmt:>10}: {path}")
