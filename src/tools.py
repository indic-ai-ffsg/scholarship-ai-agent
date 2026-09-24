"""The tools the gathering agent may call, and the budgets it works within.

The agent chooses *which pages to read*. It never chooses how they are fetched,
whether to trust them, or what the output looks like - those stay mechanical, so
an agent that goes wandering still cannot change the shape or provenance of the
result.
"""

import logging
import os
import time
from urllib.parse import urlparse

from google.genai import types

from src.fetcher import Page, fetch_page
from src.scrapers import FetchError

log = logging.getLogger(__name__)

LOOP_PAGE_CHARS = 6_000


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        log.warning("%s is not a number - using %d.", name, default)
        return default


class PageGatherer:
    """Executes the agent's page requests and enforces its budget."""

    def __init__(self, seed_url: str):
        self.seed_url = seed_url
        self.seed_host = urlparse(seed_url).netloc
        self.max_pages = _env_int("SCHOLARSHIP_MAX_PAGES", 5)
        self.max_steps = _env_int("SCHOLARSHIP_MAX_STEPS", 8)
        self.max_seconds = _env_int("SCHOLARSHIP_MAX_SECONDS", 120)
        self.allowed = {
            d.strip().lower()
            for d in os.getenv("SCHOLARSHIP_ALLOWED_DOMAINS", "").split(",")
            if d.strip()
        }
        self.pages: dict[str, Page] = {}
        self.finished_reason: str | None = None
        self._started = time.monotonic()

    # --- budget -----------------------------------------------------------
    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started

    def exhausted(self) -> str | None:
        if len(self.pages) >= self.max_pages:
            return f"page budget reached ({self.max_pages})"
        if self.elapsed > self.max_seconds:
            return f"time budget reached ({self.max_seconds}s)"
        return None

    def _permitted(self, url: str) -> str | None:
        host = urlparse(url).netloc.lower()
        if not host:
            return "that is not an absolute URL"
        if host == self.seed_host.lower() or host in self.allowed:
            return None
        return (
            f"{host} is outside the source site ({self.seed_host}). Only pages on "
            "the source site, or SCHOLARSHIP_ALLOWED_DOMAINS, may be opened."
        )

    # --- tools ------------------------------------------------------------
    def open_page(self, url: str = "") -> dict:
        url = (url or "").strip()
        if not url:
            return {"error": "no url given"}
        if url in self.pages:
            return {"error": "already read", "url": url}

        refusal = self._permitted(url)
        if refusal:
            log.warning("Refused %s - %s", url, refusal)
            return {"error": refusal, "url": url}

        spent = self.exhausted()
        if spent:
            return {"error": f"cannot open more pages: {spent}", "url": url}

        try:
            page = fetch_page(url)
        except FetchError as exc:
            return {"error": str(exc), "url": url}

        self.pages[url] = page
        return {
            "url": url,
            "fetched_via": page.tier,
            "total_chars": len(page.text),
            "text": page.text[:LOOP_PAGE_CHARS],
            "truncated": len(page.text) > LOOP_PAGE_CHARS,
            "links": page.links[:25],
            "pages_remaining": self.max_pages - len(self.pages),
        }

    def finish_gathering(self, reason: str = "") -> dict:
        self.finished_reason = reason or "agent stopped"
        return {"ok": True, "pages_read": len(self.pages)}

    def dispatch(self, name: str, args: dict) -> dict:
        handler = {"open_page": self.open_page, "finish_gathering": self.finish_gathering}.get(name)
        if handler is None:
            return {"error": f"unknown tool {name}"}
        try:
            return handler(**args)
        except TypeError as exc:
            return {"error": f"bad arguments for {name}: {exc}"}

    # --- corpus -----------------------------------------------------------
    def corpus(self, max_chars: int) -> str:
        """Everything read, newest last, trimmed to the extraction budget.

        Each page's image candidates ride along with its text, and that is not
        decoration: `html_to_text` strips every image out on purpose - none of
        them is extraction material - so the one picture a listing has room for,
        the sponsor's own mark, was invisible to the call that fills the record.
        The brief the GATHERING model reads had them and the extraction did not,
        which is a difference that looks like the model ignoring a field.
        """
        blocks = []
        for page in self.pages.values():
            block = f"===== SOURCE: {page.url} =====\n{page.text}"
            if getattr(page, "images", None):
                listed = "\n".join(page.images[:8])
                block += (
                    "\n\n--- images on this page, most likely the sponsor's mark first ---\n"
                    f"{listed}"
                )
            blocks.append(block)
        return "\n\n".join(blocks)[:max_chars]


def declarations() -> types.Tool:
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="open_page",
                description=(
                    "Read a page on the source site and return its text and outgoing "
                    "links. Use it to follow a link to a specific scheme, an eligibility "
                    "page, or an FAQ when the page you have is incomplete."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "url": types.Schema(
                            type=types.Type.STRING,
                            description="Absolute URL on the source site.",
                        )
                    },
                    required=["url"],
                ),
            ),
            types.FunctionDeclaration(
                name="finish_gathering",
                description=(
                    "Call when the pages read already cover the scholarship's dates, "
                    "amounts, eligibility, documents and how to apply - or when no "
                    "remaining link would add any of those."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "reason": types.Schema(
                            type=types.Type.STRING,
                            description="One line on why gathering is complete.",
                        )
                    },
                    required=["reason"],
                ),
            ),
        ]
    )
