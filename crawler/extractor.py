"""Single-page content extractor.

Fetches a URL and extracts structured content:
  - Page title & meta description
  - Headings (h1-h6)
  - Paragraphs / visible text
  - Images (src + alt)
  - Hyperlinks

Falls back to Playwright-based rendering when the page requires JavaScript
(e.g. Cloudflare-protected or SPA sites).
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Phrases that indicate a JS-wall / bot-challenge page
_BOT_CHALLENGE_PHRASES = (
    "performing security verification",
    "enable javascript and cookies to continue",
    "just a moment",
    "enable javascript",
    "javascript required",
    "please enable javascript",
    "javascript is required",
    "you need to enable javascript",
)

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_REQUEST_TIMEOUT = 30  # seconds

# Private / loopback / link-local IP networks – block to prevent SSRF
_PRIVATE_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _is_private_host(hostname: str) -> bool:
    """Return True if *hostname* resolves to a private / loopback address."""
    try:
        addrs = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        # Cannot resolve – treat as safe (will fail at fetch time)
        return False
    for _family, _type, _proto, _canon, sockaddr in addrs:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if any(ip in net for net in _PRIVATE_NETWORKS):
            return True
    return False


def _validate_url(url: str) -> str | None:
    """Validate that *url* is a safe, public HTTP(S) URL.

    Returns an error message string if invalid, or None if the URL is OK.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "Only http:// and https:// URLs are supported."
    if not parsed.netloc:
        return "Invalid URL: missing host."
    hostname = parsed.hostname or ""
    if not hostname:
        return "Invalid URL: missing host."
    if _is_private_host(hostname):
        return "Requests to private or loopback addresses are not allowed."
    return None


def _is_bot_challenge(html: str) -> bool:
    """Return True if the HTML looks like a bot/Cloudflare challenge page."""
    lower = html.lower()
    for phrase in _BOT_CHALLENGE_PHRASES:
        if phrase in lower:
            return True
    # Very little visible text in a large HTML body is a strong JS indicator
    soup = BeautifulSoup(html, "lxml")
    visible = soup.get_text(separator=" ", strip=True)
    if len(visible) < 100 and len(html) > 2000:
        return True
    return False


def _fetch_with_playwright(url: str) -> str | None:
    """Fetch a URL via Playwright and return the rendered HTML, or None on error."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import]

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                user_agent=_DEFAULT_HEADERS["User-Agent"],
            )
            page = ctx.new_page()
            page.goto(url, timeout=60_000)
            page.wait_for_load_state("networkidle", timeout=30_000)
            html = page.content()
            browser.close()
            return html
    except Exception as exc:  # noqa: BLE001
        logger.warning("Playwright fetch failed for %s: %s", url, exc)
        return None


def _fetch_html(url: str) -> tuple[str, bool]:
    """Fetch the HTML for *url*, using Playwright if JS rendering is needed.

    Returns ``(html, used_playwright)``.
    """
    try:
        resp = requests.get(url, headers=_DEFAULT_HEADERS, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        html = resp.text
    except requests.RequestException as exc:
        logger.warning("HTTP fetch failed for %s: %s", url, exc)
        html = ""

    if not html or _is_bot_challenge(html):
        logger.info("JS rendering required for %s – trying Playwright", url)
        pw_html = _fetch_with_playwright(url)
        if pw_html:
            return pw_html, True
        # If Playwright also failed, return whatever we have
        return html, False

    return html, False


def _clean_text(text: str) -> str:
    """Collapse whitespace and strip the result."""
    return re.sub(r"\s+", " ", text).strip()


def extract_page_content(url: str) -> dict:
    """Extract structured content from *url*.

    Returns a dict with keys:
        url, title, meta_description, headings, paragraphs,
        text_content, images, links, used_playwright, error
    """
    # Normalise URL
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    result: dict = {
        "url": url,
        "title": "",
        "meta_description": "",
        "headings": {"h1": [], "h2": [], "h3": [], "h4": [], "h5": [], "h6": []},
        "paragraphs": [],
        "text_content": "",
        "images": [],
        "links": [],
        "used_playwright": False,
        "error": None,
    }

    # Validate URL before making any network request (SSRF prevention)
    validation_error = _validate_url(url)
    if validation_error:
        result["error"] = validation_error
        return result

    try:
        html, used_playwright = _fetch_html(url)
        result["used_playwright"] = used_playwright
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
        return result

    if not html:
        result["error"] = "Failed to fetch page content."
        return result

    soup = BeautifulSoup(html, "lxml")

    # Remove script / style / noscript elements so they don't pollute text
    for tag in soup(["script", "style", "noscript", "head"]):
        tag.decompose()

    # Title
    title_tag = soup.find("title")
    result["title"] = _clean_text(title_tag.get_text()) if title_tag else ""

    # Meta description
    for attr in ("name", "property"):
        meta = soup.find("meta", attrs={attr: re.compile(r"description", re.I)})
        if meta and meta.get("content"):
            result["meta_description"] = _clean_text(meta["content"])
            break

    # Headings
    for level in range(1, 7):
        tag_name = f"h{level}"
        result["headings"][tag_name] = [
            _clean_text(h.get_text())
            for h in soup.find_all(tag_name)
            if _clean_text(h.get_text())
        ]

    # Paragraphs
    result["paragraphs"] = [
        _clean_text(p.get_text())
        for p in soup.find_all("p")
        if _clean_text(p.get_text())
    ]

    # Full visible text (body)
    body = soup.find("body") or soup
    text_content = " ".join(
        _clean_text(tag.get_text()) for tag in body.find_all(
            ["p", "li", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6"],
            recursive=True,
        )
        if _clean_text(tag.get_text())
    )
    result["text_content"] = text_content

    # Images
    parsed_base = urlparse(url)
    base_url = f"{parsed_base.scheme}://{parsed_base.netloc}"
    images = []
    for img in soup.find_all("img"):
        src = img.get("src", "").strip()
        if not src or src.startswith("data:"):
            continue
        full_src = urljoin(base_url, src)
        images.append({
            "src": full_src,
            "alt": _clean_text(img.get("alt", "")),
            "width": img.get("width", ""),
            "height": img.get("height", ""),
        })
    result["images"] = images

    # Links
    links = []
    seen_hrefs: set = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        full_href = urljoin(url, href)
        if full_href not in seen_hrefs:
            seen_hrefs.add(full_href)
            links.append({
                "url": full_href,
                "text": _clean_text(a.get_text()),
            })
    result["links"] = links

    return result
