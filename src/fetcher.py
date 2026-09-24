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
    extract_images,
    extract_links,
    extract_text_from_html,
    fetch_html,
    html_to_text,
    join_text,
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
    visible = ""
    try:
        html = fetch_html(url)
        visible, embedded = extract_text_from_html(html)
        text = join_text(visible, embedded)
    except FetchError as exc:
        log.warning("Plain fetch fell short - %s", exc)
        text = ""

    links = extract_links(html, url) if html else []
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
        rendered_visible, rendered_embedded = extract_text_from_html(rendered)
        rendered_text = join_text(rendered_visible, rendered_embedded)
        rendered_links = extract_links(rendered, url)
        
        if len(rendered_visible) > len(visible) or len(rendered_links) > len(links):
            html, text, links, tier = rendered, rendered_text, rendered_links, "render"
            visible = rendered_visible

        if len(text) < MIN_USEFUL_CHARS:
            raise FetchError(
                f"{url} yielded only {len(text)} characters even after rendering - "
                "it may be behind a bot check or a login"
            )

    page = Page(
        url=url,
        text=text[:max_chars],
        links=links,
        images=extract_images(html, url),
        tier=tier,
    )
    log.info("Read %s", page.summary())
    return page
