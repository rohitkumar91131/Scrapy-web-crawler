import json
import logging
import os
import scrapy
from scrapy_playwright.page import PageMethod
from urllib.parse import urlparse
from datetime import datetime, timezone

from crawler.login_detection import BOT_CHALLENGE_PHRASES, is_login_wall

logger = logging.getLogger(__name__)


def _is_bot_challenge(response) -> bool:
    """Return True if the response looks like a bot/Cloudflare challenge page."""
    text_lower = response.text.lower()
    for phrase in BOT_CHALLENGE_PHRASES:
        if phrase in text_lower:
            return True
    # Very short visible text in a large HTML response is a strong JS indicator
    visible_text = " ".join(response.css("body ::text").getall()).strip()
    if len(visible_text) < 100 and len(response.text) > 2000:
        return True
    return False


class WebCrawlerSpider(scrapy.Spider):
    """Scrapy spider that crawls a website and extracts page data.

    Automatically switches to Playwright-powered rendering when the start URL
    returns a page that requires JavaScript (e.g. "Enable JS to continue" walls).
    All subsequent internal links are then fetched with the same mode so the
    full site is rendered consistently.
    """

    name = "web_crawler"

    custom_settings = {
        # Keep crawl depth and page count small to stay within 512 MB RAM on Render
        "DEPTH_LIMIT": 2,
        "CLOSESPIDER_PAGECOUNT": 20,
        "ROBOTSTXT_OBEY": True,
        # Slightly longer delay reduces the number of requests in-flight at once
        "DOWNLOAD_DELAY": 2,
        # Limit parallel requests so the browser doesn't spawn too many tabs at once
        "CONCURRENT_REQUESTS": 2,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "LOG_LEVEL": "WARNING",
        "HTTPERROR_ALLOW_ALL": True,
        # Scrapy memory watchdog – gracefully closes the spider before OOM kill.
        # On Render's 512 MB free tier the OS + uvicorn use ~100-150 MB, so
        # limiting the spider subprocess to 280 MB leaves enough headroom.
        "MEMUSAGE_ENABLED": True,
        "MEMUSAGE_LIMIT_MB": 280,
        "MEMUSAGE_WARNING_MB": 200,
        # Realistic browser headers to reduce bot-detection fingerprinting
        "DEFAULT_REQUEST_HEADERS": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
        # Playwright download handlers (activated per-request via meta["playwright"])
        "DOWNLOAD_HANDLERS": {
            "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
            "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
        },
        "TWISTED_REACTOR": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
        "PLAYWRIGHT_BROWSER_TYPE": "chromium",
        # Cap open browser tabs to 1 per context – the single largest lever for
        # reducing Chromium memory usage on the 512 MB Render free tier.
        "PLAYWRIGHT_MAX_PAGES_PER_CONTEXT": 1,
        "PLAYWRIGHT_LAUNCH_OPTIONS": {
            "headless": True,
            # Flags required to run Chromium in a low-memory container environment
            # (Render free tier: 512 MB RAM, tiny /dev/shm)
            "args": [
                "--disable-dev-shm-usage",       # use /tmp instead of /dev/shm
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-sync",
                "--no-first-run",
                "--mute-audio",
                # Limit renderer processes to reduce per-process overhead
                "--renderer-process-limit=2",
                # Cap V8 JS heap to 128 MB per renderer process
                "--js-flags=--max-old-space-size=128",
                # Disable features that consume background memory
                "--disable-renderer-accessibility",
                "--disable-features=TranslateUI,BlinkGenPropertyTrees",
            ],
        },
        # Smaller viewport reduces per-tab GPU/memory footprint
        "PLAYWRIGHT_CONTEXTS": {
            "default": {
                "viewport": {"width": 800, "height": 600},
                "locale": "en-US",
            },
        },
    }

    # Playwright page methods applied when JS rendering is needed
    _PLAYWRIGHT_METHODS = [
        PageMethod("wait_for_load_state", "networkidle"),
    ]

    def __init__(self, start_url=None, session_file=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not start_url:
            raise ValueError("start_url argument is required.")
        # Ensure the URL has a scheme
        if not start_url.startswith(("http://", "https://")):
            start_url = "https://" + start_url
        self.start_urls = [start_url]
        parsed = urlparse(start_url)
        self.allowed_domain = parsed.netloc.lower()
        # Set to True once JS rendering is detected on the first page
        self._use_playwright = False
        # Load saved session cookies (created by Account Manager / auth.py)
        self._session_cookies: list = []
        if session_file and os.path.exists(session_file):
            try:
                with open(session_file, "r", encoding="utf-8") as fh:
                    session_data = json.load(fh)
                self._session_cookies = session_data.get("cookies", [])
                if self._session_cookies:
                    logger.info(
                        "Loaded %d cookies from session file: %s",
                        len(self._session_cookies),
                        session_file,
                    )
                    # When a session is provided, always use Playwright so the
                    # authenticated context is active from the very first request.
                    self._use_playwright = True
            except Exception as exc:
                logger.warning("Failed to load session file %s: %s", session_file, exc)

    def start_requests(self):
        """Yield the initial request, using an authenticated Playwright context
        when a session file was provided, or plain HTTP otherwise."""
        for url in self.start_urls:
            if self._session_cookies:
                yield scrapy.Request(
                    url,
                    callback=self.parse,
                    errback=self.errback,
                    meta={
                        "playwright": True,
                        "playwright_context": "authenticated",
                        "playwright_context_kwargs": {
                            "storage_state": {
                                "cookies": self._session_cookies,
                                "origins": [],
                            },
                            "viewport": {"width": 1024, "height": 600},
                            "locale": "en-US",
                        },
                        "playwright_page_methods": self._PLAYWRIGHT_METHODS,
                    },
                )
            else:
                yield scrapy.Request(url, callback=self.parse, errback=self.errback)

    def errback(self, failure):
        """Log request errors without crashing the spider."""
        logger.warning("Request failed: %s", failure)

    def _make_request(self, url):
        """Build a follow-up request, using Playwright only when required."""
        if self._use_playwright:
            meta: dict = {
                "playwright": True,
                "playwright_page_methods": self._PLAYWRIGHT_METHODS,
            }
            if self._session_cookies:
                meta["playwright_context"] = "authenticated"
            return scrapy.Request(
                url,
                callback=self.parse,
                errback=self.errback,
                meta=meta,
            )
        return scrapy.Request(url, callback=self.parse, errback=self.errback)

    def parse(self, response):
        """Parse each page and yield extracted data."""
        # Skip non-HTML responses
        content_type = response.headers.get("Content-Type", b"").decode("utf-8", errors="ignore")
        if "text/html" not in content_type:
            return

        url = response.url
        parsed_url = urlparse(url)
        if parsed_url.netloc.lower() != self.allowed_domain:
            return

        # On the first plain request, detect bot/Cloudflare challenge pages and
        # re-fetch with Playwright so the real content can be rendered.
        if not self._use_playwright and _is_bot_challenge(response):
            logger.warning(
                "Bot challenge / login wall detected – retrying with Playwright: %s", url
            )
            self._use_playwright = True
            yield scrapy.Request(
                url,
                callback=self.parse,
                errback=self.errback,
                dont_filter=True,
                meta={
                    "playwright": True,
                    "playwright_page_methods": self._PLAYWRIGHT_METHODS,
                },
            )
            return

        # Even after a Playwright retry (`self._use_playwright` is True), refuse to
        # store a challenge page.  The first guard above only fires for plain HTTP
        # requests, so this second check covers the Playwright response path.
        if _is_bot_challenge(response):
            if is_login_wall(response.text):
                logger.warning("Login wall detected after Playwright – emitting auth-error item: %s", url)
                title = (response.css("title::text").get("") or "").strip()
                yield {
                    "page_url": url,
                    "title": title,
                    "meta_description": "",
                    "headings": {"h1": [], "h2": [], "h3": [], "h4": [], "h5": [], "h6": []},
                    "paragraphs": [],
                    "images": [],
                    "text_content": "",
                    "internal_links": [],
                    "crawl_timestamp": datetime.now(timezone.utc).isoformat(),
                    "error": (
                        "Login required: this page is protected by authentication. "
                        "Use the Account Manager to create an authenticated session, "
                        "then re-run the crawl with that session."
                    ),
                }
            else:
                logger.warning("Bot challenge page still present after Playwright – skipping: %s", url)
            return

        title = (response.css("title::text").get("") or "").strip()
        meta_description = (
            response.css('meta[name="description"]::attr(content)').get("")
            or response.css('meta[name="Description"]::attr(content)').get("")
            or ""
        ).strip()

        headings = {
            "h1": [h.strip() for h in response.css("h1::text").getall() if h.strip()],
            "h2": [h.strip() for h in response.css("h2::text").getall() if h.strip()],
            "h3": [h.strip() for h in response.css("h3::text").getall() if h.strip()],
            "h4": [h.strip() for h in response.css("h4::text").getall() if h.strip()],
            "h5": [h.strip() for h in response.css("h5::text").getall() if h.strip()],
            "h6": [h.strip() for h in response.css("h6::text").getall() if h.strip()],
        }

        # Extract visible text content, normalized
        raw_texts = response.css(
            "body p::text, body li::text, body span::text, body div::text, "
            "body h1::text, body h2::text, body h3::text, body h4::text, "
            "body h5::text, body h6::text, body td::text, body th::text"
        ).getall()
        text_content = " ".join(t.strip() for t in raw_texts if t.strip())
        # Limit to 5000 characters to keep results manageable
        text_content = text_content[:5000]

        # Extract paragraphs
        paragraphs = [
            p.strip()
            for p in response.css("p::text").getall()
            if p.strip()
        ]

        # Extract images (src and alt text)
        images = []
        for img in response.css("img"):
            src = img.attrib.get("src", "").strip()
            if src and not src.startswith("data:"):
                full_src = response.urljoin(src)
                images.append({
                    "src": full_src,
                    "alt": img.attrib.get("alt", "").strip(),
                })

        # Collect internal links
        internal_links = set()
        links_to_follow = []
        for href in response.css("a::attr(href)").getall():
            full_url = response.urljoin(href)
            parsed_link = urlparse(full_url)
            # Keep only http/https links on the same domain
            if (
                parsed_link.scheme in ("http", "https")
                and parsed_link.netloc.lower() == self.allowed_domain
            ):
                # Normalise: drop fragments
                clean_url = parsed_link._replace(fragment="").geturl()
                internal_links.add(clean_url)
                links_to_follow.append(clean_url)

        # Follow unique internal links
        seen = set()
        for link in links_to_follow:
            if link not in seen:
                seen.add(link)
                yield self._make_request(link)

        yield {
            "page_url": url,
            "title": title,
            "meta_description": meta_description,
            "headings": headings,
            "paragraphs": paragraphs[:100],
            "images": images[:50],
            "text_content": text_content,
            "internal_links": sorted(internal_links),
            "crawl_timestamp": datetime.now(timezone.utc).isoformat(),
        }
