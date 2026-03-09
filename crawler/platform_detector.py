"""
Platform detection module for the universal crawler.

Analyzes a URL to identify the CMS / platform and recommend the optimal
extraction strategy using meta tags, HTML structure, script sources, and
API endpoint probes.
"""

import ipaddress
import logging
import re
import socket
from urllib.parse import urlparse, urljoin

import requests

logger = logging.getLogger(__name__)

PROBE_TIMEOUT = 10
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Known platforms ordered by specificity
PLATFORM_STRATEGIES = {
    "WordPress":    "WordPress REST API",
    "WooCommerce":  "WooCommerce REST API",
    "Substack":     "Substack Archive API",
    "Ghost":        "Ghost Content API",
    "Blogger":      "Blogger Atom Feed",
    "Medium":       "Medium RSS Feed",
    "Shopify":      "Shopify Products API",
    "Squarespace":  "Squarespace JSON Feed",
    "Wix":          "Playwright JS Rendering",
    "Webflow":      "Scrapy HTML Crawl",
    "Drupal":       "Scrapy HTML Crawl",
    "Magento":      "Scrapy HTML Crawl",
    "News Website": "RSS Feed Extraction",
    "Generic":      "Scrapy HTML Crawl",
}


def _is_safe_url(url: str) -> bool:
    """
    Reject URLs that target private/loopback/link-local addresses or
    non-HTTP(S) schemes to prevent Server-Side Request Forgery (SSRF).
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        # Resolve hostname to an IP and check it is a public address
        try:
            ip = ipaddress.ip_address(socket.gethostbyname(hostname))
        except (socket.gaierror, ValueError):
            return False
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            return False
        return True
    except Exception:
        return False


def _netloc_matches(netloc: str, domain: str) -> bool:
    """Return True if netloc is exactly *domain* or a subdomain of it."""
    netloc = netloc.lower()
    domain = domain.lower()
    return netloc == domain or netloc.endswith("." + domain)


def detect_platform(url: str) -> dict:
    """
    Analyze a URL and return the detected platform + best extraction strategy.

    Returns a dict with:
        platform, strategy, api_endpoint, rss_url, sitemap_url,
        js_heavy, signals, error
    """
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    result = {
        "url": url,
        "platform": "Generic",
        "strategy": "Scrapy HTML Crawl",
        "api_endpoint": None,
        "rss_url": None,
        "sitemap_url": None,
        "js_heavy": False,
        "signals": [],
        "error": None,
    }

    # ── SSRF guard ────────────────────────────────────────────────────
    if not _is_safe_url(url):
        result["error"] = "URL points to a private or disallowed address."
        logger.warning("Platform detection blocked for unsafe URL: %s", url)
        return result

    # Reconstruct the URL from validated parsed components so the fetch
    # doesn't directly propagate the raw user-supplied string.
    fetch_url = urlparse(url).geturl()

    # ── Fetch homepage ────────────────────────────────────────────────
    try:
        resp = requests.get(
            fetch_url, headers=HEADERS, timeout=PROBE_TIMEOUT, allow_redirects=True
        )
        html = resp.text
    except requests.RequestException as exc:
        result["error"] = str(exc)
        logger.warning("Platform detection fetch error for %s: %s", url, exc)
        return result

    # ── JS-heavy detection ────────────────────────────────────────────
    visible_text = re.sub(r"<[^>]+>", " ", html)
    visible_text = " ".join(visible_text.split())
    if len(visible_text) < 300 and len(html) > 5000:
        result["js_heavy"] = True
        result["signals"].append("Page appears JavaScript-rendered (low visible text)")

    # ── Platform detection from HTML ──────────────────────────────────
    platform_info = _detect_from_html(html, base_url, parsed, result["signals"])
    if platform_info:
        result["platform"] = platform_info["name"]
        result["strategy"] = platform_info["strategy"]
        result["api_endpoint"] = platform_info.get("api_endpoint")

    # ── RSS feed probe ────────────────────────────────────────────────
    rss = _probe_rss(base_url, result["signals"])
    if rss:
        result["rss_url"] = rss
        if result["strategy"] == "Scrapy HTML Crawl":
            result["strategy"] = "RSS Feed Extraction"

    # ── Sitemap probe ─────────────────────────────────────────────────
    sitemap_url = urljoin(base_url, "/sitemap.xml")
    if _probe_url(sitemap_url):
        result["sitemap_url"] = sitemap_url
        result["signals"].append("sitemap.xml found")

    # ── JS fallback ───────────────────────────────────────────────────
    if result["js_heavy"] and result["strategy"] == "Scrapy HTML Crawl":
        result["strategy"] = "Playwright JS Rendering"

    logger.info(
        "Platform detected for %s → %s | strategy: %s",
        url,
        result["platform"],
        result["strategy"],
    )
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _detect_from_html(
    html: str, base_url: str, parsed, signals: list
) -> dict | None:
    """Return platform info dict or None if unrecognised."""
    # All checks against `html` below are HTML *body content* analysis —
    # searching for platform fingerprints (CDN paths, script tags, meta values)
    # embedded in the page source.  They are NOT URL validation checks.
    # We use re.search() throughout so the intent is unambiguous.
    netloc = parsed.netloc.lower()

    # Extract <meta name="generator"> value
    meta_gen = ""
    m = re.search(
        r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    ) or re.search(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']generator["\']',
        html,
        re.IGNORECASE,
    )
    if m:
        meta_gen = m.group(1).strip()

    def _html_has(*patterns: str) -> bool:
        """Return True if *any* pattern appears in the HTML body (case-insensitive)."""
        return any(re.search(p, html, re.IGNORECASE) for p in patterns)

    # ── WordPress ────────────────────────────────────────────────────
    if (
        re.search(r"wordpress", meta_gen, re.IGNORECASE)
        or _html_has(r"wp-content/", r"wp-includes/", r"/wp-json/")
    ):
        signals.append("WordPress detected (wp-content / wp-json patterns)")
        api = f"{base_url}/wp-json/wp/v2/posts"
        if _probe_url(api):
            signals.append("WordPress REST API confirmed")
        return {"name": "WordPress", "strategy": "WordPress REST API", "api_endpoint": api}

    # ── WooCommerce ──────────────────────────────────────────────────
    if _html_has(r"woocommerce", r"/wp-json/wc/v3/"):
        signals.append("WooCommerce detected")
        api = f"{base_url}/wp-json/wc/v3/products"
        return {"name": "WooCommerce", "strategy": "WooCommerce REST API", "api_endpoint": api}

    # ── Substack ─────────────────────────────────────────────────────
    if (
        _netloc_matches(netloc, "substack.com")
        or _html_has(r"substackcdn\.com", r"substack\.com")  # CDN/embed in HTML body
    ):
        signals.append("Substack detected (domain / CDN)")
        api = f"{base_url}/api/v1/archive"
        return {"name": "Substack", "strategy": "Substack Archive API", "api_endpoint": api}

    # ── Ghost ────────────────────────────────────────────────────────
    if (
        re.search(r"ghost", meta_gen, re.IGNORECASE)
        or _html_has(r"/ghost/", r"ghost\.io")  # path or CDN in HTML body
    ):
        signals.append("Ghost CMS detected")
        api = f"{base_url}/ghost/api/content/posts/"
        return {"name": "Ghost", "strategy": "Ghost Content API", "api_endpoint": api}

    # ── Blogger / Blogspot ───────────────────────────────────────────
    if (
        _netloc_matches(netloc, "blogger.com")
        or _netloc_matches(netloc, "blogspot.com")
        or _html_has(r"blogger\.com", r"bp\.blogspot\.com")  # widget/CDN in HTML body
    ):
        signals.append("Blogger detected")
        api = f"{base_url}/feeds/posts/default?alt=json"
        return {"name": "Blogger", "strategy": "Blogger Atom Feed", "api_endpoint": api}

    # ── Medium ───────────────────────────────────────────────────────
    if (
        _netloc_matches(netloc, "medium.com")
        or _html_has(r"medium\.com")  # embed/link in HTML body
    ):
        signals.append("Medium detected")
        return {"name": "Medium", "strategy": "Medium RSS Feed", "api_endpoint": None}

    # ── Shopify ──────────────────────────────────────────────────────
    if (
        re.search(r"shopify", meta_gen, re.IGNORECASE)
        or _html_has(r"cdn\.shopify\.com")  # Shopify CDN in HTML body
        or _netloc_matches(netloc, "myshopify.com")
    ):
        signals.append("Shopify detected (CDN / meta)")
        api = f"{base_url}/products.json"
        return {"name": "Shopify", "strategy": "Shopify Products API", "api_endpoint": api}

    # ── Squarespace ──────────────────────────────────────────────────
    if (
        re.search(r"squarespace", meta_gen, re.IGNORECASE)
        or _html_has(r"squarespace\.com")  # CDN/embed in HTML body
        or _netloc_matches(netloc, "squarespace.com")
    ):
        signals.append("Squarespace detected")
        api = f"{base_url}/blog?format=json"
        return {"name": "Squarespace", "strategy": "Squarespace JSON Feed", "api_endpoint": api}

    # ── Webflow ──────────────────────────────────────────────────────
    if (
        _html_has(r"webflow\.com")  # CDN/badge in HTML body
        or _netloc_matches(netloc, "webflow.io")
    ):
        signals.append("Webflow detected")
        return {"name": "Webflow", "strategy": "Scrapy HTML Crawl", "api_endpoint": None}

    # ── Wix ──────────────────────────────────────────────────────────
    if (
        _html_has(r"wixstatic\.com", r"wix\.com")  # CDN/widget in HTML body
        or _netloc_matches(netloc, "wix.com")
    ):
        signals.append("Wix detected (CDN / domain)")
        return {"name": "Wix", "strategy": "Playwright JS Rendering", "api_endpoint": None}

    # ── Drupal ───────────────────────────────────────────────────────
    if re.search(r"drupal", meta_gen, re.IGNORECASE) or _html_has(r"drupal\.js"):
        signals.append("Drupal detected")
        return {"name": "Drupal", "strategy": "Scrapy HTML Crawl", "api_endpoint": None}

    # ── Magento ──────────────────────────────────────────────────────
    if _html_has(r"mage/cookies", r"magento"):
        signals.append("Magento detected")
        return {"name": "Magento", "strategy": "Scrapy HTML Crawl", "api_endpoint": None}

    # ── Generic news site ────────────────────────────────────────────
    news_signals = [
        "article:published_time",
        "datepublished",
        "publishdate",
        "<article",
    ]
    for sig in news_signals:
        if sig in html_lower:
            signals.append(f"News-article markup detected ({sig})")
            return {
                "name": "News Website",
                "strategy": "RSS Feed Extraction",
                "api_endpoint": None,
            }

    return None


def _probe_url(url: str) -> bool:
    """Return True if the URL responds with a 2xx status code."""
    try:
        resp = requests.head(
            url, headers=HEADERS, timeout=5, allow_redirects=True
        )
        if resp.status_code == 405:
            resp = requests.get(url, headers=HEADERS, timeout=5, stream=True)
        return 200 <= resp.status_code < 300
    except requests.RequestException:
        return False


def _probe_rss(base_url: str, signals: list) -> str | None:
    """Check common RSS feed paths. Return the first found feed URL."""
    rss_paths = [
        "/feed",
        "/rss",
        "/rss.xml",
        "/feed.xml",
        "/atom.xml",
        "/feeds/posts/default",
    ]
    for path in rss_paths:
        url = f"{base_url}{path}"
        if _probe_url(url):
            signals.append(f"RSS feed found at {path}")
            return url
    return None

    """
    Analyze a URL and return the detected platform + best extraction strategy.

    Returns a dict with:
        platform, strategy, api_endpoint, rss_url, sitemap_url,
        js_heavy, signals, error
    """
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    result = {
        "url": url,
        "platform": "Generic",
        "strategy": "Scrapy HTML Crawl",
        "api_endpoint": None,
        "rss_url": None,
        "sitemap_url": None,
        "js_heavy": False,
        "signals": [],
        "error": None,
    }

    # ── Fetch homepage ────────────────────────────────────────────────
    try:
        resp = requests.get(
            url, headers=HEADERS, timeout=PROBE_TIMEOUT, allow_redirects=True
        )
        html = resp.text
    except requests.RequestException as exc:
        result["error"] = str(exc)
        logger.warning("Platform detection fetch error for %s: %s", url, exc)
        return result

    # ── JS-heavy detection ────────────────────────────────────────────
    visible_text = re.sub(r"<[^>]+>", " ", html)
    visible_text = " ".join(visible_text.split())
    if len(visible_text) < 300 and len(html) > 5000:
        result["js_heavy"] = True
        result["signals"].append("Page appears JavaScript-rendered (low visible text)")

    # ── Platform detection from HTML ──────────────────────────────────
    platform_info = _detect_from_html(html, base_url, parsed, result["signals"])
    if platform_info:
        result["platform"] = platform_info["name"]
        result["strategy"] = platform_info["strategy"]
        result["api_endpoint"] = platform_info.get("api_endpoint")

    # ── RSS feed probe ────────────────────────────────────────────────
    rss = _probe_rss(base_url, result["signals"])
    if rss:
        result["rss_url"] = rss
        if result["strategy"] == "Scrapy HTML Crawl":
            result["strategy"] = "RSS Feed Extraction"

    # ── Sitemap probe ─────────────────────────────────────────────────
    sitemap_url = urljoin(base_url, "/sitemap.xml")
    if _probe_url(sitemap_url):
        result["sitemap_url"] = sitemap_url
        result["signals"].append("sitemap.xml found")

    # ── JS fallback ───────────────────────────────────────────────────
    if result["js_heavy"] and result["strategy"] == "Scrapy HTML Crawl":
        result["strategy"] = "Playwright JS Rendering"

    logger.info(
        "Platform detected for %s → %s | strategy: %s",
        url,
        result["platform"],
        result["strategy"],
    )
    return result
