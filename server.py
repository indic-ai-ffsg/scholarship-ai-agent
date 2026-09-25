"""The discovery service: extraction over HTTP, for the admin panel to drive.

One of two services behind the panel. The Go API owns scholarships, students and
everything with a row in Postgres; this one owns reading a sponsor's page and
turning it into a record, and owns no data at all - its Redis holds extractions
keyed by the hash of the page they came from, which can be thrown away at any
time and re-earned by re-reading the page.

It serves JSON and nothing else. The panel is the only client, and it reaches
these routes at `/discovery/api/...` through its own origin - vite's proxy in
development, nginx in the image - for the same reason it proxies the Go API:
one origin, no CORS, no credentials dance. The prefix is stripped below rather
than rewritten by either proxy, so the service works the same whether it is
called directly or mounted under a path.

Progress is why a run gets an id and a stream rather than being a plain POST.
One extraction can read five pages and take a minute or two, and a spinner that
says nothing for that long is indistinguishable from a hang. The agent already
narrates itself through `logging` - which page it read, which tool the model
called, what moved since last time - so the panel reads those same lines from
`/api/runs/<id>/events` as they happen, and the agent never learns anything is
watching.

Stdlib only, and bound to localhost by default: it fetches whatever URL it is
handed, which is not a thing to expose on a network.
"""

import argparse
import base64
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from google.genai import errors as genai_errors

import requests

from src.agent import DEFAULT_MODEL, ScholarshipAgent
from src.cache import ResultCache
from src.schema import SCHEMA_VERSION, ScholarshipSchema
from src.sweeper import sweeper

log = logging.getLogger(__name__)
PATH_PREFIX = os.getenv("DISCOVERY_PATH_PREFIX", "/discovery").rstrip("/")

VERIFY_URL = os.getenv("DISCOVERY_VERIFY_URL", "").strip()
VERIFY_TIMEOUT = 6.0

ALLOWED_ORIGINS = {
    o.strip() for o in os.getenv("DISCOVERY_ALLOWED_ORIGINS", "").split(",") if o.strip()
}

MAX_ITEMS = 25
MAX_BODY_BYTES = 1 << 20
LOGO_TYPES = ("image/png", "image/jpeg")
MAX_LOGO_BYTES = 5 * 1024 * 1024
LOGO_TIMEOUT = 10.0
PING_SECONDS = 15.0
JOB_RETENTION = 20

_RUN_EVENTS = re.compile(r"^/api/runs/([0-9a-f]{6,32})/events$")
_RUN_CANCEL = re.compile(r"^/api/runs/([0-9a-f]{6,32})/cancel$")

_cache_status = "unknown"

_SCHEMA = ScholarshipSchema.model_json_schema()


class Job:
    """One run over the list, and the event log the browser replays from."""

    def __init__(self, items: list[str], use_cache: bool):
        self.id = uuid.uuid4().hex[:12]
        self.items = items
        self.use_cache = use_cache
        self.created = time.time()
        self.cancelled = threading.Event()
        self.closed = False
        self.history: list[dict] = []
        self._cv = threading.Condition()

    def emit(self, kind: str, **fields) -> None:
        with self._cv:
            self.history.append({"type": kind, **fields})
            if kind == "done":
                self.closed = True
            self._cv.notify_all()

    def stream(self, start: int = 0):
        """Yield (id, event) from `start` on, or (None, None) as a keepalive.

        Nothing is yielded while the lock is held, because a slow reader must
        not be able to stall the worker that is producing the events.
        """
        index = max(0, start)
        while True:
            with self._cv:
                while index >= len(self.history) and not self.closed:
                    if not self._cv.wait(timeout=PING_SECONDS):
                        break
                batch = self.history[index:]
                first = index
                index += len(batch)
                finished = self.closed and index >= len(self.history)

            if batch:
                for offset, event in enumerate(batch):
                    yield first + offset, event
            else:
                yield None, None
            if finished:
                return

_routes: dict[int, Job] = {}


class StreamLogHandler(logging.Handler):
    """Forward a worker thread's log records to the job running on it."""

    def emit(self, record: logging.LogRecord) -> None:
        job = _routes.get(threading.get_ident())
        if job is None:
            return
        try:
            message = record.getMessage()
        except Exception:
            return
        job.emit("log", level=record.levelname.lower(), message=message[:500])


jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()


def start_job(items: list[str], use_cache: bool) -> Job:
    job = Job(items, use_cache)
    with _jobs_lock:
        jobs[job.id] = job
        for stale in sorted(jobs.values(), key=lambda j: j.created)[:-JOB_RETENTION]:
            jobs.pop(stale.id, None)
    threading.Thread(target=_work, args=(job,), name=f"run-{job.id}", daemon=True).start()
    return job


def _work(job: Job) -> None:
    _routes[threading.get_ident()] = job
    try:
        try:
            agent = ScholarshipAgent(use_cache=True)
        except ValueError as exc:
            job.emit("fatal", message=str(exc))
            return

        for index, item in enumerate(job.items):
            if job.cancelled.is_set():
                job.emit("item_skipped", index=index)
                continue

            job.emit("item_start", index=index)
            started = time.time()
            try:
                record, report = agent.run(item, reuse=job.use_cache)
            except (ValueError, RuntimeError, genai_errors.APIError) as exc:
                job.emit(
                    "item_error", index=index,
                    message=_first_line(exc), seconds=round(time.time() - started, 1),
                )
                continue
            except Exception as exc:
                log.exception("Unexpected failure extracting %s", item)
                job.emit(
                    "item_error", index=index,
                    message=f"unexpected {type(exc).__name__}: {_first_line(exc)}",
                    seconds=round(time.time() - started, 1),
                )
                continue

            job.emit(
                "item_done", index=index, record=record,
                seconds=round(time.time() - started, 1),
                report={
                    "reused": report.reused,
                    "grounded": report.grounded,
                    "pages_read": report.pages_read,
                    "reworded": report.reworded,
                    "changes": [name for name, _, _ in report.changes],
                    "corrections": report.corrections,
                },
            )
    finally:
        _routes.pop(threading.get_ident(), None)
        job.emit("done", cancelled=job.cancelled.is_set())


def _first_line(exc: Exception) -> str:
    text = str(exc).strip()
    return (text.splitlines()[0] if text else type(exc).__name__)[:300]


def _clean(items) -> list[str]:
    if not isinstance(items, list):
        return []
    cleaned = [item.strip() for item in items if isinstance(item, str) and item.strip()]
    return cleaned[:MAX_ITEMS]


def health() -> dict:
    key = bool(os.getenv("LLM_API_KEY"))
    return {
        "ok": key,
        "model": os.getenv("MODEL", DEFAULT_MODEL),
        "cache": _cache_status,
        "max_items": MAX_ITEMS,
        "schema_version": SCHEMA_VERSION,
        "refresh_hours": sweeper.last()["interval_hours"],
        "reason": None if key else
                  "LLM_API_KEY is not set - copy .env.example to .env and add your Gemini key.",
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "scholarship-discovery"

    def do_GET(self) -> None:
        path = self._route()
        if path == "/":
            return self._json({
                "service": "scholarship discovery",
                "ui": "the admin panel, under Discovery",
                "routes": [
                    "/api/health", "/api/schema", "/api/runs",
                    "/api/runs/<id>/events", "/api/refresh",
                ],
            })
        if path == "/api/health":
            return self._json(health())
        if path == "/api/refresh":
            return self._json(sweeper.last())
        if path == "/api/schema":
            return self._json(_SCHEMA)
        if path == "/api/logo":
            return self._logo()
        match = _RUN_EVENTS.match(path)
        if match:
            return self._events(match.group(1))
        self._json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        path = self._route()
        if not self._permitted():
            return self._json(
                {"error": "Not signed in to the platform, or not allowed to author listings."},
                status=401,
            )
        if path == "/api/runs":
            return self._create_run()
        if path == "/api/refresh":
            started = sweeper.start_in_background(reason="asked for")
            return self._json({"started": started, "running": True}, status=202)
        match = _RUN_CANCEL.match(path)
        if match:
            job = jobs.get(match.group(1))
            if job is None:
                return self._json({"error": "unknown run"}, status=404)
            job.cancelled.set()
            return self._json({"ok": True})
        self._json({"error": "not found"}, status=404)

    def do_OPTIONS(self) -> None:
        """Preflight, for a cross-origin caller that has been allowed one."""
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Last-Event-ID")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _permitted(self) -> bool:
        """Whether the caller may spend anything. See VERIFY_URL."""
        if not VERIFY_URL:
            return True

        token = self.headers.get("Authorization", "")
        if not token.startswith("Bearer "):
            return False

        request = urllib.request.Request(VERIFY_URL, headers={
            "Authorization": token,
            "Accept": "application/json",
            "User-Agent": "scholarship-discovery/1.0 (+admin run verification)",
        })
        try:
            with urllib.request.urlopen(request, timeout=VERIFY_TIMEOUT) as answer:
                return 200 <= answer.status < 300
        except urllib.error.HTTPError as exc:
            log.warning("Run refused: %s answered %s.", VERIFY_URL, exc.code)
            return False
        except (urllib.error.URLError, OSError) as exc:
            log.warning("Could not check the caller with %s (%s) - refusing.", VERIFY_URL, exc)
            return False

    def _route(self) -> str:
        """The path with the panel's mount point taken off, if it is there."""
        path = urlparse(self.path).path
        if PATH_PREFIX and (path == PATH_PREFIX or path.startswith(PATH_PREFIX + "/")):
            path = path[len(PATH_PREFIX):] or "/"
        return path

    def _cors(self) -> None:
        origin = self.headers.get("Origin", "")
        if origin and origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    # --- routes ----------------------------------------------------------
    def _create_run(self) -> None:
        body = self._body()
        if body is None:
            return
        items = _clean(body.get("items"))
        if not items:
            return self._json({"error": "No input: add at least one URL or a block of text."},
                              status=400)
        job = start_job(items, use_cache=bool(body.get("use_cache", True)))
        self._json({"job_id": job.id, "count": len(items)}, status=202)

    def _logo(self) -> None:
        """Fetch a sponsor's mark, so the panel can attach it to a draft.

        The browser can SHOW a cross-origin image and cannot read its bytes -
        no sponsor sets CORS headers for the benefit of an admin panel - so the
        one thing standing between a logo on a page and a logo on a listing is
        somebody downloading it by hand. This does that download.

        It is not a general proxy and is kept narrow on purpose: an operator who
        may start a run can already make this service fetch any URL, so nothing
        new is reachable, but only a PNG or JPEG under 5 MB comes back, and it
        comes back as data rather than as a stream to follow.
        """
        if not self._permitted():
            return self._json({"error": "Not signed in to the platform."}, status=401)

        url = (parse_qs(urlparse(self.path).query).get("url") or [""])[0].strip()
        if not url.lower().startswith(("http://", "https://")):
            return self._json({"error": "That is not a web address."}, status=400)

        try:
            with requests.get(
                url, timeout=LOGO_TIMEOUT, stream=True,
                headers={"User-Agent": "scholarship-discovery/1.0"},
            ) as answer:
                answer.raise_for_status()
                kind = answer.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if kind not in LOGO_TYPES:
                    return self._json(
                        {"error": f"That is {kind or 'not an image'}; the listing takes a PNG or a JPEG."},
                        status=415,
                    )

                body = bytearray()
                for chunk in answer.iter_content(64 * 1024):
                    body.extend(chunk)
                    if len(body) > MAX_LOGO_BYTES:
                        return self._json(
                            {"error": "That image is larger than the 5 MB a listing takes."},
                            status=413,
                        )
        except requests.RequestException as exc:
            return self._json({"error": f"The image could not be fetched ({_first_line(exc)})."},
                              status=502)

        log.info("Fetched a logo from %s (%s, %d bytes)", url, kind, len(body))
        self._json({
            "content_type": kind,
            "bytes": len(body),
            "data_url": f"data:{kind};base64,{base64.b64encode(bytes(body)).decode('ascii')}",
        })

    def _events(self, job_id: str) -> None:
        job = jobs.get(job_id)
        if job is None:
            return self._json({"error": "unknown run"}, status=404)

        resume = self.headers.get("Last-Event-ID", "")
        start = int(resume) + 1 if resume.isdigit() else 0

        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self._cors()
        self.end_headers()

        try:
            for index, event in job.stream(start):
                if event is None:
                    chunk = b": ping\n\n"
                else:
                    payload = json.dumps(event, ensure_ascii=False, default=str)
                    chunk = f"id: {index}\ndata: {payload}\n\n".encode("utf-8")
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    # --- plumbing --------------------------------------------------------
    def _body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._json({"error": "request body missing or too large"}, status=413)
            return None
        try:
            parsed = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json({"error": "body was not JSON"}, status=400)
            return None
        if not isinstance(parsed, dict):
            self._json({"error": "body was not a JSON object"}, status=400)
            return None
        return parsed

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", status)

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt: str, *args) -> None:
        """Quiet by default - the agent's own log is the interesting one."""
        log.debug("%s - %s", self.address_string(), fmt % args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="server.py",
        description="The discovery service the admin panel calls: extraction over HTTP.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind (default: localhost)")
    parser.add_argument("--port", type=int, default=8765, help="port to bind (default: 8765)")
    return parser


def main(argv: list[str] | None = None) -> int:
    global _cache_status

    load_dotenv()
    logging.basicConfig(level=logging.WARNING, format="%(message)s", stream=sys.stderr)
    logging.getLogger("src").setLevel(logging.INFO)
    logging.getLogger("src").addHandler(StreamLogHandler())
    for chatty in ("httpx", "httpcore", "google_genai"):
        logging.getLogger(chatty).setLevel(logging.WARNING)

    opts = build_parser().parse_args(sys.argv[1:] if argv is None else argv)

    if not os.getenv("LLM_API_KEY"):
        print("LLM_API_KEY is not set - the page will load, but a run will fail.", file=sys.stderr)
    _cache_status = "redis" if ResultCache().enabled else "off"

    hours = sweeper.start()
    print(
        f"Re-checking watched pages every {hours:g}h"
        if hours else
        "Automatic re-checking is off (set DISCOVERY_REFRESH_HOURS to turn it on).",
        file=sys.stderr,
    )

    try:
        httpd = ThreadingHTTPServer((opts.host, opts.port), Handler)
    except OSError as exc:
        print(f"Could not bind {opts.host}:{opts.port} - {exc}", file=sys.stderr)
        print(f"Something else is probably on that port; try --port {opts.port + 1}.", file=sys.stderr)
        return 1
    httpd.daemon_threads = True

    print(
        f"Discovery service on http://{opts.host}:{opts.port}{PATH_PREFIX or ''}"
        "  (the screen is in the admin panel, under Discovery)",
        file=sys.stderr,
    )
    if ALLOWED_ORIGINS:
        print(f"Cross-origin calls allowed from: {', '.join(sorted(ALLOWED_ORIGINS))}", file=sys.stderr)
    print(
        f"Runs are checked against {VERIFY_URL}" if VERIFY_URL
        else "Runs are NOT checked against the API - anybody who can reach this can spend on it.",
        file=sys.stderr,
    )

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
