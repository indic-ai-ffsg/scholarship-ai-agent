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
CONNECT_TIMEOUT = 5.0
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


class Unreachable(FetchError):
    """The host never answered at all - no TCP connection, no DNS, no TLS.

    Kept apart from every other FetchError because it is the one failure the
    renderer cannot do anything about. Chromium is a second HTML engine, not a
    second network stack: if requests could not open a socket to the host,
    Chromium will not either, and it will take thirty seconds to find out.

    scholarship.odisha.gov.in is the case that made this worth a class of its
    own. It loads in 0.2s from a laptop and does not route to Railway's egress
    at all, so a run there spent fifteen seconds failing to connect, thirty more
    failing to connect again through a browser, and only then began the search
    that was always going to be the answer.
    """


def fetch_html(url: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Return the raw HTML at `url`.

    Raises Unreachable when the host never answered, and FetchError for
    everything a caller can still recover from by trying another tier: HTTP
    errors, non-HTML responses, and a body too thin to use.
    """
    try:
        response = requests.get(url, headers=_HEADERS, timeout=(CONNECT_TIMEOUT, timeout))
        response.raise_for_status()
    except (requests.ConnectionError, requests.ConnectTimeout) as exc:
        raise Unreachable(f"could not reach {url} ({_brief(exc)})") from exc
    except requests.RequestException as exc:
        raise FetchError(f"could not fetch {url} ({exc})") from exc

    content_type = response.headers.get("Content-Type", "")
    if content_type and not content_type.startswith(("text/", "application/xhtml")):
        raise FetchError(f"{url} returned {content_type}, not a web page")

    if response.encoding is None:
        response.encoding = response.apparent_encoding

    return response.text


# A scheme's name, as it appears in running text. The tail is the name; the head
# is whatever sentence happened to carry it, which is why matches are keyed on
# their last few words - "How do I apply for the SBI Asha Scholarship" and "Key
# milestones for the SBI Asha Scholarship" are one scheme, not two.
_SCHEME_NAME = re.compile(r"[A-Z][A-Za-z0-9'&.,()\- ]{12,80}?(?:Scholarship|Fellowship|Scheme)\b")
_NAME_KEY_WORDS = 5


def scheme_names(text: str) -> list[str]:
    """Distinct scholarship names the text appears to list.

    Deliberately crude, and only ever used to answer "is this page about one
    scheme or about many?" - not to extract anything. Measured on three real
    pages: one scheme on sbiashascholarship.co.in, two on depwd's umbrella
    index, forty-two on scholarships.gov.in/All-Scholarships.
    """
    seen: dict[str, str] = {}
    for match in _SCHEME_NAME.findall(text):
        name = " ".join(match.split())
        # A fragment, not a name: the regex starts at a capital, so a scheme
        # written "X (Technical Degree) (Welfare Based Scheme)" also yields the
        # tail from its second bracket. Unbalanced brackets are the tell, and
        # counting is enough - these are only ever shown to a person.
        if name.count("(") != name.count(")"):
            continue
        key = " ".join(name.lower().split()[-_NAME_KEY_WORDS:])
        seen.setdefault(key, name)
    return list(seen.values())


def _brief(exc: Exception) -> str:
    """requests wraps urllib3 wraps socket; the useful part is the last clause."""
    text = str(exc)
    return text[-160:] if len(text) > 160 else text


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
    return _links_from(BeautifulSoup(html, "html.parser"), base_url, limit)


def _links_from(soup, base_url: str, limit: int = LINK_LIMIT) -> list[str]:
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
    return _images_from(BeautifulSoup(html, "html.parser"), base_url, limit)


def _images_from(soup, base_url: str, limit: int = IMAGE_LIMIT) -> list[str]:
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
    return _text_from(BeautifulSoup(html, "html.parser"))


def _text_from(soup) -> tuple[str, str]:
    """DESTRUCTIVE: decomposes `soup`. Nothing may read it afterwards."""
    embedded = _embedded_state(soup)

    for element in soup(_INERT_TAGS):
        element.decompose()
    whole = soup.get_text(separator="\n", strip=True)

    for element in soup(_CHROME_TAGS):
        element.decompose()
    visible = soup.get_text(separator="\n", strip=True)

    if len(visible) < MIN_USEFUL_CHARS <= len(whole):
        log.info(
            "Chrome tags held %d of %d characters - keeping them; this page marks "
            "its content as <header> or <nav>.", len(whole) - len(visible), len(whole),
        )
        visible = whole

    return visible, embedded


EMBEDDED_HEADING = (
    "--- embedded page data (from the page's own scripts; it is build-time and "
    "can be out of date, so where it disagrees with the text above, the text "
    "above is what the page actually shows) ---"
)


def parse_page(html: str, base_url: str) -> tuple[str, str, list[str], list[str]]:
    """Everything a fetched page yields, from ONE parse of it.

    Building the soup is 87-94% of the cost of each extractor - measured on
    three real portals, 12ms for a 100KB page and 30ms for a 205KB one - and
    fetch_page wanted three of them from the same bytes: text, links, images.
    So it paid that cost three times, and six when it escalated to the renderer,
    for a document that had not changed in between.

    The order is not arbitrary. Links and images only read the tree; the text
    pass decomposes it, and a decomposed tree has no anchors left to find. Text
    goes last and nothing touches the soup afterwards.

    Returns (visible, embedded, links, images).
    """
    soup = BeautifulSoup(html, "html.parser")
    links = _links_from(soup, base_url)
    images = _images_from(soup, base_url)
    visible, embedded = _text_from(soup)
    return visible, embedded, links, images


def join_text(visible: str, embedded: str) -> str:
    if visible and embedded:
        return f"{visible}\n\n{EMBEDDED_HEADING}\n{embedded}"
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
