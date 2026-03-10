"""Shared constants and helpers for detecting login / authentication walls.

Used by both the Scrapy spider (crawler/spider.py) and the FastAPI app
(app.py) so that the list of detection phrases is defined exactly once.
"""

# Phrases that specifically indicate an authentication / login wall.
# When these are found in rendered page text, the page requires the user to
# sign in before the real content is accessible.
LOGIN_WALL_PHRASES: tuple[str, ...] = (
    "sign in to continue",
    "please sign in to continue",
    "you must be signed in",
    "you must be logged in",
    "log in to continue",
    "login to continue",
    "log in to access",
    "sign in to access",
    "log in to view",
    "sign in to view",
    "log in to get",        # e.g. "Log in to get answers…" (ChatGPT shared links)
    "sign in to get",
    "login required",
    "please log in",
    "you need to log in",
    "you need to sign in",
    "members only",
    "create an account to",
    "register to access",
    "restricted content",
    "access restricted",
    "subscribe to read",
    "subscribe to access",
    "subscribe to continue",
    # ChatGPT / OpenAI-specific error pages for shared conversations
    "can't load shared conversation",
    "unable to load conversation",
)

# Bot-challenge / JS-wall phrases that are NOT login walls (Cloudflare,
# generic JS-gating, etc.).  Combined with LOGIN_WALL_PHRASES they form
# the full set used to decide whether to switch to Playwright rendering.
_JS_CHALLENGE_PHRASES: tuple[str, ...] = (
    "performing security verification",
    "enable javascript and cookies to continue",
    "just a moment",
    "enable javascript",
    "javascript required",
    "please enable javascript",
    "javascript is required",
    "you need to enable javascript",
)

# Full set of phrases used to detect any page that needs Playwright (or
# cannot be scraped without authentication).
BOT_CHALLENGE_PHRASES: tuple[str, ...] = _JS_CHALLENGE_PHRASES + LOGIN_WALL_PHRASES


def is_login_wall(text: str) -> bool:
    """Return True if *text* (lowercased page body) contains a login-wall phrase."""
    text_lower = text.lower()
    return any(phrase in text_lower for phrase in LOGIN_WALL_PHRASES)
