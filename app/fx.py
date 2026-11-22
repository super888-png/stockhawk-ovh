"""Live exchange rates to CNY from free public sources, cached ~1h (memory + disk), with fallbacks."""
from __future__ import annotations

import json
import logging
import os
import threading
import time

import httpx

from .config import CACHE_DIR
from .i18n import T, tr

log = logging.getLogger("fx")
TTL = 3600
FAIL_RETRY = 300          # after all sources failed, retry at most every 5 min
PATH = os.path.join(CACHE_DIR, "fx_cny.json")
# last-resort built-in rates (approximate; shown as 内置估算)
FALLBACK = {"EUR": 7.6, "USD": 6.9, "CAD": 4.9, "GBP": 8.8, "PLN": 1.8, "AUD": 4.4, "SGD": 5.2, "INR": 0.08, "CNY": 1.0}
_lock = threading.Lock()
_state: dict[str, dict] = {}   # base -> {"rate", "source", "fetched", "tried"}


def _er_api(base):
    r = httpx.get(f"https://open.er-api.com/v6/latest/{base}", timeout=8)
    r.raise_for_status()
    d = r.json()
    if d.get("result") != "success":
        raise ValueError("er-api: " + str(d.get("error-type")))
    return float(d["rates"]["CNY"]), "open.er-api.com"


def _frankfurter(base):
    r = httpx.get("https://api.frankfurter.dev/v1/latest", params={"base": base, "symbols": "CNY"}, timeout=8,
                  follow_redirects=True)
    r.raise_for_status()
    return float(r.json()["rates"]["CNY"]), "frankfurter (ECB)"


def _jsdelivr(base):
    b = base.lower()
    r = httpx.get(f"https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/{b}.json", timeout=8)
    r.raise_for_status()
    return float(r.json()[b]["cny"]), "currency-api (jsDelivr)"


SOURCES = (_er_api, _frankfurter, _jsdelivr)


def _load_disk():
    try:
        with open(PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_disk():
    try:
        tmp = PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({k: {x: v[x] for x in ("rate", "source", "fetched")} for k, v in _state.items() if v.get("fetched")}, f)
        os.replace(tmp, PATH)
    except Exception:
        pass


def rate(base: str = "EUR") -> dict:
    """{'base','rate','source','fetched' (epoch|None),'live': bool}. Never raises, never blocks > ~25s."""
    base = (base or "EUR").upper()[:3]
    if base == "CNY":
        return {"base": "CNY", "rate": 1.0, "source": "-", "fetched": time.time(), "live": True}
    with _lock:
        if not _state:
            _state.update({k: dict(v) for k, v in _load_disk().items()})
        cur = _state.get(base) or {}
        now = time.time()
        fresh = cur.get("fetched") and now - cur["fetched"] < TTL
        if not fresh and now - cur.get("tried", 0) > FAIL_RETRY:
            cur["tried"] = now
            for src in SOURCES:
                try:
                    r, name = src(base)
                    if r > 0:
                        cur.update(rate=round(r, 5), source=name, fetched=now)
                        break
                except Exception as e:
                    log.info("fx %s %s failed: %s", base, src.__name__, type(e).__name__)
            _state[base] = cur
            _save_disk()
        if cur.get("rate"):
            return {"base": base, "rate": cur["rate"], "source": cur["source"], "fetched": cur["fetched"],
                    "live": now - cur["fetched"] < TTL * 6}
    return {"base": base, "rate": FALLBACK.get(base), "source": T("内置估算"), "fetched": None, "live": False}


# ---------------------------------------------------------------- helpers for pages / notifications
SUB_CURRENCY = {"GB": "GBP", "CA": "CAD", "QC": "CAD", "WE": "USD", "WS": "USD", "US": "USD", "ASIA": "USD",
                "AU": "AUD", "SG": "SGD", "IN": "INR", "PL": "PLN", "CZ": "CZK"}
SYMBOL_CURRENCY = {"€": "EUR", "$": "USD", "C$": "CAD", "£": "GBP", "zł": "PLN", "A$": "AUD", "S$": "SGD", "₹": "INR"}


def currency_for(subsidiary: str | None = None, symbol: str | None = None, code: str | None = None) -> str:
    if code:
        return code.upper()[:3]
    if symbol and symbol.strip() in SYMBOL_CURRENCY:
        return SYMBOL_CURRENCY[symbol.strip()]
    return SUB_CURRENCY.get((subsidiary or "").upper(), "EUR")


def rate_nowait(base: str = "EUR") -> dict:
    """Cached rate without ever blocking a page render; refreshes in the background when stale."""
    base = (base or "EUR").upper()[:3]
    if base == "CNY":
        return rate("CNY")
    cur = _state.get(base) or {}
    if not (cur.get("fetched") and time.time() - cur["fetched"] < TTL):
        if not _lock.locked():
            threading.Thread(target=rate, args=(base,), daemon=True).start()
    if cur.get("rate"):
        return {"base": base, "rate": cur["rate"], "source": cur.get("source"), "fetched": cur.get("fetched"), "live": True}
    if not _state:
        disk = _load_disk().get(base)
        if disk and disk.get("rate"):
            return {"base": base, "rate": disk["rate"], "source": disk.get("source"), "fetched": disk.get("fetched"), "live": False}
    return {"base": base, "rate": FALLBACK.get(base), "source": T("内置估算"), "fetched": None, "live": False}


def cny(amount, base: str = "EUR", *, wait: bool = False) -> str:
    """'≈ ¥1,029.40' or '' (unknown amount / rate, or already CNY)."""
    try:
        if amount is None or (base or "").upper() == "CNY":
            return ""
        r = (rate if wait else rate_nowait)(base)
        return f"≈ ¥{float(amount) * r['rate']:,.2f}" if r.get("rate") else ""
    except Exception:
        return ""
