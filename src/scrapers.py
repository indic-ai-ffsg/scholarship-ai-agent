"""Fetch a web page and reduce it to the readable text the extractor needs.

Many scholarship portals (Buddy4Study, most Next.js sites) ship an empty shell
and render in the browser, so the visible DOM holds a headline and nothing else.
Their real content sits in an embedded JSON island, which is why this module
reads those *before* stripping <script> tags.
"""

import html as html_lib
import json
import logging
import re

from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15.0
MAX_CHARS = 60_000
MIN_USEFUL_CHARS = 400
LINK_LIMIT = 60
IMAGE_LIMIT = 8

_LOGO_WORDS = ("logo", "emblem", "brand", "crest", "mark", "header-img", "site-icon")
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# Split in two, because the two halves are not equally safe to remove.
#
# The first four hold no prose at any depth, so removing them cannot cost
# content. The second five are PAGE CHROME - and only when they are the page's
# chrome. HTML5 allows a <header> inside every sectioning element, and an older
# government template will happily wrap its entire body in <header> or <nav>;
# depwd.gov.in/scholarships does exactly that. Stripping by tag name there took
# a 3,371-character page down to 393, which is below MIN_USEFUL_CHARS, so it
# rendered in Chromium (six seconds, same 393 characters) and then raised
# FetchError. A page that was perfectly readable failed to extract at all.
#
# See extract_text_from_html for the guard that keeps that from happening.
_INERT_TAGS = ("script", "style", "noscript", "svg")
_CHROME_TAGS = ("nav", "header", "footer", "aside", "form")

_NOISE_TAGS = _INERT_TAGS + _CHROME_TAGS


_NOISE_KEYS = (
    "logourl", "bannerimageurl", "mobilebannerimageurl", "imageurl",
    "logo", "icon", "thumbnail", "image", "banner", "favicon",
    "youtubevideo", "css", "script",
)

_SKIP_URL_PARTS = (
    "/login", "/register", "/signup", "/privacy", "/terms", "/contact",
    "/about", "/media", "/career", "/sitemap", "/faq", "/blog/tag",
)


class FetchError(RuntimeError):
    """A page could not be fetched, or held nothing worth extracting."""


def fetch_html(url: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Return the raw HTML at `url`.

    Raises FetchError for anything a caller can recover from by trying another
    tier: network failures, HTTP errors, and non-HTML responses.
    """
    try:
        response = requests.get(url, headers=_HEADERS, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise FetchError(f"could not fetch {url} ({exc})") from exc

    content_type = response.headers.get("Content-Type", "")
    if content_type and not content_type.startswith(("text/", "application/xhtml")):
        raise FetchError(f"{url} returned {content_type}, not a web page")

    if response.encoding is None:
        response.encoding = response.apparent_encoding

    return response.text


def html_to_text(html: str) -> str:
    """Visible text plus flattened embedded JSON, as one block."""
    visible, embedded = extract_text_from_html(html)
    log.debug("html_to_text: %d visible, %d embedded", len(visible), len(embedded))
    return join_text(visible, embedded)


def extract_links(html: str, base_url: str, limit: int = LINK_LIMIT) -> list[str]:
    """Return "label -> url" lines an agent can navigate by, best candidates first.

    Same-host only, and ranked rather than taken in document order: a portal's
    mega-menu appears before its content, so the first 60 anchors on a scheme
    page are category and marketing links, not the sub-schemes worth reading.
    """
    soup = BeautifulSoup(html, "html.parser")
    host = urlparse(base_url).netloc
    seed_tokens = _tokens(urlparse(base_url).path)

    seen: set[str] = set()
    scored: list[tuple[int, str]] = []

    for anchor in soup.find_all("a", href=True):
        url = urljoin(base_url, anchor["href"]).split("#")[0].rstrip("/")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or parsed.netloc != host:
            continue
        if url in seen or url.rstrip("/") == base_url.rstrip("/"):
            continue
        label = " ".join(anchor.get_text(separator=" ", strip=True).split())[:80]
        if not label or any(skip in url.lower() for skip in _SKIP_URL_PARTS):
            continue
        seen.add(url)
        scored.append((_link_score(parsed.path, seed_tokens), f"{label} -> {url}"))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [line for _, line in scored[:limit]]


def extract_images(html: str, base_url: str, limit: int = IMAGE_LIMIT) -> list[str]:
    """Return candidate logo URLs, the most likely first.

    The text pipeline throws images away on purpose - `_NOISE_KEYS` lists
    logourl, banner and the rest, because none of them is extraction material
    and a base64 sprite in a JSON island is thousands of tokens of nothing. A
    listing does carry one picture though, and the sponsor's own mark is the
    only image on the page worth having, so the candidates are collected here
    and offered to the model separately from the prose.

    Ranked rather than taken in document order: a masthead logo is usually the
    first <img> on the page, but a hero banner often beats it, and the banner is
    a picture of students rather than the sponsor's mark.
    """
    soup = BeautifulSoup(html, "html.parser")
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()

    def offer(raw: str | None, score: int) -> None:
        if not raw:
            return
        url = urljoin(base_url, raw.strip()).split("#")[0]
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return
        path = parsed.path.lower()
        if path.endswith(".svg"):
            return
        if url in seen:
            return
        seen.add(url)
        if any(word in url.lower() for word in _LOGO_WORDS):
            score += 6
        if path.endswith(_IMAGE_SUFFIXES):
            score += 1
        scored.append((score, url))

    for prop in ("og:image", "twitter:image", "og:logo"):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if tag:
            offer(tag.get("content"), 4)

    for image in soup.find_all("img", src=True):
        words = " ".join([
            str(image.get("alt") or ""),
            str(image.get("class") or ""),
            str(image.get("id") or ""),
            str(image.get("title") or ""),
        ]).lower()
        score = 5 if any(word in words for word in _LOGO_WORDS) else 0
        if image.find_parent(["header", "nav"]) is not None:
            score += 3
        offer(image.get("src"), score)

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [url for _, url in scored[:limit]]


def _tokens(path: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", path.lower()) if len(t) > 2}


def _link_score(path: str, seed_tokens: set[str]) -> int:
    """Rank a candidate link by how likely it is to be scheme detail."""
    path = path.lower()
    score = 2 * len(_tokens(path) & seed_tokens)
    if "/scholarship/" in path:
        score += 4
    if "/page/" in path:
        score += 2
    for noise, penalty in (
        ("scholarship-result", 6),
        ("/scholarships/", 3),
        ("/article/", 3),
        ("/blog", 3),
    ):
        if noise in path:
            score -= penalty
    return score


def extract_text_from_html(html: str) -> tuple[str, str]:
    """Split page HTML into (visible text, flattened embedded JSON).

    Shared by the plain fetcher and the browser renderer so both produce text
    the extractor sees the same way.
    """
    soup = BeautifulSoup(html, "html.parser")

    embedded = _embedded_state(soup)

    for element in soup(_INERT_TAGS):
        element.decompose()
    whole = soup.get_text(separator="\n", strip=True)

    for element in soup(_CHROME_TAGS):
        element.decompose()
    visible = soup.get_text(separator="\n", strip=True)

    # The guard. Stripping chrome is right on a page that HAS chrome, and
    # catastrophic on one that mislabels its content as chrome - and the two are
    # indistinguishable by tag name, which is why this is measured instead of
    # guessed at.
    #
    # It fires only when the strip is the difference between a usable page and a
    # failed one: below the floor after, above it before. That is deliberately
    # narrow. On an ordinary page a mega-menu really can be half the text and
    # removing it really is the right answer, so a share-based rule ("keep the
    # unstripped text when stripping costs more than 60%") would fire constantly
    # and put every navigation menu back into the extraction corpus.
    if len(visible) < MIN_USEFUL_CHARS <= len(whole):
        log.info(
            "Chrome tags held %d of %d characters - keeping them; this page marks "
            "its content as <header> or <nav>.", len(whole) - len(visible), len(whole),
        )
        visible = whole

    return visible, embedded


def join_text(visible: str, embedded: str) -> str:
    return "\n\n".join(p for p in (visible, embedded) if p)


def _embedded_state(soup: BeautifulSoup) -> str:
    """Flatten the page's JSON islands (__NEXT_DATA__, ld+json) into text."""
    lines: list[str] = []
    seen: set[str] = set()

    scripts = soup.find_all("script", id="__NEXT_DATA__")
    scripts += soup.find_all("script", attrs={"type": "application/ld+json"})
    scripts += soup.find_all("script", attrs={"type": "application/json"})

    for script in scripts:
        raw = script.string or script.get_text()
        if not raw or not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        _flatten(_page_payload(payload), lines, seen)

    return "\n".join(lines)


def _page_payload(payload):
    """Next.js keeps the page's own data under props.pageProps; the rest of the
    blob is build config and a site-wide translation dictionary."""
    if isinstance(payload, dict):
        page_props = payload.get("props", {})
        if isinstance(page_props, dict) and page_props.get("pageProps"):
            return page_props["pageProps"]
    return payload


def _flatten(node, lines: list[str], seen: set[str], key: str = "") -> None:
    """Walk parsed JSON, emitting `key: value` lines for content-bearing leaves."""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and k.lower() in _NOISE_KEYS:
                continue
            _flatten(v, lines, seen, k)
        return

    if isinstance(node, list):
        for item in node:
            _flatten(item, lines, seen, key)
        return

    if node is None or isinstance(node, bool):
        return

    if isinstance(node, (int, float)):
        # Timestamps and numeric ids carry no meaning for extraction.
        if key.lower().endswith("id") or abs(node) > 10_000_000:
            return
        text = str(node)
    else:
        text = _as_text(str(node))
        if not text or text.startswith("data:"):
            return

    line = f"{key}: {text}" if key else text
    if line not in seen:
        seen.add(line)
        lines.append(line)


def _as_text(value: str) -> str:
    """Strip markup out of the HTML fragments these payloads embed."""
    if "<" in value and ">" in value:
        value = BeautifulSoup(value, "html.parser").get_text(separator=" ", strip=True)
    return html_lib.unescape(value).strip()
