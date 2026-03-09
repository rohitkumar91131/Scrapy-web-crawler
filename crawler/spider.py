import scrapy
from urllib.parse import urlparse
from datetime import datetime, timezone


class WebCrawlerSpider(scrapy.Spider):
    """Scrapy spider that crawls a website and extracts page data."""

    name = "web_crawler"

    custom_settings = {
        "DEPTH_LIMIT": 3,
        "CLOSESPIDER_PAGECOUNT": 100,
        "ROBOTSTXT_OBEY": True,
        "DOWNLOAD_DELAY": 0.5,
        "LOG_LEVEL": "WARNING",
        "HTTPERROR_ALLOW_ALL": True,
    }

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
                yield response.follow(link, callback=self.parse)

        yield {
            "page_url": url,
            "title": title,
            "meta_description": meta_description,
            "headings": headings,
            "text_content": text_content,
            "internal_links": sorted(internal_links),
            "crawl_timestamp": datetime.now(timezone.utc).isoformat(),
        }
