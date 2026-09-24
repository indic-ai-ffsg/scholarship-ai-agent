"""Turn a scholarship URL or a block of pasted text into a structured record.

Three stages, and only the middle one is agentic:

  1. fetch     deterministic - the seed page, cheapest tier that works
  2. gather    agentic       - the model decides which further pages to read
  3. extract   deterministic - one schema-constrained call over everything read

Caching is content-addressed rather than time-boxed: the seed page is fetched
every run (cheap) and its text hashed, so a cached record is reused only while
the source is genuinely unchanged, and a changed source re-extracts immediately
instead of waiting out a TTL.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date

from google import genai
from google.genai import types

from src.cache import ResultCache
from src.fetcher import fetch_page
from src.normalise import normalise, parse_date
from src.schema import SYSTEM_INSTRUCTION, ScholarshipSchema
from src.scrapers import FetchError
from src.tools import PageGatherer, declarations

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-2.5-flash"
TEMPERATURE = 0.0
EXTRACT_CORPUS_CHARS = 100_000
NAME_ONLY_CHARS = 400

_NO_AFC = types.AutomaticFunctionCallingConfig(disable=True)

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)

MATERIAL_FIELDS = (
    "name",
    "sponsor",
    "sponsor_type",
    "opens_at",
    "closes_at",
    "academic_year",
    "award_amount_min",
    "award_amount_max",
    "award_basis",
    "scholarship_type",
    "who_qualifies",
    "contacts",
)

GATHER_INSTRUCTION = """
You are gathering source material about one scholarship before it is extracted.

You have already been given the page the user supplied. Decide whether it covers
the scholarship's dates, award amounts, eligibility, required documents and how
to apply. If it does, call finish_gathering immediately - extra pages cost money
and add noise.

Only open a link when you can name the missing field it should supply, and
prefer a link that is clearly about this same scholarship. Do not open links to
unrelated scholarships, category listings, result announcements or articles.
Call finish_gathering as soon as no remaining link would fill a gap.
"""


class ExtractionError(RuntimeError):
    """The model answered, but not with a record we could parse."""


@dataclass
class Report:
    """What a run actually did - the sweep needs this, a one-off run ignores it."""

    reused: bool = False
    grounded: bool = False
    pages_read: int = 0
    changes: list[tuple[str, object, object]] = field(default_factory=list)
    reworded: int = 0
    corrections: list[str] = field(default_factory=list)


class ScholarshipAgent:
    def __init__(self, model_name: str | None = None, use_cache: bool = True):
        api_key = os.getenv("LLM_API_KEY")
        if not api_key:
            raise ValueError(
                "LLM_API_KEY is not set. Copy .env.example to .env and add your Gemini key."
            )

        self.model_name = model_name or os.getenv("MODEL", DEFAULT_MODEL)
        self.client = genai.Client(api_key=api_key)
        self.cache = ResultCache() if use_cache else None

    # --- public ----------------------------------------------------------
    def process(self, raw_input: str) -> dict:
        record, _ = self.run(raw_input)
        return record

    def run(
        self, raw_input: str, reuse: bool = True, ground_on_failure: bool = True,
    ) -> tuple[dict, Report]:
        """Extract, and report whether the source moved since it was last read.

        `reuse=False` re-reads a source whose cached record would otherwise be
        served, and still stores what it gets. That is deliberately not the same
        as constructing the agent with `use_cache=False`, which turns the cache
        off in both directions: an operator asking for a source to be read again
        wants the new record kept, not thrown away - otherwise the stale one is
        served to the next caller and the sweep goes on comparing against it.
        """
        text = raw_input.strip()
        if not text:
            raise ValueError("Nothing to extract: the input was empty.")

        report = Report()
        is_url = text.startswith(("http://", "https://"))
        seed = None

        if is_url:
            try:
                seed = fetch_page(text)
                fingerprint = ResultCache.key_for(seed.text)
            except FetchError as exc:
                # Searching for a page we could not read is right when somebody
                # has just asked for that URL: they want the scheme, the address
                # was only how they named it, and a grounded draft marked as
                # grounded is better than nothing.
                #
                # It is wrong on a sweep, and the sweep is what made that
                # visible. A watched URL whose host is down for ten minutes was
                # searched for instead, and the empty record that came back was
                # written over the good one - award_amount 45,000 to null,
                # closes_at to null, the name to "Unknown Scholarship". Nobody
                # asked for that page today; it was being re-checked, and "the
                # site did not answer" is the honest answer to a re-check.
                if not ground_on_failure:
                    raise
                log.warning("Could not read %s - %s. Falling back to search grounding.", text, exc)
                report.grounded = True
                fingerprint = ResultCache.key_for(text)
        else:
            fingerprint = ResultCache.key_for(text)
            report.grounded = len(text) < NAME_ONLY_CHARS

        cached = self._cached(fingerprint) if reuse else None
        if cached is not None:
            report.reused = True
            report.corrections = normalise(cached)
            self._check_freshness(cached)
            if is_url and self.cache:
                self.cache.watch(text)
            return cached, report

        if report.grounded:
            content = _grounded_prompt(text, is_url)
        elif seed is not None:
            content, report.pages_read = self._gather(seed)
        else:
            content = text

        result = self._extract(content, report.grounded)
        report.corrections = normalise(result)
        for correction in report.corrections:
            log.info("Corrected: %s", correction)
        report.changes = self._record(text, fingerprint, result)
        report.reworded = getattr(self, "_last_reworded", 0)
        self._check_freshness(result)

        if is_url and self.cache:
            self.cache.watch(text)

        return result, report

    # --- caching ---------------------------------------------------------
    def _cached(self, fingerprint: str) -> dict | None:
        if not self.cache:
            return None
        hit = self.cache.get(fingerprint)
        if hit is not None:
            log.info("Source unchanged since it was last read - reusing the extraction.")
        return hit

    def _record(self, raw_input: str, fingerprint: str, result: dict) -> list[tuple]:
        """Store the record and return what moved since the last version."""
        if not self.cache:
            return []

        pointer_key = ResultCache.key_for("pointer::" + raw_input)
        pointer = self.cache.get(pointer_key) or {}
        previous_fingerprint = pointer.get("fingerprint")

        changes: list[tuple] = []
        reworded = 0
        if previous_fingerprint and previous_fingerprint != fingerprint:
            previous = self.cache.get(previous_fingerprint)
            changes, reworded = _changes(previous or {}, result)
            for name, before, after in changes:
                log.warning("CHANGED %s: %s -> %s", name, _short(before), _short(after))
            if reworded:
                log.info("(%d descriptive field(s) reworded, not treated as a change)", reworded)

        self.cache.set(fingerprint, result)
        self.cache.set(pointer_key, {"fingerprint": fingerprint, "seen": date.today().isoformat()})
        self._last_reworded = reworded
        return changes

    def _check_freshness(self, result: dict) -> None:
        """A record can be faithful to its page and still be out of date."""
        closing = parse_date(result.get("closes_at"))
        if closing and closing < date.today():
            log.warning(
                "Deadline %s has already passed (today is %s) - this scheme is closed.",
                result.get("closes_at"), date.today().isoformat(),
            )

    # --- stage 2: agentic gathering --------------------------------------
    def _gather(self, seed) -> tuple[str, int]:
        gatherer = PageGatherer(seed.url)
        gatherer.pages[seed.url] = seed

        config = types.GenerateContentConfig(
            system_instruction=GATHER_INSTRUCTION,
            temperature=0.0,
            tools=[declarations()],
            automatic_function_calling=_NO_AFC,
        )
        contents = [types.Content(role="user", parts=[types.Part(text=_brief(seed))])]

        for step in range(1, gatherer.max_steps + 1):
            response = self.client.models.generate_content(
                model=self.model_name, contents=contents, config=config
            )
            calls = response.function_calls
            if not calls:
                log.info("Gathering done after %d step(s): the agent asked for nothing more.", step)
                break

            contents.append(response.candidates[0].content)
            parts = []
            for call in calls:
                args = dict(call.args or {})
                log.info("Agent -> %s(%s)", call.name, ", ".join(f"{k}={v!r}" for k, v in args.items())[:120])
                parts.append(
                    types.Part.from_function_response(
                        name=call.name, response=gatherer.dispatch(call.name, args)
                    )
                )
            contents.append(types.Content(role="user", parts=parts))

            if gatherer.finished_reason:
                log.info("Gathering done: %s", gatherer.finished_reason)
                break
            spent = gatherer.exhausted()
            if spent:
                log.info("Gathering stopped: %s", spent)
                break
        else:
            log.info("Gathering stopped: step budget reached (%d).", gatherer.max_steps)

        log.info("Extracting from %d page(s).", len(gatherer.pages))
        return gatherer.corpus(EXTRACT_CORPUS_CHARS), len(gatherer.pages)

    # --- stage 3: deterministic extraction -------------------------------
    def _extract(self, content: str, grounded: bool) -> dict:
        response = self.client.models.generate_content(
            model=self.model_name, contents=content, config=self._config(grounded)
        )
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, ScholarshipSchema):
            return parsed.model_dump(mode="json")
        return _loads(_text_of(response))

    def _config(self, grounded: bool) -> types.GenerateContentConfig:
        if not grounded:
            log.info("Extracting with %s (structured JSON mode)...", self.model_name)
            return types.GenerateContentConfig(
                system_instruction=_instruction(),
                temperature=TEMPERATURE,
                response_mime_type="application/json",
                response_schema=ScholarshipSchema,
                automatic_function_calling=_NO_AFC,
            )

        log.info("Extracting with %s (Google Search grounding)...", self.model_name)
        schema = json.dumps(ScholarshipSchema.model_json_schema(), indent=2)
        return types.GenerateContentConfig(
            system_instruction=(
                f"{_instruction()}\n"
                "Search the web for the scholarship, then reply with a single JSON "
                "object and nothing else - no prose, no code fences. It must validate "
                f"against this JSON Schema:\n{schema}"
            ),
            temperature=TEMPERATURE,
            tools=[types.Tool(google_search=types.GoogleSearch())],
            automatic_function_calling=_NO_AFC,
        )


def _grounded_prompt(text: str, is_url: bool) -> str:
    """What to say to a search-grounded extraction, which has no page in hand.

    The two ways of getting here are not the same question. A URL that would not
    fetch names the source exactly and the model's job is to reach it another
    way; a pasted name gives no address at all, and the work is finding which
    scheme is meant before reading anything about it. Telling it which case it
    is in costs one line and stops a search for the literal string.

    The paste is quoted rather than summarised into the instruction: it may
    carry a sponsor or a year that narrows the search, and a title that matches
    two schemes is a real thing - Post Matric, for one, exists separately for
    students with disabilities and for SC students, under different ministries.
    """
    if is_url:
        return f"Find and extract the scholarship published at {text}"
    return (
        "Find and extract the scholarship named below. Search for the sponsor's "
        "own page for it and use what that page states - dates, award amounts, "
        "eligibility, documents and how to apply. If the search does not settle "
        "a value, leave it null rather than filling it from what you remember of "
        "the scheme.\n\n"
        f"--- the scholarship, as it was given to us ---\n{text}"
    )


def _brief(seed) -> str:
    links = "\n".join(seed.links[:25]) or "(no links found on this page)"
    images = "\n".join(seed.images[:8]) or "(no images found on this page)"
    return (
        f"Source page: {seed.url}\n"
        f"Read via: {seed.tier}\n\n"
        f"--- page text (first 6000 of {len(seed.text):,} characters) ---\n"
        f"{seed.text[:6000]}\n\n"
        f"--- links available on this page ---\n{links}\n\n"
        f"--- images on this page, most likely to be the sponsor's mark first ---\n{images}\n"
    )


def _instruction() -> str:
    """The system prompt, anchored to today so the model cannot date an academic
    year from its training cutoff."""
    return f"Today's date is {date.today():%Y-%m-%d}.\n{SYSTEM_INSTRUCTION}"


def _changes(before: dict, after: dict) -> tuple[list[tuple[str, object, object]], int]:
    """Split a record diff into material changes and mere rewording.

    Returns (material changes, count of descriptive fields that drifted).
    """
    material: list[tuple[str, object, object]] = []
    reworded = 0

    for name in sorted(set(before) | set(after)):
        was, now = before.get(name), after.get(name)
        if was == now:
            continue
        if name in MATERIAL_FIELDS:
            material.append((name, was, now))
        elif name == "application_process":
            if _apply_urls(was) != _apply_urls(now):
                material.append(("application_links", _apply_urls(was), _apply_urls(now)))
            else:
                reworded += 1
        else:
            reworded += 1

    return material, reworded


def _apply_urls(process) -> list[str]:
    if not isinstance(process, dict):
        return []
    return sorted(
        link.get("url", "")
        for link in process.get("application_links") or []
        if isinstance(link, dict)
    )


def _short(value, limit: int = 90) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _text_of(response) -> str:
    """Pull the text off a response, explaining the blank ones."""
    text = getattr(response, "text", None)
    if text:
        return text

    feedback = getattr(response, "prompt_feedback", None)
    candidates = getattr(response, "candidates", None) or []
    reason = getattr(candidates[0], "finish_reason", None) if candidates else None
    raise ExtractionError(
        f"the model returned no text (finish_reason={reason}, prompt_feedback={feedback})"
    )


def _loads(text: str) -> dict:
    """Parse a JSON object out of a reply that may be fenced or padded with prose."""
    for candidate in (text, _search(_JSON_FENCE, text), _search(_JSON_OBJECT, text)):
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value

    raise ExtractionError(f"could not parse JSON from the reply: {text[:500]}")


def _search(pattern: re.Pattern, text: str) -> str | None:
    match = pattern.search(text)
    return match.group(1) if match and match.groups() else (match.group(0) if match else None)
