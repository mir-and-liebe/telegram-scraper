# Telegram Scraper

Read-only Telegram group/channel scraper with QR code authentication and multi-format export (JSON, CSV, Obsidian Markdown).

## Features

- **QR code login** — scan from your Telegram app, no bot token needed
- **Phone auth fallback** — OTP + 2FA support
- **Read-only** — never sends messages, only reads
- **Full history or date-bounded** scraping
- **Batch scraping** — provide a CSV of groups to scrape
- **Export formats** — JSON, CSV, and Obsidian-flavoured Markdown
- **Rate limiting** — automatic FloodWaitError handling with backoff
- **Media downloads** — optional, with parallel downloads (max 5 concurrent)
- **Session persistence** — re-uses auth across runs

## Quick start

```bash
# Clone and install
git clone <repo-url> && cd telegram-scraper
pip install -r requirements.txt

# Run
python main.py
```

On first run you'll see an ASCII QR code. Scan it with Telegram (Settings → Devices → Link Desktop Device). Your session is saved to `telegram_scraper.session` for future runs.

## Usage

The interactive menu offers:

1. **List groups** — see all groups/channels you're in
2. **Scrape a group** — pick a group, choose mode (full / last N days / last N messages), select export formats
3. **Batch scrape** — provide a CSV file with a `group` column (usernames or IDs)
4. **Exit**

### Batch CSV format

```csv
group
@channelname
-1001234567890
some_public_group
```

## Configuration

API credentials default to Telegram Desktop's public values. Override with environment variables:

```bash
export TELEGRAM_API_ID=2040
export TELEGRAM_API_HASH=b18441a1ff607e10a989891a5462e627
```

## Output

Files are written to the `output/` directory by default:

- `output/Group_Name.json`
- `output/Group_Name.csv`
- `output/Group_Name.md` (Obsidian-ready with YAML front matter)

## Project structure

```
main.py              Entry point
scraper/
  __init__.py
  auth.py            QR code + phone authentication
  client.py          Singleton TelegramClient
  scrape.py          Message retrieval with rate limiting
  export.py          JSON / CSV / Markdown exporters
  cli.py             Interactive CLI menu
```

## Requirements

- Python 3.10+
- telethon
- qrcode
- aiohttp
