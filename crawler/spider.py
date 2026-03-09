import scrapy
from scrapy_playwright.page import PageMethod
from urllib.parse import urlparse
from datetime import datetime, timezone


# Phrases that indicate a page requires JavaScript to render
_JS_WALL_PHRASES = (
    "enable javascript",
    "javascript required",
    "please enable javascript",
    "javascript is required",
    "you need to enable javascript",
)


def _page_needs_js(response) -> bool:
    """Return True if the response looks like it needs JavaScript to render."""
    text_lower = response.text.lower()
    for phrase in _JS_WALL_PHRASES:
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
        "DEPTH_LIMIT": 3,
        "CLOSESPIDER_PAGECOUNT": 100,
        "ROBOTSTXT_OBEY": True,
        "DOWNLOAD_DELAY": 0.5,
        "LOG_LEVEL": "WARNING",
        "HTTPERROR_ALLOW_ALL": True,
        # Playwright download handlers (activated per-request via meta["playwright"])
        "DOWNLOAD_HANDLERS": {
            "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
            "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
        },
        "TWISTED_REACTOR": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
        "PLAYWRIGHT_BROWSER_TYPE": "chromium",
        "PLAYWRIGHT_LAUNCH_OPTIONS": {"headless": True},
    }

    # Playwright page methods applied when JS rendering is needed
    _PLAYWRIGHT_METHODS = [
        PageMethod("wait_for_load_state", "networkidle"),
    ]

    def __init__(self, start_url=None, *args, **kwargs):
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

    def start_requests(self):
        """Yield the initial request using plain HTTP (no Playwright overhead)."""
        for url in self.start_urls:
            yield scrapy.Request(url, callback=self.parse, errback=self.errback)

    def errback(self, failure):
        """Log request errors without crashing the spider."""
        import logging
        logging.getLogger(__name__).warning("Request failed: %s", failure)

    def _make_request(self, url):
        """Build a follow-up request, using Playwright only when required."""
        if self._use_playwright:
            return scrapy.Request(
                url,
                callback=self.parse,
                errback=self.errback,
                meta={
                    "playwright": True,
                    "playwright_page_methods": self._PLAYWRIGHT_METHODS,
                },
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

        # On the first plain request, detect JS-heavy pages and re-fetch with Playwright
        if not self._use_playwright and _page_needs_js(response):
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
            "text_content": text_content,
            "internal_links": sorted(internal_links),
            "crawl_timestamp": datetime.now(timezone.utc).isoformat(),
        }
