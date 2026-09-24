"""Tier-2 fetcher: render a JavaScript page in headless Chromium.

Only used when `scrapers.fetch_webpage_text` comes back empty-handed, because a
rendered DOM is downstream of the data and picks up variance a plain fetch does
not. Everything configurable here is pinned so two runs of the same page produce
the same text: fixed viewport, locale, timezone and user agent, motion disabled,
images/fonts/media blocked, and an explicit wait for the text to stop growing
rather than a `networkidle` race.
"""

import logging

from src.scrapers import FetchError

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
VIEWPORT = {"width": 1280, "height": 1800}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
BLOCKED_RESOURCES = {"image", "font", "media"}

SETTLE_INTERVAL_MS = 250
SETTLE_STABLE_SAMPLES = 2
SETTLE_MIN_TEXT = 200
SETTLE_MAX_MS = 15_000


def render_html(url: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Return `url`'s HTML after the page's JavaScript has run."""
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise FetchError(
            "playwright is not installed - run: pip install playwright && playwright install chromium"
        ) from exc

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    viewport=VIEWPORT,
                    user_agent=USER_AGENT,
                    locale="en-IN",
                    timezone_id="Asia/Kolkata",
                    reduced_motion="reduce",
                )
                context.route("**/*", _block_heavy_resources)
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                _wait_for_stable_text(page, PlaywrightTimeout)
                html = page.content()
            finally:
                browser.close()
    except PlaywrightError as exc:
        raise FetchError(f"could not render {url} ({_first_line(exc)})") from exc

    log.info("Rendered %s (%d chars of HTML)", url, len(html))
    return html


def _block_heavy_resources(route, request) -> None:
    """Drop bytes that never affect extracted text."""
    if request.resource_type in BLOCKED_RESOURCES:
        route.abort()
    else:
        route.continue_()


def _wait_for_stable_text(page, timeout_error) -> None:
    """Wait until document text stops changing, not until the network is idle.

    Ad and analytics traffic can keep a connection open indefinitely, so
    `networkidle` either races or times out; text length settling is the signal
    that actually matters here.
    """
    script = """
        () => {
            const state = (window.__scrape_state ||= { last: -1, stable: 0 });
            const length = (document.body && document.body.innerText || '').length;
            state.stable = length === state.last ? state.stable + 1 : 0;
            state.last = length;
            return length >= %d && state.stable >= %d;
        }
    """ % (SETTLE_MIN_TEXT, SETTLE_STABLE_SAMPLES)

    try:
        page.wait_for_function(script, polling=SETTLE_INTERVAL_MS, timeout=SETTLE_MAX_MS)
    except timeout_error:
        log.warning("Text never settled within %dms - extracting the page as-is.", SETTLE_MAX_MS)


def _first_line(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0][:120]
