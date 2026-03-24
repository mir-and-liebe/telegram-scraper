"""Interactive CLI menu for the Telegram scraper."""

from __future__ import annotations

import asyncio
import csv as csv_mod
import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

from .auth import ensure_authorized
from .client import get_client, install_signal_handlers
from .export import export_all
from .scrape import GroupInfo, ScrapeResult, get_groups, get_messages

logger = logging.getLogger(__name__)

# Path to persist the last-used scrape config for "quick scrape"
_LAST_CONFIG_PATH = Path.cwd() / ".scraper_last_config.json"


# -- Logging setup ------------------------------------------------------------

def _setup_logging(debug: bool = False) -> None:
    """Configure the root logger for the scraper package."""
    level = logging.DEBUG if debug else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=level, format=fmt, stream=sys.stderr)
    # Suppress noisy Telethon logs unless in debug mode
    if not debug:
        logging.getLogger("telethon").setLevel(logging.WARNING)


# -- Main menu ----------------------------------------------------------------

async def run_cli() -> None:
    """Top-level interactive menu loop."""
    debug = os.environ.get("SCRAPER_DEBUG", "").lower() in ("1", "true", "yes")
    _setup_logging(debug=debug)

    install_signal_handlers()

    client = get_client()
    await ensure_authorized(client)

    while True:
        print("\n" + "=" * 50)
        print("  Telegram Scraper -- Main Menu")
        print("=" * 50)
        print("  1) List groups & channels")
        print("  2) Scrape a group/channel")
        print("  3) Batch scrape from CSV")
        print("  4) Quick scrape (reuse last settings)")
        print("  5) Exit")
        print()

        choice = input("Select an option [1-5]: ").strip()

        try:
            if choice == "1":
                await _list_groups(client)
            elif choice == "2":
                await _interactive_scrape(client)
            elif choice == "3":
                await _batch_scrape(client)
            elif choice == "4":
                await _quick_scrape(client)
            elif choice == "5":
                print("Goodbye.")
                break
            else:
                print("Invalid choice.")
        except KeyboardInterrupt:
            print("\n\nOperation cancelled.")
        except Exception as exc:
            logger.error("Unexpected error: %s", exc, exc_info=True)
            print(f"\n  ERROR: {exc}")


# -- List groups --------------------------------------------------------------

async def _list_groups(client: Any) -> list[GroupInfo]:
    """Fetch and display all groups/channels."""
    print("\nFetching groups and channels...")
    groups = await get_groups(client)

    if not groups:
        print("No groups or channels found.")
        return []

    print(f"\nFound {len(groups)} groups/channels:\n")
    print(f"  {'#':<4} {'Title':<40} {'Type':<10} {'Members':<10} {'Username'}")
    print(f"  {'---':<4} {'---':<40} {'---':<10} {'---':<10} {'---':<20}")

    for i, g in enumerate(groups, 1):
        kind = "Channel" if g.is_channel else "Group"
        members = str(g.member_count) if g.member_count else "--"
        uname = f"@{g.username}" if g.username else "--"
        title = g.title[:38] if len(g.title) > 38 else g.title
        print(f"  {i:<4} {title:<40} {kind:<10} {members:<10} {uname}")

    return groups


# -- Interactive single scrape ------------------------------------------------

async def _interactive_scrape(client: Any) -> None:
    """Let the user pick a group and configure the scrape."""
    groups = await _list_groups(client)
    if not groups:
        return

    # Select group
    raw = input(f"\nEnter group number [1-{len(groups)}]: ").strip()
    try:
        idx = int(raw) - 1
        if idx < 0 or idx >= len(groups):
            raise IndexError
        group = groups[idx]
    except (ValueError, IndexError):
        print("Invalid selection.")
        return

    # Configure scrape
    config = _get_scrape_config()
    _save_last_config(config, group)

    # Run scrape
    await _run_scrape(client, group, config)


# -- Quick scrape (reuse last settings) --------------------------------------

async def _quick_scrape(client: Any) -> None:
    """Rerun the last scrape with the same settings."""
    saved = _load_last_config()
    if saved is None:
        print("\nNo previous scrape settings found. Use option 2 first.")
        return

    config = saved["config"]
    group = GroupInfo(**saved["group"])

    print(f"\nQuick scrape: {group.title}")
    print(f"  Config: days={config['days']}, limit={config['limit']}, "
          f"media={config['download_media']}")
    print(f"  Formats: {config['formats']}, output: {config['output_dir']}")

    confirm = input("\nProceed? [Y/n]: ").strip().lower()
    if confirm in ("n", "no"):
        return

    await _run_scrape(client, group, config)


# -- Batch scrape from CSV ---------------------------------------------------

async def _batch_scrape(client: Any) -> None:
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
    total_messages = 0
    successes = 0
    failures = 0

    for target in targets:
        print(f"\n{'---' * 17}")
        print(f"Scraping: {target}")
        try:
            entity = await client.get_entity(target)
            group = GroupInfo(
                id=entity.id,
                title=getattr(entity, "title", target),
                username=getattr(entity, "username", None),
                is_channel=getattr(entity, "broadcast", False),
            )
            result = await get_messages(
                client,
                group,
                days=config["days"],
                limit=config["limit"],
                download_media=config["download_media"],
                progress_callback=_progress,
            )
            if result.messages:
                export_results = export_all(
                    result.messages, group, config["output_dir"], config["formats"]
                )
                _print_export_results(export_results)
                total_messages += result.collected
                successes += 1
            else:
                print("  No messages found.")
                if result.error:
                    print(f"  Reason: {result.error}")
                failures += 1
        except Exception as exc:
            logger.error("Failed to scrape %s: %s", target, exc, exc_info=True)
            print(f"  Error: {exc}")
            failures += 1

    # Batch summary
    print(f"\n{'=' * 50}")
    print(f"  Batch scrape complete")
    print(f"  Succeeded: {successes}/{len(targets)}")
    print(f"  Failed:    {failures}/{len(targets)}")
    print(f"  Total messages collected: {total_messages}")
    print(f"{'=' * 50}")


# -- Core scrape runner -------------------------------------------------------

async def _run_scrape(client: Any, group: GroupInfo, config: dict[str, Any]) -> None:
    """Execute a scrape and display results."""
    print(f"\nScraping: {group.title}...")
    print(f"  Config: days={config['days']}, limit={config['limit']}, "
          f"media={config['download_media']}")
    print(f"  Formats: {config['formats']}, output: {config['output_dir']}")

    result = await get_messages(
        client,
        group,
        days=config["days"],
        limit=config["limit"],
        download_media=config["download_media"],
        progress_callback=_progress,
    )

    # Clear the progress line
    _clear_line()
    print()

    # Print summary
    _print_scrape_summary(group, result)

    if not result.messages:
        return

    # Export
    print("Exporting...")
    export_results = export_all(
        result.messages, group, config["output_dir"], config["formats"]
    )
    _print_export_results(export_results)


# -- Config prompts -----------------------------------------------------------

def _get_scrape_config() -> dict[str, Any]:
    """Prompt user for scrape parameters."""
    print("\n  Scrape mode:")
    print("    1) Full history")
    print("    2) Last N days")
    print("    3) Last N messages")

    mode = input("  Select mode [1-3] (default: 1): ").strip() or "1"

    days: Optional[int] = None
    limit: Optional[int] = None
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


def _save_last_config(config: dict[str, Any], group: GroupInfo) -> None:
    """Persist scrape config for quick-scrape reuse."""
    data = {
        "config": config,
        "group": {
            "id": group.id,
            "title": group.title,
            "username": group.username,
            "member_count": group.member_count,
            "is_channel": group.is_channel,
        },
    }
    try:
        _LAST_CONFIG_PATH.write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("Could not save last config: %s", exc)


def _load_last_config() -> Optional[dict[str, Any]]:
    """Load previously saved scrape config."""
    if not _LAST_CONFIG_PATH.is_file():
        return None
    try:
        return json.loads(_LAST_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not load last config: %s", exc)
        return None


# -- Display helpers ----------------------------------------------------------

def _clear_line() -> None:
    """Clear the current terminal line properly."""
    columns = shutil.get_terminal_size().columns
    print(f"\r{' ' * columns}\r", end="", flush=True)


def _progress(collected: int, skipped: int, scanned: int) -> None:
    """Display scrape progress, clearing the full line."""
    _clear_line()
    print(
        f"  Scanned {scanned} | Collected {collected} messages "
        f"| Skipped {skipped} system msgs",
        end="",
        flush=True,
    )


def _print_scrape_summary(group: GroupInfo, result: ScrapeResult) -> None:
    """Print a clear summary after scraping completes."""
    print(f"\n{'=' * 50}")
    print(f"  Scrape Summary: {group.title}")
    print(f"{'=' * 50}")
    print(f"  Messages collected: {result.collected}")
    print(f"  Messages skipped:   {result.skipped}")
    print(f"  Total scanned:      {result.scanned}")
    print(f"  Time elapsed:       {result.elapsed_seconds:.1f}s")
    if result.timed_out:
        print("  WARNING: Scrape timed out before completion")
    if result.error:
        print(f"  ERROR: {result.error}")
    print(f"{'=' * 50}")


def _print_export_results(results: dict[str, Path]) -> None:
    """Display exported file paths."""
    print("\n  Exported files:")
    for fmt, path in results.items():
        size = path.stat().st_size if path.exists() else 0
        size_str = _format_size(size)
        print(f"    {fmt:>10}: {path}  ({size_str})")


def _format_size(size: int) -> str:
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024  # type: ignore[assignment]
    return f"{size:.1f} TB"
