"""Shared HTTP 429 back-off (v2.1.1).

One back-off state per key, shared by every task / feed in the process:
  ("public", endpoint)          unauthenticated availability polling (shared poll feeds)
  ("acct", user_id, endpoint)   authenticated calls with one OVH account's keys (carts, prebuilt carts, checkout prep)
On a 429 the wait is max(Retry-After, BASE * 2^(n-1)) with +/-20 % jitter, capped at MAX_S; any success resets it.
Callers check remaining() before calling OVH again, so there are no tight retry loops.
"""
from __future__ import annotations

import email.utils
import random
import threading
import time

BASE_S = 5.0
MAX_S = 300.0
_mu = threading.Lock()
_state: dict[tuple, tuple[float, int]] = {}   # key -> (until monotonic, consecutive 429s)


def parse_retry_after(value) -> float | None:
    """Retry-After is either delta-seconds or an HTTP-date."""
    if value is None or value == "":
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        dt = email.utils.parsedate_to_datetime(str(value))
        return max(0.0, dt.timestamp() - time.time())
    except Exception:
        return None


def _response(e):
    return getattr(e, "response", None)


def is_429(e) -> bool:
    """httpx/requests response, or an exception carrying one (python-ovh APIError keeps .response)."""
    r = e if hasattr(e, "status_code") and not isinstance(e, BaseException) else _response(e)
    if r is not None and getattr(r, "status_code", None) == 429:
        return True
    return isinstance(e, BaseException) and ("429" in str(e) or "too many requests" in str(e).lower())


def retry_after_of(e) -> float | None:
    r = e if hasattr(e, "headers") and not isinstance(e, BaseException) else _response(e)
    try:
        return parse_retry_after((r.headers or {}).get("Retry-After")) if r is not None else None
    except Exception:
        return None


def hit(key: tuple, retry_after: float | None = None) -> float:
    """Record a 429 for key; returns the wait in seconds."""
    with _mu:
        _, n = _state.get(key, (0.0, 0))
        n += 1
        expo = min(MAX_S, BASE_S * (2 ** (n - 1)))
        wait = max(retry_after or 0.0, expo) * random.uniform(0.8, 1.2)
        wait = min(MAX_S, max(1.0, wait))
        _state[key] = (time.monotonic() + wait, n)
        return wait


def remaining(key: tuple) -> float:
    with _mu:
        until, _ = _state.get(key, (0.0, 0))
    return max(0.0, until - time.monotonic())


def ok(key: tuple) -> None:
    with _mu:
        _state.pop(key, None)
