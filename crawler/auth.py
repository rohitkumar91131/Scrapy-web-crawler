"""Email-verification authentication automation.

Uses the 1secmail temporary email API and Playwright to:
  1. Generate a temporary email address.
  2. Automate signup on a target website.
  3. Poll the inbox until a verification email arrives.
  4. Extract the verification link and click it.
  5. Automate login and save the authenticated session to session.json.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import string
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 1secmail helpers
# ---------------------------------------------------------------------------

_SECMAIL_API = "https://www.1secmail.com/api/v1/"
_SECMAIL_DOMAINS = ["1secmail.com", "1secmail.org", "1secmail.net"]


def generate_temp_email(username: Optional[str] = None) -> tuple[str, str, str]:
    """Return (email, username, domain) for a new 1secmail address.

    If *username* is not provided a random one is generated.
    """
    if username is None:
        username = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    domain = random.choice(_SECMAIL_DOMAINS)
    email = f"{username}@{domain}"
    return email, username, domain


def _list_messages(username: str, domain: str) -> list[dict]:
    """Return raw message list from the 1secmail API."""
    try:
        resp = requests.get(
            _SECMAIL_API,
            params={"action": "getMessages", "login": username, "domain": domain},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json() if isinstance(resp.json(), list) else []
    except Exception as exc:  # noqa: BLE001
        logger.warning("1secmail list failed: %s", exc)
        return []


def _fetch_message(username: str, domain: str, msg_id: int) -> dict:
    """Fetch the full message body from the 1secmail API."""
    try:
        resp = requests.get(
            _SECMAIL_API,
            params={
                "action": "readMessage",
                "login": username,
                "domain": domain,
                "id": msg_id,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json() if isinstance(resp.json(), dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("1secmail fetch failed: %s", exc)
        return {}


def wait_for_verification_email(
    username: str,
    domain: str,
    timeout: int = 120,
    poll_interval: int = 5,
) -> Optional[str]:
    """Poll the 1secmail inbox until a verification/confirmation email arrives.

    Returns the full HTML/text body of the first matching email, or *None* on
    timeout.
    """
    deadline = time.monotonic() + timeout
    seen_ids: set[int] = set()
    logger.info("Waiting for verification email at %s@%s …", username, domain)
    while time.monotonic() < deadline:
        messages = _list_messages(username, domain)
        for msg in messages:
            msg_id = msg.get("id")
            if msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            subject = (msg.get("subject") or "").lower()
            # Accept any email that looks like a verification / activation message
            if any(
                kw in subject
                for kw in ("verify", "verif", "confirm", "activate", "activation")
            ):
                full = _fetch_message(username, domain, msg_id)
                body = full.get("htmlBody") or full.get("textBody") or ""
                if body:
                    logger.info("Verification email received (id=%s).", msg_id)
                    return body
        time.sleep(poll_interval)
    logger.warning("Timed out waiting for verification email.")
    return None


def extract_verification_link(email_body: str) -> Optional[str]:
    """Extract the first https verification/confirm/activate link from *email_body*."""
    pattern = re.compile(
        r'https?://[^\s\'"<>]+(?:verif|confirm|activate|token)[^\s\'"<>]*',
        re.IGNORECASE,
    )
    match = pattern.search(email_body)
    if match:
        return match.group(0)
    # Fallback: any https link
    all_links = re.findall(r'https?://[^\s\'"<>]+', email_body)
    return all_links[0] if all_links else None


# ---------------------------------------------------------------------------
# Playwright helpers (sync API via asyncio bridge)
# ---------------------------------------------------------------------------

def _run_sync(coro):
    """Run *coro* in a new asyncio event loop (blocking)."""
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _signup_async(
    signup_url: str,
    username: str,
    email: str,
    password: str,
    username_selector: str,
    email_selector: str,
    password_selector: str,
    submit_selector: str,
) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(signup_url, wait_until="networkidle", timeout=30000)
        await page.fill(username_selector, username)
        await page.fill(email_selector, email)
        await page.fill(password_selector, password)
        await page.click(submit_selector)
        await page.wait_for_load_state("networkidle", timeout=15000)
        await browser.close()


async def _verify_async(verification_link: str) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(verification_link, wait_until="networkidle", timeout=30000)
        await browser.close()


async def _login_and_save_async(
    login_url: str,
    email: str,
    password: str,
    email_selector: str,
    password_selector: str,
    submit_selector: str,
    session_file: str,
) -> dict:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(login_url, wait_until="networkidle", timeout=30000)
        await page.fill(email_selector, email)
        await page.fill(password_selector, password)
        await page.click(submit_selector)
        await page.wait_for_load_state("networkidle", timeout=15000)
        cookies = await context.cookies()
        local_storage = await page.evaluate(
            "() => Object.fromEntries(Object.entries(localStorage))"
        )
        session_data = {"cookies": cookies, "localStorage": local_storage}
        session_dir = os.path.dirname(os.path.abspath(session_file))
        if session_dir:
            os.makedirs(session_dir, exist_ok=True)
        with open(session_file, "w", encoding="utf-8") as fh:
            json.dump(session_data, fh, indent=2)
        await context.close()
        await browser.close()
        return session_data


# ---------------------------------------------------------------------------
# Public high-level API
# ---------------------------------------------------------------------------

def signup_with_playwright(
    signup_url: str,
    username: str,
    email: str,
    password: str,
    username_selector: str = 'input[name="username"]',
    email_selector: str = 'input[type="email"], input[name="email"]',
    password_selector: str = 'input[type="password"]',
    submit_selector: str = 'button[type="submit"]',
) -> None:
    """Fill and submit the signup form at *signup_url* using Playwright."""
    _run_sync(
        _signup_async(
            signup_url,
            username,
            email,
            password,
            username_selector,
            email_selector,
            password_selector,
            submit_selector,
        )
    )


def complete_verification(verification_link: str) -> None:
    """Visit the verification link with Playwright to activate the account."""
    _run_sync(_verify_async(verification_link))


def login_and_save_session(
    login_url: str,
    email: str,
    password: str,
    session_file: str = "session.json",
    email_selector: str = 'input[type="email"], input[name="email"]',
    password_selector: str = 'input[type="password"]',
    submit_selector: str = 'button[type="submit"]',
) -> dict:
    """Login via Playwright and persist cookies + localStorage to *session_file*."""
    return _run_sync(
        _login_and_save_async(
            login_url,
            email,
            password,
            email_selector,
            password_selector,
            submit_selector,
            session_file,
        )
    )


def load_session(session_file: str = "session.json") -> dict:
    """Load a previously saved session from *session_file*.

    Returns an empty dict if the file does not exist or is unreadable.
    """
    if not os.path.exists(session_file):
        return {}
    try:
        with open(session_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def create_account_with_verification(
    signup_url: str,
    login_url: str,
    password: str,
    session_file: str = "session.json",
    max_retries: int = 3,
    email_timeout: int = 120,
) -> dict:
    """Full end-to-end flow: signup → wait for email → verify → login → save session.

    Returns a dict with account information and status.  On repeated email
    failures it retries with a fresh temporary address (up to *max_retries*
    times).
    """
    attempted_emails: list[str] = []
    for attempt in range(1, max_retries + 1):
        email, username, domain = generate_temp_email()
        attempted_emails.append(email)
        logger.info(
            "Attempt %d/%d – using temp email: %s (previous attempts: %s)",
            attempt,
            max_retries,
            email,
            attempted_emails[:-1] or "none",
        )
        try:
            # Step 1: Signup
            signup_with_playwright(signup_url, username, email, password)

            # Step 2: Wait for verification email
            body = wait_for_verification_email(username, domain, timeout=email_timeout)
            if body is None:
                logger.warning("No verification email received; retrying …")
                continue

            # Step 3: Extract link
            link = extract_verification_link(body)
            if not link:
                logger.warning("Could not extract verification link; retrying …")
                continue

            # Step 4: Click verification link
            complete_verification(link)

            # Step 5: Login and save session
            session = login_and_save_session(login_url, email, password, session_file)

            return {
                "email": email,
                "username": username,
                "password": password,
                "verification_link": link,
                "session_file": session_file,
                "verification_status": "verified",
                "login_status": "success",
            }

        except Exception as exc:  # noqa: BLE001
            logger.error("Attempt %d failed: %s", attempt, exc)

    return {
        "email": None,
        "username": None,
        "password": password,
        "verification_link": None,
        "session_file": session_file,
        "verification_status": "failed",
        "login_status": "failed",
    }
