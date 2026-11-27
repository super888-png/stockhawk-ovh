"""Cross-task / cross-process order lock (v2.1.1).

Only one task at a time may run the cart -> price check -> checkout section for the same OVH account
(user + API endpoint) and the same plan. Uses fcntl.flock on a file under DATA_DIR/locks: flock locks belong to
the open file description, so they exclude other tasks in the same process (each acquire opens the file anew)
as well as other processes (a second panel / CLI on the same data directory). The kernel drops the lock if the
process dies, so a crash can never leave a stale lock behind.
"""
from __future__ import annotations

import fcntl
import hashlib
import os

from .config import DATA_DIR


class OrderLock:
    def __init__(self, *parts) -> None:
        key = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:32]
        d = os.path.join(DATA_DIR, "locks")
        os.makedirs(d, exist_ok=True)
        self.path = os.path.join(d, f"order-{key}.lock")
        self._f = None

    def acquire(self) -> bool:
        """Non-blocking. False => another task is ordering this plan on this account right now."""
        f = open(self.path, "a+")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self._f = f
        return True

    def release(self) -> None:
        f, self._f = self._f, None
        if f is not None:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            finally:
                f.close()

    @property
    def held(self) -> bool:
        return self._f is not None


def order_lock_for(user_id: int, endpoint: str, plan_code: str) -> OrderLock:
    return OrderLock("order", user_id, endpoint, plan_code)
