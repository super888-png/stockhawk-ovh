"""Shared availability poller.

All tasks (and live-stock page viewers) watching the same (endpoint, planCode) share ONE
poll loop, run at the fastest interval requested by its subscribers and never faster than
MIN_POLL_INTERVAL. So 50 users sniping RISE-1 cost OVH one request per interval.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from .config import MIN_POLL_INTERVAL
from . import ovh_engine as E
from . import ratelimit as RL
from .i18n import T, tr

log = logging.getLogger("poller")
VIEWER_INTERVAL = max(MIN_POLL_INTERVAL, 10.0)   # live-stock page refresh rate per plan
VIEWER_TTL = 45.0                                # viewer subscription expires without a refresh
MAX_FEEDS = 300                                  # hard cap on concurrent plan feeds


class Feed:
    def __init__(self, hub: "PollHub", endpoint: str, plan: str):
        self.hub, self.endpoint, self.plan = hub, endpoint, plan
        self.subs: dict[int, tuple[float, asyncio.Event]] = {}  # task id -> (interval, wake event)
        self.viewers_until = 0.0
        self.data: list[dict] | None = None
        self.error: str | None = None
        self.version = 0
        self.last_ok = 0.0
        self.last_ms = 0
        self.task: asyncio.Task | None = None

    def alive(self) -> bool:
        return bool(self.subs) or time.time() < self.viewers_until

    def interval(self) -> float:
        cands = [iv for iv, _ in self.subs.values()]
        if time.time() < self.viewers_until:
            cands.append(VIEWER_INTERVAL)
        return max(MIN_POLL_INTERVAL, min(cands) if cands else VIEWER_INTERVAL)

    async def run(self):
        url = f"{E.ENDPOINTS[self.endpoint]}/dedicated/server/datacenter/availabilities"
        client = self.hub.client(self.endpoint)
        while self.alive():
            rl_key = ("public", self.endpoint)
            wait = RL.remaining(rl_key)          # shared by every feed of this endpoint
            if wait > 0:
                self.error = T("OVH 限流 429，等待 {0:.0f}s", wait)
                await asyncio.sleep(min(wait, 30.0))
                continue
            t0 = time.monotonic()
            try:
                r = await client.get(url, params={"planCode": self.plan})
                if r.status_code == 429:
                    wait = RL.hit(rl_key, RL.retry_after_of(r))   # Retry-After + exponential back-off with jitter
                    self.error = T("OVH 限流 429，等待 {0:.0f}s", wait)
                    log.warning("429 from %s (%s), backing off %.0fs", self.endpoint, self.plan, wait)
                    continue
                r.raise_for_status()
                RL.ok(rl_key)
                self.data, self.error, self.last_ok = r.json(), None, time.time()
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
            self.last_ms = int((time.monotonic() - t0) * 1000)
            self.hub.requests += 1
            self.version += 1
            for _, ev in list(self.subs.values()):
                ev.set()
            await asyncio.sleep(max(0.0, self.interval() - (time.monotonic() - t0)))
        if self.hub.feeds.get((self.endpoint, self.plan)) is self:
            self.hub.feeds.pop((self.endpoint, self.plan), None)


class PollHub:
    def __init__(self):
        self.feeds: dict[tuple, Feed] = {}
        self._clients: dict[str, httpx.AsyncClient] = {}
        self.requests = 0

    def client(self, endpoint: str) -> httpx.AsyncClient:
        c = self._clients.get(endpoint)
        if c is None:
            c = httpx.AsyncClient(timeout=httpx.Timeout(8, connect=4), headers={"User-Agent": "ovh-sniper-web/1.1"},
                                  limits=httpx.Limits(max_keepalive_connections=20, max_connections=50))
            self._clients[endpoint] = c
        return c

    def _feed(self, endpoint: str, plan: str) -> Feed:
        key = (endpoint, plan)
        feed = self.feeds.get(key)
        if feed is None or (feed.task is not None and feed.task.done()):
            if len(self.feeds) >= MAX_FEEDS:
                raise RuntimeError(T("同时监控的型号过多，请稍后再试"))
            feed = Feed(self, endpoint, plan)
            self.feeds[key] = feed
        return feed

    def _ensure_running(self, feed: Feed) -> None:
        if feed.task is None or feed.task.done():
            feed.task = asyncio.create_task(feed.run(), name=f"feed:{feed.endpoint}:{feed.plan}")

    def subscribe(self, sid: int, endpoint: str, plan: str, interval: float, event: asyncio.Event) -> Feed:
        feed = self._feed(endpoint, plan)
        feed.subs[sid] = (max(MIN_POLL_INTERVAL, interval), event)
        self._ensure_running(feed)
        return feed

    def unsubscribe(self, sid: int, endpoint: str, plan: str) -> None:
        feed = self.feeds.get((endpoint, plan))
        if feed:
            feed.subs.pop(sid, None)

    def watch(self, endpoint: str, plan: str) -> Feed:
        """Viewer subscription for the live-stock page; expires VIEWER_TTL s after the last call."""
        feed = self._feed(endpoint, plan)
        feed.viewers_until = time.time() + VIEWER_TTL
        self._ensure_running(feed)
        return feed

    def stats(self) -> list[dict]:
        return [{"endpoint": f.endpoint, "plan": f.plan, "subscribers": len(f.subs),
                 "viewers": time.time() < f.viewers_until, "interval": f.interval(),
                 "polls": f.version, "last_ms": f.last_ms, "error": f.error} for f in self.feeds.values()]

    async def close(self):
        for c in self._clients.values():
            await c.aclose()


hub = PollHub()


async def fetch_once(endpoint: str, plan: str) -> list[dict]:
    """One-off availability fetch for the UI; reuses live feed data when fresh."""
    feed = hub.feeds.get((endpoint, plan))
    if feed and feed.data is not None and time.time() - feed.last_ok < max(10, feed.interval() * 2):
        return feed.data
    r = await hub.client(endpoint).get(f"{E.ENDPOINTS[endpoint]}/dedicated/server/datacenter/availabilities",
                                       params={"planCode": plan})
    r.raise_for_status()
    return r.json()
