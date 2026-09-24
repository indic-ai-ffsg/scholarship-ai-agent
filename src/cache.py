"""Redis-backed cache of finished extractions.

Redis is an optimisation, not a dependency: if it is not reachable the cache
turns itself off once, says so once, and every call becomes a no-op.
"""

import hashlib
import json
import logging
import os

import redis

from src.schema import SCHEMA_VERSION

log = logging.getLogger(__name__)

KEY_PREFIX = "scholarship_cache:"
WATCH_PREFIX = "scholarship_watch:"
TTL_SECONDS = 7 * 24 * 60 * 60  # a week
CONNECT_TIMEOUT = 2.0


class ResultCache:
    def __init__(self, ttl: int = TTL_SECONDS):
        self.ttl = ttl
        self._client = _connect()

    @property
    def enabled(self) -> bool:
        return self._client is not None

    @staticmethod
    def key_for(text: str) -> str:
        """The key a source's record is stored under.

        The record's shape is part of it. Without that, changing schema.py
        serves yesterday's record - extracted against fields that no longer
        exist - to a panel that now expects the new ones, and the miss shows up
        as blank boxes in a form rather than as a cache that needs clearing.
        """
        digest = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
        return f"{KEY_PREFIX}v{SCHEMA_VERSION}:{digest}"

    def get(self, key: str) -> dict | None:
        if self._client is None:
            return None
        try:
            raw = self._client.get(key)
        except redis.RedisError as exc:
            self._disable(f"read failed: {exc}")
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Discarding a corrupt cache entry for %s.", key)
            self.delete(key)
            return None

    def set(self, key: str, value: dict) -> None:
        if self._client is None:
            return
        try:
            self._client.setex(key, self.ttl, json.dumps(value, ensure_ascii=False))
        except redis.RedisError as exc:
            self._disable(f"write failed: {exc}")

    def delete(self, key: str) -> None:
        if self._client is None:
            return
        try:
            self._client.delete(key)
        except redis.RedisError:
            pass

    # --- watch list -------------------------------------------------------
    def watch(self, url: str) -> None:
        if self._client is None:
            return
        key = WATCH_PREFIX + hashlib.sha256(url.encode("utf-8")).hexdigest()
        try:
            if not self._client.exists(key):
                self._client.set(
                    key,
                    json.dumps({"url": url, "added": _today()}),
                )
        except redis.RedisError as exc:
            self._disable(f"watch write failed: {exc}")

    def watched(self) -> list[str]:
        if self._client is None:
            return []
        try:
            urls = []
            for key in self._client.scan_iter(match=WATCH_PREFIX + "*", count=100):
                raw = self._client.get(key)
                if raw:
                    try:
                        urls.append(json.loads(raw)["url"])
                    except (json.JSONDecodeError, KeyError):
                        continue
            return sorted(urls)
        except redis.RedisError as exc:
            self._disable(f"watch read failed: {exc}")
            return []

    def unwatch(self, url: str) -> bool:
        if self._client is None:
            return False
        key = WATCH_PREFIX + hashlib.sha256(url.encode("utf-8")).hexdigest()
        try:
            return bool(self._client.delete(key))
        except redis.RedisError as exc:
            self._disable(f"watch delete failed: {exc}")
            return False

    def _disable(self, reason: str) -> None:
        log.warning("Redis %s - continuing without a cache.", reason)
        self._client = None


def _today() -> str:
    from datetime import date

    return date.today().isoformat()


def _connect() -> redis.Redis | None:
    """Return a live client, or None if Redis is not reachable."""
    url = os.getenv("REDIS_URL")
    host = os.getenv("REDIS_HOST", "localhost")
    port = os.getenv("REDIS_PORT", "6379")

    # REDIS_URL carries the password, so describe the target - never log it.
    target = "the server in REDIS_URL" if url else f"{host}:{port}"
    hint = (
        "Check that REDIS_URL is current and the instance is awake."
        if url
        else "Start a local server with: brew services start redis"
    )

    try:
        if url:
            client = redis.from_url(
                url, decode_responses=True, socket_connect_timeout=CONNECT_TIMEOUT
            )
        else:
            client = redis.Redis(
                host=host,
                port=int(port),
                db=0,
                decode_responses=True,
                socket_connect_timeout=CONNECT_TIMEOUT,
            )
        client.ping()
        return client
    except (redis.RedisError, OSError, ValueError) as exc:
        log.warning(
            "Redis (%s) is not reachable (%s) - results will not be cached. %s",
            target,
            exc,
            hint,
        )
        return None
