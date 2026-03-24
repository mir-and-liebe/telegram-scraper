"""Authentication via QR code (primary) or phone number (fallback).

QR login flow:
  1. Call client.qr_login() to get a QR URL.
  2. Render the URL as an ASCII QR code in the terminal.
  3. Wait for the user to scan it in their Telegram app.
  4. On expiry, regenerate and display a new code.
  5. Aggressively check is_user_authorized() to detect success.

Phone login flow (fallback):
  1. Prompt for phone number.
  2. Telegram sends an OTP code.
  3. Prompt for the code (and 2FA password if enabled).
"""

from __future__ import annotations

import asyncio
import io
import logging
import sys

import qrcode  # type: ignore[import-untyped]
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

logger = logging.getLogger(__name__)


# -- QR code auth ------------------------------------------------------------

async def qr_login(client: TelegramClient) -> bool:
    """Attempt QR-code login. Returns True on success, False on failure."""
    try:
        qr = await client.qr_login()
    except Exception as exc:
        logger.warning("QR login unavailable: %s", exc)
        return False

    print("\nScan this QR code with your Telegram app:")
    print("  (Telegram -> Settings -> Devices -> Link Desktop Device)\n")
    _render_qr(qr.url)

    max_attempts = 20
    for attempt in range(max_attempts):
        # Check authorization *before* waiting -- the scan may already
        # have succeeded between iterations.
        if await client.is_user_authorized():
            print("\nQR login successful!")
            return True

        try:
            await qr.wait(timeout=15)
            # wait() returned normally -- check auth
            if await client.is_user_authorized():
                print("\nQR login successful!")
                return True
        except asyncio.TimeoutError:
            pass  # will check auth at top of loop
        except SessionPasswordNeededError:
            return await _handle_2fa(client)
        except Exception as exc:
            # Some exceptions are thrown on successful login too
            if await client.is_user_authorized():
                print("\nQR login successful!")
                return True
            logger.debug("QR wait error (attempt %d): %s", attempt + 1, exc)

        # If not authorized, try to regenerate the QR code
        if not await client.is_user_authorized():
            try:
                await qr.recreate()
                print(
                    f"\nQR code expired -- scan the new one "
                    f"(attempt {attempt + 2}/{max_attempts}):\n"
                )
                _render_qr(qr.url)
            except Exception:
                if await client.is_user_authorized():
                    print("\nQR login successful!")
                    return True
                logger.debug("Failed to recreate QR code on attempt %d", attempt + 1)

    # Final check
    if await client.is_user_authorized():
        print("\nQR login successful!")
        return True
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


# -- Phone auth (fallback) ---------------------------------------------------

async def phone_login(client: TelegramClient) -> bool:
    """Interactive phone-number login. Returns True on success."""
    phone = input(
        "\nEnter your phone number (with country code, e.g. +1234567890): "
    ).strip()
    if not phone:
        print("No phone number provided.")
        return False

    try:
        await client.send_code_request(phone)
    except Exception as exc:
        logger.error("Failed to send code: %s", exc, exc_info=True)
        return False

    code = input("Enter the code you received: ").strip()

    try:
        await client.sign_in(phone, code)
        print("Phone login successful!")
        return True
    except SessionPasswordNeededError:
        return await _handle_2fa(client)
    except Exception as exc:
        logger.error("Sign-in failed: %s", exc, exc_info=True)
        return False


# -- Helpers ------------------------------------------------------------------

async def _handle_2fa(client: TelegramClient) -> bool:
    """Prompt for 2FA password and complete login."""
    password = input(
        "Two-factor authentication enabled. Enter your password: "
    ).strip()
    try:
        await client.sign_in(password=password)
        print("Login successful (2FA)!")
        return True
    except Exception as exc:
        logger.error("2FA login failed: %s", exc, exc_info=True)
        return False


async def ensure_authorized(client: TelegramClient) -> None:
    """Make sure the client is connected and authorized, prompting if needed."""
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        if me is not None:
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
