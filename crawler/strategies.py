"""
Extraction strategies for different website platforms.

Fallback order for each crawl:
  1. Platform-specific API  (WordPress REST, Substack, Ghost, Shopify, …)
  2. RSS / Atom feed
  3. sitemap.xml URL list
  4. Scrapy HTML crawl  (invoked externally via the spider)
  5. Playwright JS render  (triggered automatically by the spider on JS walls)
"""

import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/html,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 20


# Maximum characters of text content kept per page to avoid unbounded memory use
_MAX_TEXT_CHARS = 5000


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strip_html(html: str) -> str:
    if not html:
        return ""
    clean = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", clean).strip()


def _make_page(
    *,
    url: str = "",
    title: str = "",
    author: str = "",
    publish_date: str = "",
    headings: dict | None = None,
    text: str = "",
    images: list | None = None,
    links: list | None = None,
    platform: str = "",
    strategy: str = "",
    meta_description: str = "",
) -> dict:
    return {
        "page_url": url,
        "title": title,
        "author": author,
        "publish_date": publish_date,
        "headings": headings or {"h1": [], "h2": [], "h3": []},
        "text_content": text[:_MAX_TEXT_CHARS],
        "images": images or [],
        "internal_links": links or [],
        "crawl_timestamp": _now(),
        "platform": platform,
        "extraction_strategy": strategy,
        "meta_description": meta_description,
    }


def _base_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


# ---------------------------------------------------------------------------
# Platform-specific extractors
# ---------------------------------------------------------------------------

def extract_wordpress_api(base_url: str, max_pages: int = 100) -> list:
    """Extract posts from the WordPress REST API."""
    logger.info("WordPress REST API – extracting from %s", base_url)
    results: list = []
    page = 1
    per_page = 20

    while len(results) < max_pages:
        url = (
            f"{base_url}/wp-json/wp/v2/posts"
            f"?per_page={per_page}&page={page}&_embed=1"
        )
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if resp.status_code in (400, 404):
                break
            if not resp.ok:
                break
            posts = resp.json()
            if not isinstance(posts, list) or not posts:
                break
            for post in posts:
                author = ""
                try:
                    embedded_authors = (
                        post.get("_embedded", {}).get("author", [])
                    )
                    if embedded_authors:
                        author = embedded_authors[0].get("name", "")
                except Exception:
                    pass
                results.append(
                    _make_page(
                        url=post.get("link", ""),
                        title=_strip_html(
                            post.get("title", {}).get("rendered", "")
                        ),
                        author=author,
                        publish_date=post.get("date", ""),
                        text=_strip_html(
                            post.get("content", {}).get("rendered", "")
                        ),
                        platform="WordPress",
                        strategy="WordPress REST API",
                    )
                )
            if len(posts) < per_page:
                break
            page += 1
        except Exception as exc:
            logger.warning("WordPress API error: %s", exc)
            break

    logger.info("WordPress REST API – extracted %d posts", len(results))
    return results


def extract_substack_api(base_url: str, max_pages: int = 100) -> list:
    """Extract posts from the Substack archive API."""
    logger.info("Substack API – extracting from %s", base_url)
    results: list = []
    offset = 0
    limit = 12

    while len(results) < max_pages:
        url = (
            f"{base_url}/api/v1/archive"
            f"?sort=new&offset={offset}&limit={limit}"
        )
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if not resp.ok:
                break
            posts = resp.json()
            if not isinstance(posts, list) or not posts:
                break
            for post in posts:
                slug = post.get("slug", "")
                post_url = (
                    f"{base_url}/p/{slug}" if slug
                    else post.get("canonical_url", "")
                )
                author_obj = post.get("author") or {}
                author = (
                    author_obj.get("name", "")
                    if isinstance(author_obj, dict)
                    else ""
                )
                results.append(
                    _make_page(
                        url=post_url,
                        title=post.get("title", ""),
                        author=author,
                        publish_date=post.get("post_date", ""),
                        text=_strip_html(
                            post.get("body_html", "")
                            or post.get("truncated_body_text", "")
                        ),
                        platform="Substack",
                        strategy="Substack Archive API",
                    )
                )
            if len(posts) < limit:
                break
            offset += limit
        except Exception as exc:
            logger.warning("Substack API error: %s", exc)
            break

    logger.info("Substack API – extracted %d posts", len(results))
    return results


def extract_ghost_api(
    base_url: str, api_key: str = "", max_pages: int = 100
) -> list:
    """Extract posts from the Ghost Content API."""
    logger.info("Ghost API – extracting from %s", base_url)
    results: list = []
    page = 1
    limit = 15

    while len(results) < max_pages:
        params = f"?limit={limit}&page={page}&include=authors,tags&formats=plaintext"
        if api_key:
            params += f"&key={api_key}"
        url = f"{base_url}/ghost/api/content/posts/{params}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if not resp.ok:
                break
            data = resp.json()
            posts = data.get("posts", [])
            if not posts:
                break
            for post in posts:
                author = ""
                authors = post.get("authors") or post.get("primary_author") or {}
                if isinstance(authors, list) and authors:
                    author = authors[0].get("name", "")
                elif isinstance(authors, dict):
                    author = authors.get("name", "")
                results.append(
                    _make_page(
                        url=post.get("url", ""),
                        title=post.get("title", ""),
                        author=author,
                        publish_date=post.get("published_at", ""),
                        text=post.get("plaintext", "")
                        or _strip_html(post.get("html", "")),
                        platform="Ghost",
                        strategy="Ghost Content API",
                    )
                )
            pagination = data.get("meta", {}).get("pagination", {})
            if page >= pagination.get("pages", 1):
                break
            page += 1
        except Exception as exc:
            logger.warning("Ghost API error: %s", exc)
            break

    logger.info("Ghost API – extracted %d posts", len(results))
    return results


def extract_shopify_api(base_url: str, max_pages: int = 100) -> list:
    """Extract products from the Shopify JSON API."""
    logger.info("Shopify API – extracting from %s", base_url)
    results: list = []
    page = 1
    limit = 50

    while len(results) < max_pages:
        url = f"{base_url}/products.json?limit={limit}&page={page}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if not resp.ok:
                break
            products = resp.json().get("products", [])
            if not products:
                break
            for prod in products:
                results.append(
                    _make_page(
                        url=f"{base_url}/products/{prod.get('handle', '')}",
                        title=prod.get("title", ""),
                        text=_strip_html(prod.get("body_html", "")),
                        publish_date=prod.get("created_at", ""),
                        platform="Shopify",
                        strategy="Shopify Products API",
                    )
                )
            if len(products) < limit:
                break
            page += 1
        except Exception as exc:
            logger.warning("Shopify API error: %s", exc)
            break

    logger.info("Shopify API – extracted %d products", len(results))
    return results


def extract_squarespace_feed(base_url: str, max_pages: int = 100) -> list:
    """Extract posts from the Squarespace JSON feed."""
    logger.info("Squarespace JSON feed – extracting from %s", base_url)
    results: list = []
    try:
        resp = requests.get(
            f"{base_url}/blog?format=json", headers=HEADERS, timeout=TIMEOUT
        )
        if resp.ok:
            items = resp.json().get("items", [])
            for item in items[:max_pages]:
                results.append(
                    _make_page(
                        url=f"{base_url}{item.get('fullUrl', '')}",
                        title=item.get("title", ""),
                        text=_strip_html(item.get("body", "")),
                        publish_date=str(item.get("publishOn", "")),
                        platform="Squarespace",
                        strategy="Squarespace JSON Feed",
                    )
                )
    except Exception as exc:
        logger.warning("Squarespace feed error: %s", exc)

    logger.info("Squarespace – extracted %d posts", len(results))
    return results


def extract_rss_feed(
    rss_url: str, platform: str = "Generic", max_pages: int = 100
) -> list:
    """Extract articles from an RSS / Atom feed."""
    logger.info("RSS extraction from %s (platform=%s)", rss_url, platform)
    results: list = []
    try:
        import feedparser  # type: ignore

        feed = feedparser.parse(rss_url)
        for entry in feed.entries[:max_pages]:
            author = ""
            if hasattr(entry, "author_detail") and isinstance(
                entry.author_detail, dict
            ):
                author = entry.author_detail.get("name", "")
            elif hasattr(entry, "author"):
                author = str(entry.author)

            content = ""
            if hasattr(entry, "content") and entry.content:
                content = entry.content[0].get("value", "")
            elif hasattr(entry, "summary"):
                content = entry.summary

            results.append(
                _make_page(
                    url=entry.get("link", ""),
                    title=entry.get("title", ""),
                    author=author,
                    publish_date=entry.get("published", ""),
                    text=_strip_html(content),
                    platform=platform,
                    strategy="RSS Feed Extraction",
                )
            )
    except Exception as exc:
        logger.warning("RSS extraction error for %s: %s", rss_url, exc)

    logger.info("RSS – extracted %d articles", len(results))
    return results


def extract_sitemap(
    sitemap_url: str, platform: str = "Generic", max_pages: int = 100
) -> list:
    """Build a page list from sitemap.xml URLs (metadata fetched lazily)."""
    logger.info("Sitemap extraction from %s", sitemap_url)
    urls: list = []
    try:
        import xml.etree.ElementTree as ET

        resp = requests.get(sitemap_url, headers=HEADERS, timeout=TIMEOUT)
        if resp.ok:
            root = ET.fromstring(resp.content)
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            # Sitemap index → recurse into sub-sitemaps
            for child_loc in root.findall(".//sm:sitemap/sm:loc", ns):
                sub = requests.get(
                    child_loc.text.strip(), headers=HEADERS, timeout=TIMEOUT
                )
                if sub.ok:
                    sub_root = ET.fromstring(sub.content)
                    for loc in sub_root.findall(".//sm:url/sm:loc", ns):
                        urls.append(loc.text.strip())
                if len(urls) >= max_pages:
                    break
            # Regular sitemap
            for loc in root.findall(".//sm:url/sm:loc", ns):
                urls.append(loc.text.strip())
    except Exception as exc:
        logger.warning("Sitemap parse error: %s", exc)

    urls = list(dict.fromkeys(urls))[:max_pages]
    results = [
        _make_page(
            url=u,
            title=u.rstrip("/").split("/")[-1] or u,
            platform=platform,
            strategy="Sitemap XML Extraction",
        )
        for u in urls
    ]
    logger.info("Sitemap – found %d URLs", len(results))
    return results


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_strategy(detection: dict, max_pages: int = 100) -> tuple[list, str]:
    """
    Run the best extraction strategy for the detected platform.

    Returns (results, strategy_name_used).
    Falls back through: platform API → RSS → Sitemap → Scrapy (caller handles).
    """
    platform = detection.get("platform", "Generic")
    base = _base_url(detection.get("url", ""))
    api_endpoint = detection.get("api_endpoint")
    rss_url = detection.get("rss_url")
    sitemap_url = detection.get("sitemap_url")

    results: list = []

    # 1 ── Platform-specific API ────────────────────────────────────
    if platform == "WordPress" and api_endpoint:
        results = extract_wordpress_api(base, max_pages)
        if results:
            return results, "WordPress REST API"

    elif platform == "WooCommerce":
        results = extract_shopify_api(base, max_pages)  # products pattern
        if results:
            return results, "WooCommerce REST API"

    elif platform == "Substack" and api_endpoint:
        results = extract_substack_api(base, max_pages)
        if results:
            return results, "Substack Archive API"

    elif platform == "Ghost" and api_endpoint:
        results = extract_ghost_api(base, max_pages=max_pages)
        if results:
            return results, "Ghost Content API"

    elif platform == "Shopify" and api_endpoint:
        results = extract_shopify_api(base, max_pages)
        if results:
            return results, "Shopify Products API"

    elif platform == "Squarespace":
        results = extract_squarespace_feed(base, max_pages)
        if results:
            return results, "Squarespace JSON Feed"

    elif platform == "Medium":
        rss_url = rss_url or f"{base}/feed"

    # 2 ── RSS / Atom feed ──────────────────────────────────────────
    if not results and rss_url:
        results = extract_rss_feed(rss_url, platform, max_pages)
        if results:
            return results, "RSS Feed Extraction"

    # 3 ── sitemap.xml ──────────────────────────────────────────────
    if not results and sitemap_url:
        results = extract_sitemap(sitemap_url, platform, max_pages)
        if results:
            return results, "Sitemap XML Extraction"

    # 4 ── Caller must invoke Scrapy spider ─────────────────────────
    return results, "Scrapy HTML Crawl"
