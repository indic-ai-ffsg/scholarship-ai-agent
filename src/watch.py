"""Re-check every watched scholarship and report what moved.

The sweep is deliberately lopsided: re-reading a page costs a fraction of a
second and nothing in API spend, so every URL is re-fetched and hashed, while
the model is only called for the pages that actually changed.
"""

import logging
import os
from dataclasses import dataclass, field
from datetime import date, timedelta

from src.agent import ScholarshipAgent
from src.normalise import parse_date
from src.scrapers import FetchError

log = logging.getLogger(__name__)

CLOSING_SOON_DAYS = int(os.getenv("SCHOLARSHIP_CLOSING_SOON_DAYS", "7"))


@dataclass
class Event:
    url: str
    status: str                  # open | closing_soon | closed | unknown | error
    changed: bool = False
    changes: list[tuple] = field(default_factory=list)
    name: str | None = None
    closes_at: str | None = None
    detail: str = ""

    def line(self) -> str:
        mark = {
            "closed": "CLOSED     ",
            "closing_soon": "CLOSING    ",
            "open": "open       ",
            "unknown": "no deadline",
            "error": "ERROR      ",
        }[self.status]
        changed = " [CHANGED]" if self.changed else ""
        return f"{mark}{changed} {self.name or self.url}\n            {self.detail}"


def refresh(agent: ScholarshipAgent, urls: list[str] | None = None) -> list[Event]:
    """Re-check each watched URL; extract again only where the source moved."""
    if urls is None:
        urls = agent.cache.watched() if agent.cache else []

    if not urls:
        log.info("Nothing is being watched yet - extract a URL first.")
        return []

    log.info("Re-checking %d watched page(s)...", len(urls))
    events = []
    for url in urls:
        events.append(_check(agent, url))
    return events


def _check(agent: ScholarshipAgent, url: str) -> Event:
    try:
        record, report = agent.run(url, ground_on_failure=False)
    except (FetchError, RuntimeError, ValueError) as exc:
        log.warning("Could not re-check %s - %s", url, exc)
        return Event(url=url, status="error", detail=str(exc)[:160])

    status, detail = _deadline_status(record.get("closes_at"))
    if report.changes:
        moved = ", ".join(name for name, _, _ in report.changes)
        detail = f"{detail}; changed: {moved}"
    elif report.reworded:
        detail = f"{detail}; re-extracted, no material change"

    return Event(
        url=url,
        status=status,
        changed=bool(report.changes),
        changes=report.changes,
        name=record.get("name"),
        closes_at=record.get("closes_at"),
        detail=detail,
    )


def _deadline_status(close_dates) -> tuple[str, str]:
    """Classify a record by its deadline relative to today."""
    today = date.today()
    closing = parse_date(close_dates)

    if closing is None:
        return "unknown", f"no parseable closing date ({close_dates!r})"
    if closing < today:
        return "closed", f"closed {(today - closing).days} day(s) ago ({close_dates})"
    if closing <= today + timedelta(days=CLOSING_SOON_DAYS):
        return "closing_soon", f"closes in {(closing - today).days} day(s) ({close_dates})"
    return "open", f"closes in {(closing - today).days} day(s) ({close_dates})"


def summarise(events: list[Event]) -> str:
    counts: dict[str, int] = {}
    for event in events:
        counts[event.status] = counts.get(event.status, 0) + 1
    changed = sum(1 for e in events if e.changed)
    parts = [f"{n} {status}" for status, n in sorted(counts.items())]
    return f"{len(events)} watched: " + ", ".join(parts) + f" | {changed} re-extracted"
