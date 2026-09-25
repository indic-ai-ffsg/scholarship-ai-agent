"""One page, fetched by the cheapest tier that actually returns content.

Tier choice is mechanical, not a decision the agent makes: a plain request is
tried first, and the browser only runs when the plain request comes back with a
shell. Keeping this out of the agent's hands is deliberate - the agent decides
*which pages are worth reading*, never *how bytes are obtained*.
"""

import logging
from dataclasses import dataclass, field

from src.scrapers import (
    MAX_CHARS,
    MIN_USEFUL_CHARS,
    FetchError,
    Unreachable,
    fetch_html,
    join_text,
    parse_page,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Page:
    url: str
    text: str
    links: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    tier: str = "fetch"

    def summary(self) -> str:
        return f"{self.url} [{self.tier}, {len(self.text):,} chars, {len(self.links)} links]"


def fetch_page(url: str, max_chars: int = MAX_CHARS) -> Page:
    """Return `url` as text plus navigable links, escalating tiers as needed."""
    html, tier = "", "fetch"
    visible, images = "", []
    try:
        html = fetch_html(url)
        visible, embedded, links, images = parse_page(html, url)
        text = join_text(visible, embedded)
    except Unreachable as exc:
        log.warning("%s", exc)
        raise
    except FetchError as exc:
        log.warning("Plain fetch fell short - %s", exc)
        text, links = "", []
    if len(visible) < MIN_USEFUL_CHARS or not links:
        if html:
            log.info(
                "%s from %s - rendering it.",
                f"Only {len(visible)} chars of visible text"
                if len(visible) < MIN_USEFUL_CHARS else "No followable links",
                url,
            )
        from src.render import render_html

        rendered = render_html(url)
        rendered_visible, rendered_embedded, rendered_links, rendered_images = parse_page(rendered, url)
        rendered_text = join_text(rendered_visible, rendered_embedded)
        
        if len(rendered_visible) > len(visible) or len(rendered_links) > len(links):
            html, text, links, tier = rendered, rendered_text, rendered_links, "render"
            visible, images = rendered_visible, rendered_images

        if len(text) < MIN_USEFUL_CHARS:
            raise FetchError(
                f"{url} yielded only {len(text)} characters even after rendering - "
                "it may be behind a bot check or a login"
            )

    page = Page(
        url=url,
        text=text[:max_chars],
        links=links,
        images=images,
        tier=tier,
    )
    log.info("Read %s", page.summary())
    return page
