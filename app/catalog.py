"""Public OVH catalog: fetched once per (endpoint, subsidiary, type), slimmed, cached on disk + memory."""
from __future__ import annotations

import json
import os
import threading
import time

import httpx

from .config import CACHE_DIR, CATALOG_TTL
from . import ovh_engine as E
from .i18n import T, tr

_mem: dict[tuple, dict] = {}
_locks: dict[tuple, threading.Lock] = {}
_glock = threading.Lock()
UA = "ovh-sniper-web/1.0"


SLIM_VERSION = 2   # v2: pricings for default/upfront12/upfront24 + product technical blobs (official specs)
MODES = ("default", "upfront12", "upfront24")


def _tech(prod: dict | None) -> dict:
    return ((prod or {}).get("blobs") or {}).get("technical") or {}


def _slim(raw: dict) -> dict:
    plans = {}
    for p in raw.get("plans") or []:
        pc = p.get("planCode")
        if not pc:
            continue
        plans[pc] = {
            "planCode": pc,
            "invoiceName": p.get("invoiceName") or pc,
            "product": p.get("product"),
            "pricings": [x for x in (p.get("pricings") or []) if x.get("mode") in MODES][:12],
            "configurations": p.get("configurations") or [],
            "addonFamilies": p.get("addonFamilies") or [],
        }
    used = {a for p in plans.values() for f in p["addonFamilies"] for a in (f.get("addons") or [])}
    addons = {}
    for a in raw.get("addons") or []:
        if a.get("planCode") in used:
            addons[a["planCode"]] = {"invoiceName": a.get("invoiceName"), "product": a.get("product"),
                                     "pricings": [x for x in (a.get("pricings") or []) if x.get("mode") in MODES][:8]}
    want = {p["product"] for p in plans.values()} | {a["product"] for a in addons.values()}
    products = {}
    for pr in raw.get("products") or []:
        t = _tech(pr)
        if pr.get("name") in want and t:
            products[pr["name"]] = {k: t[k] for k in ("server", "memory", "storage", "bandwidth") if k in t}
    loc = raw.get("locale") or {}
    return {"v": SLIM_VERSION, "fetched": time.time(), "currency": loc.get("currencyCode") or "EUR",
            "plans": plans, "addons": addons, "products": products}


def get_catalog(endpoint: str, subsidiary: str, ctype: str, refresh: bool = False) -> dict:
    if endpoint not in E.ENDPOINTS:
        raise ValueError("unknown endpoint")
    if ctype not in E.CATALOG_TYPES:
        raise ValueError("unknown catalog type")
    subsidiary = "".join(ch for ch in subsidiary.upper() if ch.isalpha())[:6] or "IE"
    key = (endpoint, subsidiary, ctype)
    with _glock:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:
        cur = _mem.get(key)
        if cur and not refresh and cur.get("v") == SLIM_VERSION and time.time() - cur["fetched"] < CATALOG_TTL:
            return cur
        path = os.path.join(CACHE_DIR, f"{endpoint}_{subsidiary}_{ctype}.json")
        if not refresh and os.path.exists(path) and time.time() - os.path.getmtime(path) < CATALOG_TTL:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("v") == SLIM_VERSION:
                _mem[key] = data
                return data
        url = f"{E.ENDPOINTS[endpoint]}/order/catalog/public/{ctype}"
        r = httpx.get(url, params={"ovhSubsidiary": subsidiary}, timeout=60, headers={"User-Agent": UA})
        r.raise_for_status()
        data = _slim(r.json())
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        _mem[key] = data
        return data


def peek(endpoint: str, subsidiary: str, ctype: str) -> dict | None:
    """Cached catalog (memory, then disk, any age) without ever hitting the network. For display labels."""
    subsidiary = "".join(ch for ch in (subsidiary or "").upper() if ch.isalpha())[:6] or "IE"
    key = (endpoint, subsidiary, ctype)
    cur = _mem.get(key)
    if cur and cur.get("v") == SLIM_VERSION:
        return cur
    path = os.path.join(CACHE_DIR, f"{endpoint}_{subsidiary}_{ctype}.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if data.get("v") != SLIM_VERSION:
        return None
    _mem.setdefault(key, data)
    return data


def cur_symbol(code: str) -> str:
    return {"EUR": "€", "USD": "$", "CAD": "C$", "GBP": "£", "PLN": "zł", "AUD": "A$", "SGD": "S$", "INR": "₹"}.get(code, code + " ")


def plan_summaries(cat: dict, q: str = "") -> list[dict]:
    q = (q or "").strip().lower()
    out = []
    sym = cur_symbol(cat.get("currency", "EUR"))
    for pc, p in cat["plans"].items():
        name = p["invoiceName"]
        if q and q not in name.lower() and q not in pc.lower():
            continue
        mo, setup = E.monthly_and_setup(p)
        out.append({"planCode": pc, "name": name, "monthly": mo, "setup": setup,
                    "label": T("{0}  [{1}]  {2}/月", name, pc, E.fmt_price(mo, sym)), "dcs": E.allowed_dcs(p)})
    out.sort(key=lambda x: (x["name"].split(" |")[0], x["planCode"]))
    return out


def plan_detail(cat: dict, plan_code: str) -> dict | None:
    p = cat["plans"].get(plan_code)
    if not p:
        return None
    sym = cur_symbol(cat.get("currency", "EUR"))
    fams = {}
    defaults = E.default_addons(p)
    for fam in p["addonFamilies"]:
        name = fam.get("name")
        items = []
        for code in fam.get("addons") or []:
            a = cat["addons"].get(code) or {}
            mo, setup = E.monthly_and_setup(a)
            frag = E.strip_plan_suffix(code, plan_code) if name in ("memory", "storage") else code
            label = (E.short_ram(frag) if name == "memory" else E.short_disk(frag) if name == "storage" else
                     (a.get("invoiceName") or code))
            items.append({"code": code, "fragment": frag, "label": T("{0}  (+{1}/月)", label, E.fmt_price(mo, sym)),
                          "default": code == fam.get("default")})
        fams[name] = {"mandatory": bool(fam.get("mandatory")), "items": items}
    mo, setup = E.monthly_and_setup(p)
    return {"planCode": plan_code, "name": p["invoiceName"], "monthly": mo, "setup": setup, "currency": sym,
            "dcs": [{"code": d, "label": E.dc_label(d)} for d in E.allowed_dcs(p)],
            "regions": E.allowed_regions(p), "defaults": defaults, "families": fams}
