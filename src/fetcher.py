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
    fetch_html,
    html_to_text,
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
    try:
        html = fetch_html(url)
        text = html_to_text(html)
    except FetchError as exc:
        log.warning("Plain fetch fell short - %s", exc)
        text = ""

    links = extract_links(html, url) if html else []

    # Two reasons to render, not one.
    #
    # Too little text was the original test and it is still right. What it
    # missed is a page that returns plenty of text and NOTHING TO FOLLOW. That
    # is not a page that was read successfully, it is a dead end for the gather
    # stage, which navigates by links and would have none.
    #
    # buddy4study.com/scholarships is the case that showed it: the plain fetch
    # returns 13,883 characters and 0 links, because the text is the SEO blob in
    # __NEXT_DATA__ rather than the listing, which is fetched after hydration.
    # Being well over MIN_USEFUL_CHARS it looked like a clean tier-0 read and
    # stopped the escalation. Rendered, the same URL gives 43,140 characters and
    # 60 links, and they are the scholarships, named, with their deadlines.
    if len(text) < MIN_USEFUL_CHARS or not links:
        if html:
            log.info(
                "%s from %s - rendering it.",
                f"Only {len(text)} chars" if len(text) < MIN_USEFUL_CHARS else "No followable links",
                url,
            )
        from src.render import render_html

        rendered = render_html(url)
        rendered_text = html_to_text(rendered)
        rendered_links = extract_links(rendered, url)

        # Kept only when it is actually better; the plain read stands otherwise.
        # Rendering is downstream of the data and picks up variance a plain
        # fetch does not (render.py's header says why), so it has to earn the
        # swap rather than win by going second.
        if len(rendered_text) >= MIN_USEFUL_CHARS or len(rendered_links) > len(links):
            html, text, links, tier = rendered, rendered_text, rendered_links, "render"

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
