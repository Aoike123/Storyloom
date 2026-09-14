"""Small, dependency-free request guardrails for the anonymous public demo."""

import os
import time
from threading import Lock

from fastapi import Request


_SAFE_METHODS = {"GET", "HEAD"}
_WINDOW_SECONDS = 60
_MAX_BUCKETS = 10_000
_lock = Lock()
_window = -1
_counts: dict[tuple[str, str], int] = {}


def _configured_limit(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, 10_000))


def request_retry_after(request: Request) -> int | None:
    """Return a retry delay when this public-demo client exceeds its minute bucket."""
    if os.getenv("STORYLOOM_DEMO_MODE", "local").strip().lower() != "public":
        return None

    path = request.url.path
    if request.method == "OPTIONS" or path == "/api/health":
        return None
    if not (path == "/api" or path.startswith("/api/") or path == "/media" or path.startswith("/media/")):
        return None

    bucket_kind = "read" if request.method in _SAFE_METHODS else "write"
    limit = _configured_limit(
        "PUBLIC_READ_REQUESTS_PER_MINUTE" if bucket_kind == "read" else "PUBLIC_WRITE_REQUESTS_PER_MINUTE",
        180 if bucket_kind == "read" else 20,
    )
    client = request.client.host if request.client else "unknown"
    key = (str(client)[:128], bucket_kind)
    now = int(time.time())
    current_window = now // _WINDOW_SECONDS

    global _window
    with _lock:
        if current_window != _window:
            _window = current_window
            _counts.clear()
        count = _counts.get(key, 0)
        # Bound memory even if a botnet rotates source addresses within one minute.
        if count >= limit or (key not in _counts and len(_counts) >= _MAX_BUCKETS):
            return max(1, _WINDOW_SECONDS - now % _WINDOW_SECONDS)
        _counts[key] = count + 1
    return None


def clear_request_limit_state() -> None:
    """Clear in-memory counters; useful when reloading configuration and in tests."""
    global _window
    with _lock:
        _window = -1
        _counts.clear()
