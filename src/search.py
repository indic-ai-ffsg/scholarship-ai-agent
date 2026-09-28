"""Find scholarships worth reading, so nobody has to go looking for URLs first.

The extractor reads one scheme per source and refuses a page that lists many
(src/agent.py, CatalogueSource) - rightly, because asked for one record from a
catalogue it produces a plausible scheme that does not exist. What that left the
operator was a screen that would only work once they had already found every
scheme's own page by hand, and a search for "scholarships for disabled students"
typed into it came back as a refusal. The people using it read that as the tool
having stopped finding scholarships, and they were right about the effect.

So finding and reading are two steps, and this is the first:

    topic --search--> candidates (name, sponsor, the scheme's OWN page) --> operator ticks
                                                                        --> the ordinary run

It returns a list and writes nothing. Each candidate is then read by the same
extractor, one scheme per source, with the same catalogue refusal and the same
Save-by-a-person at the end - so a search cannot put anything in the catalogue
that reading the page by hand would not.

What it asks the model for, and why each part matters:

  * the scheme's own page, not a portal's listing of it - a candidate whose URL
    is a catalogue would only be refused one step later;
  * only what the search results say - a name the model remembers is how a
    scheme that closed three years ago reappears as open;
  * no guessing at a URL - a plausible address on the right domain is worse
    than none, because it fetches a 404 and grounds on the name instead.

The URL rule is enforced here as well as asked for: anything that is not an
http(s) address is dropped, and duplicates of the same page or the same name
are merged, because a model asked for fifteen results will happily return the
same scheme three times under three spellings.
"""

import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import date
from urllib.parse import urlparse

import requests
from google.genai import types

log = logging.getLogger(__name__)

DEFAULT_TOPIC = (
    "scholarships in India for students with disabilities (divyang / PwD) "
    "that are open now or opening soon"
)
MAX_RESULTS = 20
MAX_TOPIC_CHARS = 300
RESOLVE_TIMEOUT = 5.0

# Search results often come back as Google's own redirect links. They work, but
# a draft whose source is vertexaisearch.cloud.google.com is unreadable to the
# person reviewing it and cannot be watched for changes, so they are followed
# to where they land before being offered.
_REDIRECT_HOSTS = ("vertexaisearch.cloud.google.com", "www.google.com", "google.com")

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)
_JSON_ARRAY = re.compile(r"\[.*\]", re.DOTALL)
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SearchError(RuntimeError):
    """The search answered, but not with anything we could use."""


@dataclass
class Candidate:
    name: str
    url: str
    sponsor: str | None = None
    closes_at: str | None = None
    note: str | None = None


def find(client, model: str, topic: str | None = None, limit: int = 12) -> list[dict]:
    """Search for individual schemes on a topic and return them as candidates."""
    topic = (topic or "").strip()[:MAX_TOPIC_CHARS] or DEFAULT_TOPIC
    limit = max(1, min(int(limit or 12), MAX_RESULTS))

    log.info("Searching the web for: %s", topic)
    response = client.models.generate_content(
        model=model,
        contents=_prompt(topic, limit),
        config=types.GenerateContentConfig(
            system_instruction=f"Today's date is {date.today():%Y-%m-%d}.",
            temperature=0.0,
            tools=[types.Tool(google_search=types.GoogleSearch())],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )

    text = getattr(response, "text", None) or ""
    candidates = parse(text)[:limit]
    for candidate in candidates:
        candidate["url"] = resolve(candidate["url"])
    candidates = _dedupe(candidates)

    log.info("Found %d scheme(s).", len(candidates))
    return candidates


def parse(text: str) -> list[dict]:
    """Candidates out of a reply that may be fenced, padded, or partly wrong.

    Kept separate from `find` so it can be tested without a network or a key.
    Anything malformed is dropped rather than repaired: a candidate with no
    address, or an address that is not a web page, is nothing the extractor can
    read, and offering it would put a row on screen that can only fail.
    """
    items = _array(text)
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"))
        url = _text(item.get("url"))
        if not name or not url or not _is_web(url):
            continue
        closes = _text(item.get("closes_at"))
        out.append(asdict(Candidate(
            name=name[:200],
            url=url,
            sponsor=_text(item.get("sponsor")),
            closes_at=closes if closes and _ISO_DATE.match(closes) else None,
            note=_text(item.get("note")),
        )))
    return _dedupe(out)


def resolve(url: str) -> str:
    """Follow a search redirect to the page it points at, or leave it alone."""
    host = (urlparse(url).hostname or "").lower()
    if host not in _REDIRECT_HOSTS:
        return url
    try:
        answer = requests.head(url, allow_redirects=True, timeout=RESOLVE_TIMEOUT,
                               headers={"User-Agent": "scholarship-discovery/1.0"})
        landed = answer.url
    except requests.RequestException as exc:
        log.info("Could not follow a search link (%s); keeping it as it is.", exc)
        return url
    return landed if _is_web(landed) else url


def _prompt(topic: str, limit: int) -> str:
    return (
        f"Search the web and list up to {limit} individual scholarship schemes for: {topic}\n\n"
        "Rules:\n"
        "- One entry per scheme. Never an entry for a portal, a directory or a list of "
        "schemes (for example the National Scholarship Portal home page, or a Buddy4Study "
        "category page). If a portal hosts a scheme, give the scheme itself.\n"
        "- `url` must be the scheme's own page - on the sponsor's site where there is one, "
        "otherwise the page for that one scheme on a portal. Copy it from a search result. "
        "Never construct or guess an address.\n"
        "- Only include what the search results say. If a result does not state the closing "
        "date, leave `closes_at` null. Do not fill it from memory.\n"
        "- Prefer schemes that are open now or open within the next few months; skip schemes "
        "whose last date has clearly passed.\n\n"
        "Reply with a JSON array and nothing else - no prose, no code fences. Each element:\n"
        '{"name": str, "sponsor": str | null, "url": str, '
        '"closes_at": "YYYY-MM-DD" | null, "note": str | null}\n'
        "`note` is at most one short sentence on who it is for."
    )


def _array(text: str) -> list:
    for candidate in (text, _first(_JSON_FENCE, text), _first(_JSON_ARRAY, text)):
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            return value
        if isinstance(value, dict) and isinstance(value.get("results"), list):
            return value["results"]
    raise SearchError(f"the search did not come back as a list: {text[:300]}")


def _dedupe(candidates: list[dict]) -> list[dict]:
    """One row per page and one row per scheme name, first seen wins."""
    seen_urls: set[str] = set()
    seen_names: set[str] = set()
    out = []
    for c in candidates:
        url_key = c["url"].rstrip("/").lower()
        name_key = re.sub(r"[^a-z0-9]", "", c["name"].lower())
        if url_key in seen_urls or (name_key and name_key in seen_names):
            continue
        seen_urls.add(url_key)
        seen_names.add(name_key)
        out.append(c)
    return out


def _is_web(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.hostname) and " " not in url


def _text(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value or None


def _first(pattern: re.Pattern, text: str) -> str | None:
    match = pattern.search(text)
    if not match:
        return None
    return match.group(1) if match.groups() else match.group(0)
