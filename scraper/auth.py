"""Authentication via QR code (primary) or phone number (fallback).

QR login flow:
  1. Call client.qr_login() to get a QR URL.
  2. Render the URL as an ASCII QR code in the terminal.
  3. Wait for the user to scan it in their Telegram app.
  4. On expiry, regenerate and display a new code.

Phone login flow (fallback):
  1. Prompt for phone number.
  2. Telegram sends an OTP code.
  3. Prompt for the code (and 2FA password if enabled).
"""

from __future__ import annotations

import asyncio
import io
import sys

import qrcode
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError


# ── QR code auth ─────────────────────────────────────────────────────────────

async def qr_login(client: TelegramClient) -> bool:
    """Attempt QR-code login. Returns True on success, False on failure."""
    try:
        qr = await client.qr_login()
    except Exception as exc:
        print(f"QR login unavailable: {exc}")
        return False

    print("\nScan this QR code with your Telegram app:")
    print("  (Telegram → Settings → Devices → Link Desktop Device)\n")
    _render_qr(qr.url)

    while True:
        try:
            # wait() raises asyncio.TimeoutError when the QR expires
            await qr.wait(timeout=30)
            print("\nQR login successful!")
            return True
        except asyncio.TimeoutError:
            # QR expired — regenerate
            try:
                await qr.recreate()
                print("\nQR code expired — scan the new one:\n")
                _render_qr(qr.url)
            except Exception:
                # Three consecutive failures → give up
                return False
        except SessionPasswordNeededError:
            # 2FA enabled — need password after QR scan
            return await _handle_2fa(client)
        except Exception as exc:
            print(f"\nQR login error: {exc}")
            return False


def _render_qr(data: str) -> None:
    """Print an ASCII QR code to the terminal."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=1,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)

    buf = io.StringIO()
    qr.print_ascii(out=buf, invert=True)
    print(buf.getvalue())


# ── Phone auth (fallback) ───────────────────────────────────────────────────

async def phone_login(client: TelegramClient) -> bool:
    """Interactive phone-number login. Returns True on success."""
    phone = input("\nEnter your phone number (with country code, e.g. +1234567890): ").strip()
    if not phone:
        print("No phone number provided.")
        return False

    try:
        await client.send_code_request(phone)
    except Exception as exc:
        print(f"Failed to send code: {exc}")
        return False

    code = input("Enter the code you received: ").strip()

    try:
        await client.sign_in(phone, code)
        print("Phone login successful!")
        return True
    except SessionPasswordNeededError:
        return await _handle_2fa(client)
    except Exception as exc:
        print(f"Sign-in failed: {exc}")
        return False


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _handle_2fa(client: TelegramClient) -> bool:
    """Prompt for 2FA password and complete login."""
    password = input("Two-factor authentication enabled. Enter your password: ").strip()
    try:
        await client.sign_in(password=password)
        print("Login successful (2FA)!")
        return True
    except Exception as exc:
        print(f"2FA login failed: {exc}")
        return False


async def ensure_authorized(client: TelegramClient) -> None:
    """Make sure the client is connected and authorized, prompting if needed."""
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        name = me.first_name or me.username or str(me.id)
        print(f"Already logged in as {name}.")
        return

    print("Not logged in. Attempting QR code authentication...")
    if await qr_login(client):
        return

    print("\nQR login failed. Falling back to phone authentication...")
    if await phone_login(client):
        return

    print("Authentication failed. Exiting.")
    sys.exit(1)
