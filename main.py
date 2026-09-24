"""CLI for the scholarship extractor.

    python main.py <URL or text>     extract one scholarship (URLs are watched)
    python main.py --refresh         re-check every watched page, report changes
    python main.py --list            show what is being watched
    python main.py --unwatch <URL>   stop watching a page
"""

import argparse
import dataclasses
import json
import logging
import sys

from dotenv import load_dotenv
from google.genai import errors as genai_errors

from src.agent import ScholarshipAgent
from src.watch import refresh, summarise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Extract structured scholarship records from a URL or pasted text.",
    )
    parser.add_argument("input", nargs="*", help="URL or raw text to extract")
    parser.add_argument("--refresh", action="store_true", help="re-check all watched pages")
    parser.add_argument("--list", dest="list_watched", action="store_true", help="list watched pages")
    parser.add_argument("--unwatch", metavar="URL", help="stop watching a page")
    parser.add_argument("--no-cache", action="store_true", help="ignore Redis for this run")
    parser.add_argument("--json", action="store_true", help="machine-readable output for --refresh")
    return parser


def read_input(args: list[str]) -> str:
    """Take the input from the argv, a pipe, or an interactive prompt."""
    if args:
        return " ".join(args).strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    try:
        return input("Enter a scholarship URL or paste text, then press Enter: ").strip()
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return ""


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(level=logging.WARNING, format="%(message)s", stream=sys.stderr)
    logging.getLogger("src").setLevel(logging.INFO)
    for chatty in ("httpx", "httpcore", "google_genai"):
        logging.getLogger(chatty).setLevel(logging.WARNING)

    opts = build_parser().parse_args(sys.argv[1:] if argv is None else argv)

    try:
        agent = ScholarshipAgent(use_cache=not opts.no_cache)

        if opts.list_watched:
            return _list(agent)
        if opts.unwatch:
            return _unwatch(agent, opts.unwatch)
        if opts.refresh:
            return _refresh(agent, as_json=opts.json)

        user_input = read_input(opts.input)
        if not user_input:
            print("Nothing to extract - no input given.", file=sys.stderr)
            return 1
        record = agent.process(user_input)
    except (ValueError, RuntimeError, genai_errors.APIError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130

    print(json.dumps(record, indent=2, ensure_ascii=False))
    return 0


def _list(agent: ScholarshipAgent) -> int:
    urls = agent.cache.watched() if agent.cache else []
    if not urls:
        print("Nothing is being watched yet.", file=sys.stderr)
        return 0
    for url in urls:
        print(url)
    return 0


def _unwatch(agent: ScholarshipAgent, url: str) -> int:
    if agent.cache and agent.cache.unwatch(url):
        print(f"No longer watching {url}", file=sys.stderr)
        return 0
    print(f"Not watching {url}", file=sys.stderr)
    return 1


def _refresh(agent: ScholarshipAgent, as_json: bool) -> int:
    events = refresh(agent)
    if as_json:
        print(json.dumps([dataclasses.asdict(e) for e in events], indent=2, ensure_ascii=False))
        return 0
    for event in events:
        print(event.line())
    if events:
        print(f"\n{summarise(events)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
