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

Asking was not enough, and the first live run on 2026-09-28 showed it three
ways, which is why `vet` exists:

  * Pages that cannot be read. education.gov.in answers a plain request with
    an empty app shell and a 200, and a browser with Akamai's "Access Denied" -
    so its scheme pages are real addresses that the extractor cannot read, and
    each came back as "From a search, not the page". Asking the model for good
    addresses cannot fix that; only trying to read them can. So every candidate
    is read the way a run would read it (fetch_page) before it is offered. One
    that cannot be read is swapped for another real search result about the
    same scheme that can (the grounding links are the pages the search actually
    returned), and where there is none it is offered UNticked and marked, so
    choosing a search-built draft is a decision rather than a surprise. A 404 or
    410 is a wrong address outright and is dropped when there is no swap.
  * A portal's home page. scholarships.gov.in/ and tribal.nic.in/ are a list of
    schemes and a ministry, not a scheme. A government site's bare home page is
    never one scheme's page, so it is dropped here rather than refused later.
  * Closed schemes. The National Overseas Scholarship closed on 31 July and was
    offered in September. A candidate whose closing date has passed is dropped;
    one with no date stays, because "not stated" is not "closed".
"""

import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import date
from urllib.parse import urlparse

from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout

import requests
from google.genai import types

from src.fetcher import fetch_page
from src.scrapers import FetchError

log = logging.getLogger(__name__)

DEFAULT_TOPIC = (
    "scholarships in India for students with disabilities (divyang / PwD) "
    "that are open now or opening soon"
)
MAX_RESULTS = 20
MAX_TOPIC_CHARS = 300
RESOLVE_TIMEOUT = 5.0
CHECK_TIMEOUT = 8.0
# Reading a candidate can mean launching Chromium, so they are read a few at a
# time and the whole check is capped. A candidate still being read at the cap is
# offered as unconfirmed rather than holding the answer up - the proxy allows
# ten minutes, but the operator is watching a spinner.
PROBE_WORKERS = 4
PROBE_DEADLINE = 50.0

# Status codes that mean the ADDRESS is wrong rather than that the site is
# slow or unwelcoming to a server. Only these drop a candidate: a timeout or a
# 403 from a government host is how some of them answer anything that is not a
# browser, and the extractor has a search fallback for exactly that.
_WRONG_ADDRESS = (404, 410)

# A bare home page on one of these is a department or a portal, never one
# scheme. A sponsor's own single-scheme site (sbiashascholarship.co.in) is a
# bare home page too and is the scheme, which is why this is a list of hosts
# rather than a rule about every root URL.
_PORTAL_SUFFIXES = (".gov.in", ".nic.in", "buddy4study.com", "vidyasaarathi.co.in")
_PORTAL_NAME = re.compile(r"\b(portal|schemes)\b", re.IGNORECASE)

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
    # False when the page could not be read the way a run reads it - blocked,
    # empty, or not answering. Offered unticked: reading it will fall back to a
    # search, which is weaker, and the operator should choose that knowingly.
    readable: bool = True


def _quiet(event: dict) -> None:
    """The progress callback when nobody is listening."""


def _host(url: str) -> str:
    return (urlparse(url).hostname or url).removeprefix("www.")


def find(client, model: str, topic: str | None = None, limit: int = 12,
         progress=None) -> tuple[list[dict], list[str]]:
    """Search for individual schemes on a topic.

    Returns the candidates worth offering and, for the ones that were not, one
    line each saying why - so the screen can say "3 left out: closed" rather
    than quietly showing fewer than were asked for.

    `progress(event)` is told what is happening as it happens: the search, what
    it returned, and each page as it is checked - see server.py's search jobs,
    which stream these to the panel. A search takes up to a minute, and "What
    is it doing, which sources is it checking" was the first thing asked of it
    in testing (2026-09-29). Each event is a dict with `stage` (search, check,
    done) and a `message` in plain words; a check also carries `url`, `name`
    and `status`, so the screen can update one row in place rather than append
    a line per state.
    """
    say = progress or _quiet
    topic = (topic or "").strip()[:MAX_TOPIC_CHARS] or DEFAULT_TOPIC
    limit = max(1, min(int(limit or 12), MAX_RESULTS))

    log.info("Searching the web for: %s", topic)
    say({"stage": "search", "message": f"Searching the web for “{topic}”"})
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
    parsed = parse(text)
    say({"stage": "search", "count": len(parsed),
         "message": f"The search returned {len(parsed)} scheme{'' if len(parsed) == 1 else 's'} — checking each one"})
    candidates, dropped = vet(parsed, grounding_links(response), date.today(), progress=say)
    candidates = candidates[:limit]

    say({"stage": "done", "count": len(candidates),
         "message": f"{len(candidates)} ready to read"
                    + (f", {len(dropped)} left out" if dropped else "")})
    log.info("Found %d scheme(s); left out %d.", len(candidates), len(dropped))
    for why in dropped:
        log.info("Left out: %s", why)
    return candidates, dropped


def vet(candidates: list[dict], links: list[tuple[str, str]], today: date,
        probe=None, progress=None) -> tuple[list[dict], list[str]]:
    """Keep what is open, is one scheme, and can actually be read.

    `probe(url)` answers "ok", "missing" (404/410) or "unreadable"; it is a
    parameter so the tests can say what the web would have said. The cheap rules
    run first, so nothing is fetched for a scheme that has closed.
    """
    probe = probe or _probe
    say = progress or _quiet
    kept: list[dict] = []
    dropped: list[str] = []

    def check(c, status, message, url=None):
        # One row per scheme on the screen, keyed by the address it was
        # offered with; a swap reports the new address beside it.
        say({"stage": "check", "key": c["_key"], "name": c["name"],
             "url": url or c["url"], "status": status, "message": message})

    pending: list[dict] = []
    for c in candidates:
        c["_key"] = c["url"]
        closes = _date(c.get("closes_at"))
        if closes and closes < today:
            dropped.append(f"{c['name']} - closed on {closes:%d %B %Y}")
            check(c, "closed", f"Closed on {closes:%d %B %Y} — left out")
            continue
        c["url"] = resolve(c["url"])
        if _is_portal(c["name"], c["url"]):
            dropped.append(f"{c['name']} - {c['url']} lists many schemes, not one")
            check(c, "portal", "A portal listing many schemes — left out")
            continue
        pending.append(c)

    def probed(c):
        """The probe, narrated: 'checking' when it starts, the verdict when it
        ends. Runs on the pool's threads; the callback must be thread-safe,
        which server.py's Job.emit is."""
        def run(url):
            check(c, "checking", f"Checking {_host(url)}…")
            verdict = probe(url)
            check(c, {"ok": "readable", "missing": "missing"}.get(verdict, "unreadable"),
                  {"ok": f"{_host(url)} can be read",
                   "missing": f"{_host(url)} does not exist"}.get(
                      verdict, f"{_host(url)} blocks automated reading"))
            return verdict
        return run

    by_url = {c["url"]: c for c in pending}
    verdicts = _probe_all(list(by_url), lambda u: probed(by_url[u])(u))
    for c in pending:
        verdict = verdicts.get(c["url"], "unreadable")
        if verdict != "ok":
            check(c, "looking", f"Looking for another page about {c['name']}…")
            better = _match(c["name"], links, probe, avoid=c["url"])
            if better:
                log.info("Swapped %s (%s) for %s from the search results.", c["url"], verdict, better)
                c["url"], verdict = better, "ok"
                check(c, "swapped", f"Found a readable page on {_host(better)}", url=better)
            else:
                check(c, "missing" if verdict == "missing" else "unreadable",
                      "No other page found — left out" if verdict == "missing"
                      else "No readable page — offered unticked")
        if verdict == "missing":
            dropped.append(f"{c['name']} - its address does not exist "
                           "and the search had no other page for it")
            continue
        c["readable"] = verdict == "ok"
        kept.append(c)

    for c in kept:
        c.pop("_key", None)

    # Readable first: they are the ones worth ticking.
    kept.sort(key=lambda c: not c["readable"])
    return _dedupe(kept), dropped


def _probe_all(urls: list[str], probe) -> dict[str, str]:
    """Probe several addresses at once, within PROBE_DEADLINE."""
    out: dict[str, str] = {}
    if not urls:
        return out
    pool = ThreadPoolExecutor(max_workers=PROBE_WORKERS)
    futures = {pool.submit(probe, u): u for u in urls}
    try:
        for done in as_completed(futures, timeout=PROBE_DEADLINE):
            try:
                out[futures[done]] = done.result()
            except Exception:
                out[futures[done]] = "unreadable"
    except FuturesTimeout:
        log.info("Stopped checking pages after %ss; the rest are offered unconfirmed.", PROBE_DEADLINE)
    pool.shutdown(wait=False, cancel_futures=True)
    return out


def _probe(url: str) -> str:
    """Read an address the way a run will, and say how it went."""
    status = _status(url)
    if status in _WRONG_ADDRESS:
        return "missing"
    try:
        fetch_page(url)
    except FetchError as exc:
        log.info("Cannot read %s: %s", url, exc)
        return "unreadable"
    return "ok"


def grounding_links(response) -> list[tuple[str, str]]:
    """The pages the search actually returned, as (title, address).

    These are real results, unlike an address the model writes into its answer,
    so they are what an unreadable address is swapped from. Missing metadata is
    an empty list, not an error: it is a better-if-present, not a requirement.
    """
    out: list[tuple[str, str]] = []
    try:
        chunks = response.candidates[0].grounding_metadata.grounding_chunks or []
    except (AttributeError, IndexError, TypeError):
        return out
    for chunk in chunks:
        web = getattr(chunk, "web", None)
        uri = getattr(web, "uri", None)
        if uri:
            out.append((getattr(web, "title", "") or "", uri))
    return out


def _match(name: str, links: list[tuple[str, str]], probe, avoid: str = "") -> str | None:
    """A search result that is plainly about this scheme and can be read, if any.

    Plainly means most of the scheme's distinctive words are in the result's
    title - not a similarity score, because a near miss here is a different
    scheme's page under this one's name, which is the error being fixed.
    """
    words = _words(name)
    if not words:
        return None
    for title, uri in links:
        if len(words & _words(title)) < max(2, (len(words) + 1) // 2):
            continue
        url = resolve(uri)
        if url.rstrip("/") == avoid.rstrip("/") or _is_portal(name, url):
            continue
        if probe(url) == "ok":
            return url
    return None


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
        f"Today is {date.today():%d %B %Y}. Only schemes that are open now or will open "
        "later - leave out every scheme whose last date to apply has already passed.\n\n"
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


_STOP = {
    "scheme", "scholarship", "scholarships", "for", "the", "of", "and", "in", "to",
    "a", "an", "students", "student", "national", "government", "india", "indian",
}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2 and w not in _STOP}


def _is_portal(name: str, url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    bare = parsed.path in ("", "/") and not parsed.query
    if bare and any(host == s.lstrip(".") or host.endswith(s) for s in _PORTAL_SUFFIXES):
        return True
    return bare and bool(_PORTAL_NAME.search(name))


def _status(url: str) -> int | None:
    """The status a page answers with, or None when nothing answered at all.

    GET rather than HEAD: enough government servers answer HEAD with 405 or 404
    while serving the page to GET that HEAD would drop real schemes. Streamed,
    so only the headers are read.
    """
    try:
        with requests.get(url, timeout=CHECK_TIMEOUT, stream=True, allow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 scholarship-discovery/1.0"}) as answer:
            return answer.status_code
    except requests.RequestException:
        return None


def _date(value) -> date | None:
    if not isinstance(value, str) or not _ISO_DATE.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


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
