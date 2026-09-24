"""Re-check watched pages on a timer, and keep what the last sweep found.

`watch.refresh` already does the work and has since the beginning; what it never
had was anything to run it. It was reachable only as `python main.py --refresh`,
printed to stdout, and was forgotten the moment the terminal scrolled - so a
deadline that moved on a sponsor's page moved nowhere else. The SBI Asha page is
what made that concrete: its closing date went from 31 July to 19 September and
the platform went on showing the old one, because nobody happened to type the
command.

The sweep is cheap by construction and that is what makes a timer reasonable.
Every watched URL is re-fetched and hashed - a fraction of a second, no API
spend - and the model is called only for the pages whose text actually moved. A
nightly sweep over two hundred unchanged pages costs two hundred HTTP requests
and nothing else.

It stays OFF unless an interval is set. A service that starts spending model
credits because somebody ran `python server.py` is not a good default, and this
one binds to localhost where it may be nothing more than a developer's scratch
process. One variable turns it on.

What it deliberately does NOT do is write anything back. The Go API owns every
listing; this service owns reading pages. So a moved deadline becomes a FINDING
- readable at /api/refresh, shown in the panel - and a person decides whether
the listing changes. Automatic detection, reviewed application: the same
boundary the draft flow already draws, for the same reason.
"""

import logging
import os
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone

from src.agent import ScholarshipAgent
from src.watch import refresh, summarise

log = logging.getLogger(__name__)

# 0 - and anything unparseable - means "do not sweep".
ENV_INTERVAL = "DISCOVERY_REFRESH_HOURS"
MAX_CHANGE_CHARS = 120


def interval_hours() -> float:
    raw = os.getenv(ENV_INTERVAL, "0").strip()
    try:
        hours = float(raw)
    except ValueError:
        log.warning("%s is not a number (%r) - automatic re-checking is off.", ENV_INTERVAL, raw)
        return 0.0
    return hours if hours > 0 else 0.0


def _short(value) -> str:
    text = repr(value)
    return text if len(text) <= MAX_CHANGE_CHARS else text[: MAX_CHANGE_CHARS - 3] + "..."


class Sweeper:
    """Runs the sweep on an interval, and holds the last result for the panel."""

    def __init__(self) -> None:
        # Non-reentrant and never blocked on: a sweep that is already running is
        # a reason to decline a second one, not to queue it. Two sweeps over the
        # same watch list would re-fetch every page twice and race each other
        # into the cache.
        self._running = threading.Lock()
        self._state_lock = threading.Lock()
        self._state: dict = {
            "ran_at": None,
            "reason": None,
            "seconds": None,
            "summary": None,
            "watched": 0,
            "changed": 0,
            "findings": [],
            "error": None,
        }

    # --- reading ---------------------------------------------------------
    def last(self) -> dict:
        with self._state_lock:
            state = dict(self._state)
        state["interval_hours"] = interval_hours()
        state["running"] = self._running.locked()
        return state

    # --- running ---------------------------------------------------------
    def run_once(self, reason: str = "manual") -> bool:
        """Sweep now. False when one is already in flight."""
        if not self._running.acquire(blocking=False):
            log.info("A sweep is already running - not starting another.")
            return False
        try:
            self._sweep(reason)
        finally:
            self._running.release()
        return True

    def start_in_background(self, reason: str = "manual") -> bool:
        """Sweep on a worker thread, so an HTTP caller is not held open for it."""
        if self._running.locked():
            return False
        threading.Thread(
            target=self.run_once, args=(reason,), name="sweep", daemon=True
        ).start()
        return True

    def _sweep(self, reason: str) -> None:
        started = time.monotonic()
        try:
            agent = ScholarshipAgent()
        except ValueError as exc:                      # no key configured
            log.warning("Cannot sweep: %s", exc)
            self._store(reason, started, error=str(exc))
            return

        if agent.cache is None or not agent.cache.enabled:
            # The watch list lives in Redis. Without it there is nothing to
            # sweep - not an error, just a service that was never going to have
            # anything to re-check.
            log.info("No Redis, so nothing is watched - skipping the sweep.")
            self._store(reason, started, error="no cache, so nothing is watched")
            return

        try:
            events = refresh(agent)
        except Exception as exc:                        # a sweep must not kill the thread
            log.exception("Sweep failed")
            self._store(reason, started, error=f"{type(exc).__name__}: {exc}"[:300])
            return

        findings = []
        for event in events:
            row = asdict(event)
            # The before/after of a change can be a whole who_qualifies object.
            # The panel wants to know WHAT moved, and opens the draft to see the
            # detail, so the values are shortened here rather than shipped whole.
            row["changes"] = [
                {"field": name, "was": _short(was), "now": _short(now)}
                for name, was, now in event.changes
            ]
            findings.append(row)

        changed = sum(1 for e in events if e.changed)
        if changed:
            log.info("Sweep: %d of %d watched page(s) moved.", changed, len(events))
        self._store(
            reason, started,
            summary=summarise(events) if events else "nothing is being watched",
            watched=len(events),
            changed=changed,
            findings=findings,
        )

    def _store(self, reason: str, started: float, **fields) -> None:
        with self._state_lock:
            self._state = {
                "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "reason": reason,
                "seconds": round(time.monotonic() - started, 1),
                "summary": None,
                "watched": 0,
                "changed": 0,
                "findings": [],
                "error": None,
                **fields,
            }

    # --- the timer -------------------------------------------------------
    def start(self) -> float:
        """Start the interval thread. Returns the interval, or 0 when off."""
        hours = interval_hours()
        if not hours:
            return 0.0
        threading.Thread(target=self._loop, args=(hours,), name="sweeper", daemon=True).start()
        return hours

    def _loop(self, hours: float) -> None:
        # Sleeps first. Sweeping on startup sounds helpful and is the wrong
        # shape: a service that is restarting - a crash loop, a deploy, somebody
        # iterating on the config - would sweep every time it came up, which is
        # the one situation where the extra spend is least wanted.
        seconds = hours * 3600
        while True:
            time.sleep(seconds)
            self.run_once(reason="scheduled")


sweeper = Sweeper()
