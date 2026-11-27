"""Per-user OVH buying engine.

Ported from the original scripts (ovh_bf.py / autobuy_focus.py) with the global
state (single ~/.ovh.conf, fixed subsidiary, fixed eco catalog, lock files)
replaced by explicit parameters so many users / tasks can share it.

Kept from the originals, unchanged in spirit:
  * live_stock / pick_stock / addon_code / is_default_config / region_for
  * build_cart: item + dedicated_datacenter/dedicated_os/region configs + mandatory
    addon families; never invent addon codes; refuse to swap a pinned RAM/disk for
    the catalog default
  * cart verification (exact planCode set + configuration read-back)
  * preview (GET /checkout) and price-cap check before ordering on the slow path
  * checkout with autoPayWithPreferredPaymentMethod + waiveRetractationPeriod
  * definite 4xx rejection => ORDER_FAIL (no order); anything else (timeout,
    network, 5xx) => PENDING_CONFIRM: stop the task for good and list /me/order
  * after an order: poll /me/order/{id}/status up to ~90s => ORDER_PAID / ORDER_UNPAID
  * prebuilt-cart fast path (Slots): one verified cart per DC, estimated first
    invoice = cart item TOTAL + catalog setup fees, x VAT; revalidated, rebuilt
    before expiry, deleted on stop
"""
from __future__ import annotations

import datetime
import json
import re
import threading
import time
from typing import Any, Callable

import ovh
import ovh.exceptions as oe
from .i18n import T, TDict, tr
from . import ratelimit as RL

ENDPOINTS = {
    "ovh-eu": "https://eu.api.ovh.com/1.0",
    "ovh-ca": "https://ca.api.ovh.com/1.0",
    "ovh-us": "https://api.us.ovhcloud.com/1.0",
}
# Subsidiaries accepted per endpoint (OVH /order/catalog/public ovhSubsidiary values)
SUBSIDIARIES = {
    "ovh-eu": ["IE", "FR", "GB", "DE", "ES", "IT", "NL", "PL", "PT", "CZ", "FI", "LT", "MA", "SN", "TN"],
    "ovh-ca": ["CA", "QC", "WE", "WS", "ASIA", "AU", "SG", "IN"],
    "ovh-us": ["US"],
}
CATALOG_TYPES = TDict({"eco": "Eco（Kimsufi/So you Start/Rise）", "baremetalServers": "Bare Metal（Advance/Scale/HGR）"})

OS_DEFAULT = "none_64.en"
DC_LABEL = TDict({
    "bhs": "加拿大 BHS", "gra": "法国 GRA", "rbx": "法国 RBX", "sbg": "法国 SBG",
    "fra": "德国 FRA", "lon": "英国 LON", "waw": "波兰 WAW", "sgp": "新加坡 SGP",
    "syd": "澳大利亚 SYD", "mum": "印度 MUM", "ynm": "印度 YNM", "hil": "美国 HIL", "vin": "美国 VIN",
    "par": "法国 PAR", "eri": "英国 ERI", "lim": "德国 LIM", "bom": "印度 BOM", "yyz": "加拿大 YYZ",
})
CANADA_DCS = {"bhs", "yyz"}
APAC_DCS = {"sgp", "syd", "mum", "bom", "ynm"}
US_DCS = {"hil", "vin"}
UNAVAIL = {"unavailable", "unknown", "comingSoon"}
PAID = {"checking", "delivering", "delivered", "documentsRequested"}
# Checkout errors that prove no order was created. 409 Conflict is NOT here: OVH can answer 409 for a cart that is
# already being / has been checked out, so after POST /checkout it means "unknown" => PENDING_CONFIRM (v2.1.1).
DEFINITE_REJECT = (oe.BadParametersError, oe.ResourceNotFoundError,
                   oe.ResourceExpiredError, oe.Forbidden, oe.NotGrantedCall, oe.NotCredential,
                   oe.InvalidKey, oe.InvalidCredential)
AUTH_ERRORS = (oe.InvalidKey, oe.InvalidCredential, oe.Forbidden, oe.NotCredential, oe.NotGrantedCall)

# Access rules requested by the "generate consumer key" helper: enough to read the
# account, build carts, check out and read orders. Nothing else.
ACCESS_RULES = [
    {"method": "GET", "path": "/me"},
    {"method": "GET", "path": "/me/*"},
    {"method": "GET", "path": "/order/*"},
    {"method": "POST", "path": "/order/*"},
    {"method": "PUT", "path": "/order/*"},
    {"method": "DELETE", "path": "/order/*"},
    # v1.3: read-only view of the account's existing servers on the 订单记录 page
    {"method": "GET", "path": "/dedicated/server"},
    {"method": "GET", "path": "/dedicated/server/*"},
]



def is_auth_error(e: Exception) -> bool:
    """Credential problems. OVH answers an unknown application key with a bare 401/403
    APIError (no errorCode), so check the HTTP status too, not just the exception type."""
    if isinstance(e, AUTH_ERRORS):
        return True
    resp = getattr(e, "response", None)
    return isinstance(e, oe.APIError) and resp is not None and getattr(resp, "status_code", 0) in (401, 403)


API_CONSOLE = {
    "ovh-eu": "https://eu.api.ovh.com",
    "ovh-ca": "https://ca.api.ovh.com",
    "ovh-us": "https://api.us.ovhcloud.com",
}
ENDPOINT_HELP = TDict({
    "ovh-eu": ("欧洲区", "账号在 OVH 欧洲站注册（ovhcloud.com/en-ie、/fr、/de、/en-gb、/pl、/es、/it…），子公司 IE/FR/DE/GB 等"),
    "ovh-ca": ("加拿大 / 亚太 / 全球区", "账号在 ovhcloud.com/en-ca、/fr-ca、/asia、/en-sg、/en-au、/en-in 或国际站（WE/WS）注册"),
    "ovh-us": ("美国区", "账号在 us.ovhcloud.com 注册（OVHcloud US，独立体系）"),
})


def create_token_url(endpoint: str) -> str:
    """createToken page with our access rules prefilled (?GET=/me&POST=/order/*...)."""
    from urllib.parse import quote
    q = "&".join(f"{r['method']}={quote(r['path'], safe='/*')}" for r in ACCESS_RULES)
    return f"{API_CONSOLE[endpoint]}/createToken/?{q}"


def create_app_url(endpoint: str) -> str:
    return f"{API_CONSOLE[endpoint]}/createApp/"


Logger = Callable[[str, str], None]  # (marker, message)


# ---------------------------------------------------------------- formatting
# Prices come in two formats and are parsed by SOURCE, never guessed from the magnitude (v2.1.1: the old euro()
# divided anything >= 1000 by 1e8, so a real €1,234.56 cart total became €0.0000123 and passed every price cap).
def catalog_price(n: Any) -> float:
    """/order/catalog/public pricings[].price: an integer in 1/100,000,000 of the currency (1699000000 = 16.99)."""
    if n is None or isinstance(n, bool):
        return 0.0
    if isinstance(n, dict):          # never expected in the catalog; refuse to guess
        raise ValueError(f"unexpected catalog price object: {n!r}")
    return float(n) / 1e8


def price_value(p: Any) -> float | None:
    """Cart / checkout / order price object {"value": 1234.56, "text": "€1,234.56", "currencyCode": "EUR"}: value is
    already in currency units. "priceInUcents" (1/100,000,000 units) is accepted too. Anything else => None
    (callers that place orders treat None as "price unknown" and refuse)."""
    if not isinstance(p, dict):
        return None
    v = p.get("value")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    u = p.get("priceInUcents")
    if isinstance(u, (int, float)) and not isinstance(u, bool):
        return float(u) / 1e8
    return None


def fmt_price(n: float | None, cur: str = "€") -> str:
    return "?" if n is None else f"{cur}{n:.2f}"


def short_ram(code: str) -> str:
    from .labels import ram_label
    return ram_label(code) or "?"


def short_disk(code: str) -> str:
    from .labels import disk_label
    return disk_label(code) or "?"


def dc_label(dc: str) -> str:
    return DC_LABEL.get(dc, dc.upper())


# ---------------------------------------------------------------- catalog helpers
def region_for(dc: str, allowed: list[str] | None) -> str:
    if dc in CANADA_DCS:
        cand = "canada"
    elif dc in APAC_DCS:
        cand = "apac" if allowed and "apac" in allowed else "asia"
    elif dc in US_DCS:
        cand = "us"
    else:
        cand = "europe"
    if allowed and cand not in allowed:
        return allowed[0]
    return cand


def monthly_and_setup(plan_obj: dict) -> tuple[float | None, float | None]:
    monthly = setup = None
    for x in plan_obj.get("pricings") or []:
        if x.get("mode") != "default":
            continue
        caps = x.get("capacities") or []
        price = catalog_price(x.get("price") or 0)
        if x.get("interval") == 1 and "renew" in caps and monthly is None:
            monthly = price
        if (x.get("interval") in (0, None) or x.get("intervalUnit") == "none") and "installation" in caps and setup is None:
            setup = price
    return monthly, setup


def default_addons(plan_obj: dict) -> dict[str, str]:
    return {f["name"]: f["default"] for f in plan_obj.get("addonFamilies") or [] if f.get("default")}


def family_addons(plan_obj: dict, family: str) -> list[str]:
    for f in plan_obj.get("addonFamilies") or []:
        if f.get("name") == family:
            return list(f.get("addons") or [])
    return []


def strip_plan_suffix(code: str, plan: str) -> str:
    """ram-32g-ecc-3200-24rise01-v2 -> ram-32g-ecc-3200 (the availability FQN fragment)."""
    for suffix in (plan, plan.split("-v")[0]):
        if suffix and code.endswith("-" + suffix):
            return code[: -len(suffix) - 1]
    return code


_NOISE = re.compile(r"^ddr\d$")


def _norm_tokens(code: str, plan: str) -> frozenset:
    """'ram-64g-ddr4-ecc-2666-27skle03-v1' -> {'ram', '64g', 'ecc', '2666'} (plan suffix and DDR generation dropped)."""
    return frozenset(t for t in strip_plan_suffix(code.lower(), plan.lower()).split("-") if t and not _NOISE.match(t))


def canon_cfg(code: str, plan: str = "") -> frozenset:
    """Canonical form used for EXACT config matching (v2.1.1): lower-case, whitespace stripped, the plan suffix
    removed (catalog addons are '<fragment>-<planCode>', availability uses '<fragment>') and the DDR generation token
    dropped (OVH lists the same stick as ram-64g-ecc-2666 and ram-64g-ddr4-ecc-2666). Every other token must be equal:
    no substring / prefix matching, so ram-32g never matches ram-32g-ecc-2400 and 2x480 never matches 2x4800."""
    return _norm_tokens((code or "").strip().lower(), (plan or "").strip().lower())


def same_cfg(a: str, b: str, plan: str = "") -> bool:
    return bool(a) and bool(b) and canon_cfg(a, plan) == canon_cfg(b, plan)


def addon_code(plan_obj: dict, family: str, want: str, *, synthesize: bool = True) -> str | None:
    """Map an availability FQN fragment to a catalog addon planCode by EXACT match:
    1. identical code, or the code minus the plan suffix  2. identical canonical form (canon_cfg), only if unique.
    No substring / prefix matching (v2.1.1). Never silently falls back to the family default."""
    if not want:
        return None
    codes = family_addons(plan_obj, family)
    plan = plan_obj.get("planCode") or ""
    w = want.strip()
    for code in codes:
        if code == w or strip_plan_suffix(code, plan) == w:
            return code
    wt = canon_cfg(w, plan)
    same = [c for c in codes if canon_cfg(c, plan) == wt]
    if len(same) == 1:
        return same[0]
    if not synthesize:
        return None
    if plan and not w.endswith(plan):
        return f"{w}-{plan}"
    return w


def orderable(plan_obj: dict | None, memory: str, storage: str) -> tuple[bool, str]:
    """Can this availability config be put in a cart? OVH's availability API sometimes lists hardware variants
    (e.g. KS-LE-3 ram-64g-ddr4-ecc-2666) that have no addon in the public catalog nor in the cart's options,
    so they cannot be ordered explicitly. Returns (ok, reason)."""
    if not plan_obj:
        return True, ""
    for fam, want in (("memory", memory), ("storage", storage)):
        if not want or not family_addons(plan_obj, fam):
            continue
        if not addon_code(plan_obj, fam, want, synthesize=False):
            have = " / ".join((short_ram if fam == "memory" else short_disk)(strip_plan_suffix(c, plan_obj.get("planCode") or ""))
                              for c in family_addons(plan_obj, fam))
            what = T("内存") if fam == "memory" else T("硬盘")
            return False, T("{0} {1} 在官方目录里没有可下单的配件（可选：{2}）", what, (short_ram if fam == 'memory' else short_disk)(want), have)
    return True, ""


def is_default_config(plan_obj: dict, memory: str, storage: str) -> bool:
    defs = default_addons(plan_obj)
    plan = plan_obj.get("planCode") or ""
    return same_cfg(memory, defs.get("memory", ""), plan) and same_cfg(storage, defs.get("storage", ""), plan)


def allowed_dcs(plan_obj: dict) -> list[str]:
    for c in plan_obj.get("configurations") or []:
        if c.get("name") == "dedicated_datacenter":
            return list(c.get("values") or [])
    return []


def allowed_regions(plan_obj: dict) -> list[str]:
    for c in plan_obj.get("configurations") or []:
        if c.get("name") == "region":
            return list(c.get("values") or [])
    return ["europe", "canada"]


def live_stock(plan: str, plan_obj: dict | None, data: list[dict]) -> list[dict]:
    rows = []
    for e in data:
        if e.get("planCode") and e.get("planCode") != plan:
            continue
        dcs = [(d["datacenter"], d["availability"]) for d in e.get("datacenters") or []
               if d.get("availability") not in UNAVAIL]
        if not dcs:
            continue
        mem, sto = e.get("memory") or "", e.get("storage") or ""
        ok, why = orderable(plan_obj, mem, sto)
        rows.append({"fqn": e.get("fqn"), "memory": mem, "storage": sto, "datacenters": dcs,
                     "is_default": is_default_config(plan_obj, mem, sto) if plan_obj else False,
                     "orderable": ok, "why": why, "raw": e})
    return rows


def stock_signature(stock: list[dict]) -> str:
    return "\n".join(f"{r.get('memory')}|{r.get('storage')}|" + ",".join(f"{d}:{s}" for d, s in r["datacenters"])
                     for r in stock)


def pick_stock(rows: list[dict], dcs: list[str], any_config: bool, want_ram: str | None,
               want_disk: str | None, plan: str = "") -> dict | None:
    """Pick the first matching in-stock row and datacenter (dcs in priority order).
    Without --any-config and without pinned RAM/disk only the default config is bought.
    Pinned RAM/disk match EXACTLY on the canonical code (same_cfg), never by prefix (v2.1.1)."""
    def ram_ok(row):
        return (not want_ram) or same_cfg(row["memory"], want_ram, plan)

    def disk_ok(row):
        return (not want_disk) or same_cfg(row["storage"], want_disk, plan)

    def dc_ok(row):
        have = [dc for dc, _ in row["datacenters"]]
        if not dcs:
            return have[0] if have else None
        for dc in dcs:
            if dc in have:
                return dc
        return None

    # base (default) config first; non-default rows are only eligible with any_config or pinned RAM/disk
    ordered = rows if want_ram or want_disk else sorted(rows, key=lambda r: 0 if r["is_default"] else 1)
    for row in ordered:
        if not row.get("orderable", True):   # listed by the availability API but not purchasable
            continue
        if not ram_ok(row) or not disk_ok(row):
            continue
        if not row["is_default"] and not any_config and not want_ram and not want_disk:
            continue
        dc = dc_ok(row)
        if dc:
            av = dict(row["datacenters"]).get(dc)
            return {**row, "dc": dc, "availability": av}
    return None


def explain_no_match(rows: list[dict], dcs: list[str], any_config: bool, want_ram: str | None,
                     want_disk: str | None) -> str:
    """Why in-stock rows were not picked (shown in HEARTBEAT / status). Empty when nothing is in stock."""
    if not rows:
        return ""
    out = []
    for r in rows[:4]:
        cfg = f"{short_ram(r['memory'])} · {short_disk(r['storage'])}"
        where = ",".join(dc.upper() for dc, _ in r["datacenters"])
        if not r.get("orderable", True):
            why = T("官方目录无此配件，无法下单")
        elif want_ram and not same_cfg(r["memory"], want_ram):
            why = T("不是所选内存 {0}", short_ram(want_ram))
        elif want_disk and not same_cfg(r["storage"], want_disk):
            why = T("不是所选硬盘 {0}", short_disk(want_disk))
        elif not r["is_default"] and not any_config and not want_ram and not want_disk:
            why = T("非基础配置（未开启「允许非基础配置」）")
        elif dcs and not any(dc in dcs for dc, _ in r["datacenters"]):
            why = T("不在所选机房")
        else:
            why = T("未匹配")
        out.append(T("{0} @ {1}：{2}", cfg, where, why))
    more = T(" 等 {0} 个", len(rows)) if len(rows) > 4 else ""
    return T("有货但不匹配") + more + T("：") + T("；").join(out)


def format_row(plan_obj: dict, row: dict) -> str:
    flag = T("基础配置") if row["is_default"] else T("非默认配置")
    if not row.get("orderable", True):
        flag += T(" · 不可下单（官方目录无此配件）")
    dcs = ", ".join(f"{dc_label(dc)} {st}" for dc, st in row["datacenters"])
    return T("有货 · {0} · {1} · {2} · {3}", flag, short_ram(row['memory']), short_disk(row['storage']), dcs)


# ---------------------------------------------------------------- price parsing
def extract_prices(preview: dict) -> dict[str, float]:
    prices = preview.get("prices") or preview
    out = {}
    if isinstance(prices, list):
        for p in prices:
            if not isinstance(p, dict):
                continue
            v = price_value(p.get("price")) if isinstance(p.get("price"), dict) else price_value(p)
            if v is not None:
                out[str(p.get("label") or p.get("id") or "")] = v
        return out
    if isinstance(prices, dict):
        for k, v in prices.items():          # bare numbers are ambiguous (unit unknown): skipped, never guessed
            pv = price_value(v)
            if pv is not None:
                out[k] = pv
    return out


def first_invoice_total(preview: dict) -> float | None:
    prices = preview.get("prices") or {}
    extracted = extract_prices(preview)
    if isinstance(prices, dict):
        for key in ("withTax", "priceWithTax", "totalWithTax"):
            if key in prices:
                return price_value(prices[key])    # None if missing/unparseable => callers refuse to order
    for k, v in extracted.items():
        if "withtax" in k.lower().replace("_", ""):
            return v
    return None


# ---------------------------------------------------------------- client
def make_client(endpoint: str, app_key: str, app_secret: str, consumer_key: str | None,
                timeout=(5, 30)) -> ovh.Client:
    return ovh.Client(endpoint=endpoint, application_key=app_key, application_secret=app_secret,
                      consumer_key=consumer_key or None, timeout=timeout)


def request_consumer_key(endpoint: str, app_key: str, app_secret: str, redirect: str | None) -> dict:
    """POST /auth/credential; returns {'validationUrl', 'consumerKey', 'state'}."""
    c = ovh.Client(endpoint=endpoint, application_key=app_key, application_secret=app_secret, timeout=(5, 20))
    return c.request_consumerkey(ACCESS_RULES, redirect_url=redirect or None)


def account_info(client: ovh.Client) -> dict:
    me = client.get("/me")
    methods = []
    try:
        raw = client.get("/me/payment/method") or []
        for m in raw[:10]:
            methods.append(m if isinstance(m, dict) else client.get(f"/me/payment/method/{m}"))
    except Exception:
        pass
    default = [m for m in methods if m.get("default")]
    return {"nichandle": me.get("nichandle"), "subsidiary": me.get("ovhSubsidiary"),
            "currency": (me.get("currency") or {}).get("code"),
            "payment_methods": len(methods),
            "default_payment": (default[0].get("paymentType") if default else None)}


def parse_exp(exp):
    try:
        return datetime.datetime.fromisoformat(exp).timestamp()
    except Exception:
        return time.time() + 3600


def new_assigned_cart(client, subsidiary: str) -> tuple[str, float]:
    cart = client.post("/order/cart", ovhSubsidiary=subsidiary)
    cid = cart["cartId"]
    client.post(f"/order/cart/{cid}/assign")
    return cid, parse_exp(cart.get("expire"))


def recent_orders(client, since_iso: str) -> list[dict]:
    out = []
    try:
        for oid in client.get("/me/order", **{"date.from": since_iso})[-10:]:
            o = client.get(f"/me/order/{oid}")
            st = client.get(f"/me/order/{oid}/status")
            out.append({"orderId": oid, "date": o.get("date"), "status": st,
                        "priceWithTax": (o.get("priceWithTax") or {}).get("text"), "url": o.get("url")})
    except Exception as e:
        out.append({"error": f"{type(e).__name__}: {e}"})
    return out


# ---------------------------------------------------------------- order spec
class OrderSpec:
    """Everything needed to build a cart for one task (replaces argparse args / TARGETS)."""

    def __init__(self, *, subsidiary: str, catalog_type: str, plan_obj: dict, os_code: str = OS_DEFAULT,
                 duration: str = "P1M", pricing_mode: str = "default", quantity: int = 1,
                 bandwidth: str = "", extra_addons: list[str] | None = None, price_cap: float | None = None,
                 auto_pay: bool = True, vat_rate: float = 1.23, addon_pricing: dict | None = None):
        self.subsidiary = subsidiary
        self.catalog_type = catalog_type if catalog_type in CATALOG_TYPES else "eco"
        self.plan_obj = plan_obj
        self.plan = plan_obj["planCode"]
        self.os_code = os_code or OS_DEFAULT
        self.duration = duration or "P1M"
        self.pricing_mode = pricing_mode or "default"
        self.quantity = max(1, int(quantity or 1))
        self.bandwidth = bandwidth
        self.extra_addons = [x for x in (extra_addons or []) if x]
        self.price_cap = price_cap
        self.auto_pay = auto_pay
        self.vat_rate = vat_rate or 1.0
        self.addon_pricing = addon_pricing or {}
        self.regions = allowed_regions(plan_obj)

    def addon_codes(self, memory: str, storage: str) -> list[str]:
        """Resolve all addon planCodes for this cart. Mirrors build_cart() of ovh_bf.py."""
        mapping = {"memory": memory, "storage": storage, "bandwidth": ""}
        codes = []
        for fam in self.plan_obj.get("addonFamilies") or []:
            name = fam.get("name")
            if name == "bandwidth" and self.bandwidth:
                if self.bandwidth not in (fam.get("addons") or []):
                    raise RuntimeError(T("带宽配件 {0} 不在目录中", self.bandwidth))
                codes.append(self.bandwidth)
                continue
            if not fam.get("mandatory") and name not in ("memory", "storage", "bandwidth", "system-storage",
                                                         "system", "storage-system"):
                continue
            want = mapping.get(name, "")
            if want:
                code = addon_code(self.plan_obj, name, want, synthesize=False)
            else:
                code = fam.get("default") if fam.get("mandatory") else None
            if not code:
                if fam.get("mandatory"):
                    raise RuntimeError(T("无法匹配必选配件 {0}: {1}", name, want or T("(空)")))
                continue
            if want and name in ("memory", "storage"):
                default = fam.get("default") or ""
                listed = any(c == want or same_cfg(c, want, self.plan) for c in (fam.get("addons") or []))
                if not listed and code == default:
                    raise RuntimeError(T("{0} 配件 {1} 不在官方目录中，拒绝改用默认 {2}", name, want, default))
            codes.append(code)
        for extra in self.extra_addons:
            if extra not in codes:
                codes.append(extra)
        return codes

    def setup_fee(self, codes: list[str]) -> float:
        total = monthly_and_setup(self.plan_obj)[1] or 0.0
        for code in codes:
            total += monthly_and_setup({"pricings": self.addon_pricing.get(code) or []})[1] or 0.0
        return total * self.quantity


def build_cart(client, spec: OrderSpec, dc: str, memory: str, storage: str,
               cart_id: str | None = None, workers: list | None = None) -> tuple[str, int, list[str], float]:
    """Create (or reuse) an assigned cart and fully configure one item. Returns (cid, iid, codes, expire_ts).

    With `workers` (extra signed clients) the 3 configurations and the addon options are sent in parallel,
    like slow_attempt() in autobuy_focus.py; any call that fails in parallel is retried once sequentially."""
    codes = spec.addon_codes(memory, storage)
    ct = spec.catalog_type
    cid, exp = cart_id, time.time() + 3600
    if cid:
        try:
            exp = parse_exp(client.get(f"/order/cart/{cid}").get("expire"))
        except Exception:
            cid = None
    if not cid:
        cid, exp = new_assigned_cart(client, spec.subsidiary)
    item = client.post(f"/order/cart/{cid}/{ct}", planCode=spec.plan, duration=spec.duration,
                       pricingMode=spec.pricing_mode, quantity=spec.quantity)
    iid = item["itemId"]
    calls = [("cfg", label, value) for label, value in (("dedicated_datacenter", dc), ("dedicated_os", spec.os_code),
                                                          ("region", region_for(dc, spec.regions)))]
    calls += [("opt", code, None) for code in codes]

    def run(c, cl):
        if c[0] == "cfg":
            return cl.post(f"/order/cart/{cid}/item/{iid}/configuration", label=c[1], value=c[2])
        return cl.post(f"/order/cart/{cid}/{ct}/options", itemId=iid, planCode=c[1], duration=spec.duration,
                       pricingMode=spec.pricing_mode, quantity=spec.quantity)

    if workers:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(workers)) as pool:
            futs = [(c, pool.submit(run, c, workers[i % len(workers)])) for i, c in enumerate(calls)]
            failed = []
            for c, f in futs:
                try:
                    f.result()
                except Exception:
                    failed.append(c)
        for c in failed:
            run(c, client)  # sequential retry; a second failure propagates
    else:
        for c in calls:
            run(c, client)
    return cid, iid, codes, exp


def verify_cart(client, spec: OrderSpec, cid: str, iid: int, dc: str, codes: list[str]) -> tuple[float, float]:
    """Read the cart back; verify exact planCodes + configuration; return (monthly ex-VAT TOTAL, expire_ts)."""
    vals = {}
    for cf in client.get(f"/order/cart/{cid}/item/{iid}/configuration"):
        x = client.get(f"/order/cart/{cid}/item/{iid}/configuration/{cf}")
        vals[x["label"]] = x["value"]
    want_cfg = {"dedicated_datacenter": dc, "dedicated_os": spec.os_code, "region": region_for(dc, spec.regions)}
    if any(vals.get(k) != v for k, v in want_cfg.items()):
        raise RuntimeError(T("购物车配置不一致 {0} != {1}", vals, want_cfg))
    cart = client.get(f"/order/cart/{cid}")
    want = {spec.plan, *codes}
    got, monthly = set(), 0.0
    for i in cart.get("items") or []:
        x = client.get(f"/order/cart/{cid}/item/{i}")
        got.add((x.get("settings") or {}).get("planCode"))
        for p in x.get("prices") or []:
            if p.get("label") == "TOTAL":
                monthly += float((p.get("price") or {}).get("value") or 0)
    if got != want or len(cart.get("items") or []) != len(want):
        raise RuntimeError(T("购物车内容不一致 {0} != {1}", sorted(map(str, got)), sorted(want)))
    return monthly, parse_exp(cart.get("expire"))


def safe_delete(client, cid: str | None) -> None:
    if not cid:
        return
    try:
        client.delete(f"/order/cart/{cid}")
    except Exception:
        pass


# ---------------------------------------------------------------- prebuilt carts (fast path)
FRESH_PRICE_MAX = 2700
REBUILD_BEFORE_EXPIRY = 7200
REVALIDATE_EVERY = 1800
MIN_LEFT_AT_HIT = 600


class Slots:
    """One prebuilt, verified cart per datacenter for a pinned memory+storage config."""

    def __init__(self, client, spec: OrderSpec, memory: str, storage: str, dcs: list[str], log: Logger,
                 on_change: Callable[[], None] | None = None):
        self.client, self.spec, self.memory, self.storage, self.dcs, self.log = client, spec, memory, storage, dcs, log
        self.mu = threading.Lock()
        self.s: dict[str, dict] = {}
        self.on_change = on_change or (lambda: None)
        self.rl_key: tuple | None = None   # ("acct", user_id, endpoint): shared 429 back-off

    def _real_price(self, cid: str, estimate: float) -> tuple[float, bool]:
        """Pre-read OVH's real first-invoice total incl. tax while the cart is warm (GET /checkout), so the slot's
        price is the real one and the hit-time check is only a final verification. Falls back to the estimate."""
        try:
            real = first_invoice_total(self.client.get(f"/order/cart/{cid}/checkout"))
        except Exception as e:
            if RL.is_429(e) and self.rl_key:
                RL.hit(self.rl_key, RL.retry_after_of(e))
            real = None
        return (round(real, 2), True) if real is not None else (estimate, False)

    def cart_ids(self) -> list[str]:
        with self.mu:
            return [v["cid"] for v in self.s.values()]

    def build(self, dc: str) -> dict | None:
        cid = None
        try:
            cid, iid, codes, exp = build_cart(self.client, self.spec, dc, self.memory, self.storage)
            monthly, exp = verify_cart(self.client, self.spec, cid, iid, dc, codes)
            price, real = self._real_price(cid, round((monthly + self.spec.setup_fee(codes)) * self.spec.vat_rate, 2))
            ok = self.spec.price_cap is None or price <= self.spec.price_cap
            slot = {"cid": cid, "iid": iid, "codes": codes, "exp": exp, "price": price, "real": real, "checked": time.time(), "ok": ok}
            if not ok:
                self.log("ERROR", T("预建购物车 {0} 估算首期 {1} > 上限 {2}，该机房快速通道停用", dc, price, self.spec.price_cap))
            with self.mu:
                old = self.s.get(dc)
                self.s[dc] = slot
            self.on_change()
            if old and old.get("cid") != cid:
                safe_delete(self.client, old["cid"])
            return slot
        except Exception as e:
            if RL.is_429(e) and self.rl_key:
                RL.hit(self.rl_key, RL.retry_after_of(e))
            self.log("ERROR", T("预建购物车 {0} 失败: {1}: {2}", dc, type(e).__name__, e))
            safe_delete(self.client, cid)
            return None

    def revalidate(self, dc: str) -> None:
        with self.mu:
            slot = dict(self.s.get(dc) or {})
        if not slot:
            return
        try:
            monthly, exp = verify_cart(self.client, self.spec, slot["cid"], slot["iid"], dc, slot["codes"])
            price, real = self._real_price(slot["cid"], round((monthly + self.spec.setup_fee(slot["codes"])) * self.spec.vat_rate, 2))
            with self.mu:
                cur = self.s.get(dc)
                if cur and cur["cid"] == slot["cid"]:
                    cur.update(price=price, real=real, exp=exp, checked=time.time(),
                               ok=self.spec.price_cap is None or price <= self.spec.price_cap)
        except Exception as e:
            if RL.is_429(e) and self.rl_key:
                RL.hit(self.rl_key, RL.retry_after_of(e))
                return                      # rate limited: keep the slot, retry after the back-off
            self.log("ERROR", T("复核预建购物车 {0} 失败（{1}: {2}），将重建", dc, type(e).__name__, e))
            with self.mu:
                if (self.s.get(dc) or {}).get("cid") == slot["cid"]:
                    self.s.pop(dc, None)

    def maintain_once(self, stop: threading.Event) -> None:
        now = time.time()
        for dc in self.dcs:
            if stop.is_set() or (self.rl_key and RL.remaining(self.rl_key) > 0):
                return
            with self.mu:
                slot = self.s.get(dc)
            if slot is None or slot["exp"] - now < REBUILD_BEFORE_EXPIRY:
                self.build(dc)
                stop.wait(1.0)
            elif now - slot["checked"] > REVALIDATE_EVERY:
                self.revalidate(dc)
                stop.wait(0.3)

    def ready(self, dc: str) -> bool:
        now = time.time()
        with self.mu:
            slot = self.s.get(dc)
            return bool(slot and slot["ok"] and now - slot["checked"] < FRESH_PRICE_MAX
                        and slot["exp"] - now > MIN_LEFT_AT_HIT)

    def take(self, dc: str) -> dict | None:
        """Single use: a usable slot is removed from the pool (same as the original)."""
        if self.ready(dc):
            with self.mu:
                slot = self.s.pop(dc, None)
            self.on_change()
            return slot
        return None

    def summary(self) -> str:
        with self.mu:
            return ", ".join(f"{dc}:{v['price']}{'' if v['ok'] else T('(超上限)')}" for dc, v in self.s.items()) or "-"

    def delete_all(self) -> None:
        with self.mu:
            cids = [v["cid"] for v in self.s.values()]
            self.s.clear()
        for cid in cids:
            safe_delete(self.client, cid)
        self.on_change()


# ---------------------------------------------------------------- order attempt
class CheckoutResult:
    def __init__(self, outcome: str, **info):
        # outcome: preview | ordered | rejected | failed | pending
        self.outcome, self.info = outcome, info


def check_cart_price(client, spec: OrderSpec, cid: str, log: Logger) -> CheckoutResult:
    """GET /order/cart/{cid}/checkout and compare OVH's REAL first-invoice total incl. tax (prices.withTax) to the cap.
    Fails closed: unreadable / missing price => rejected (cart deleted). Used right before every real checkout,
    including the prebuilt-cart fast path whose local estimate is only a prefilter (v2.1.1)."""
    try:
        prev = client.get(f"/order/cart/{cid}/checkout")
    except Exception as e:
        safe_delete(client, cid)
        return CheckoutResult("rejected", reason=T("读取购物车真实价格失败 {0}: {1}，拒绝下单", type(e).__name__, e),
                              rate_limited=RL.is_429(e), retry_after=RL.retry_after_of(e))
    total = first_invoice_total(prev)
    log("PREVIEW", T("购物车预览: ") + json.dumps(prev.get("prices", prev), ensure_ascii=False, default=str)[:800])
    if total is None:
        safe_delete(client, cid)
        return CheckoutResult("rejected", reason=T("无法解析首期价格，拒绝下单"))
    if spec.price_cap is not None and total > spec.price_cap:
        safe_delete(client, cid)
        return CheckoutResult("rejected", reason=T("首期含税 {0:.2f} 超过上限 {1:.2f}", total, spec.price_cap), total=total)
    return CheckoutResult("preview", cid=cid, total=total)


def preview_cart(client, spec: OrderSpec, pick: dict, log: Logger, keep: bool = False,
                 workers: list | None = None) -> CheckoutResult:
    """Slow path part 1: build cart, GET /checkout, parse first invoice, enforce cap. Deletes cart unless keep.
    keep=True means a real order follows: then an unparseable price is always refused, cap or not."""
    cid = None
    try:
        cid, iid, codes, _ = build_cart(client, spec, pick["dc"], pick["memory"], pick["storage"], workers=workers)
        prev = client.get(f"/order/cart/{cid}/checkout")
        total = first_invoice_total(prev)
        log("PREVIEW", T("购物车预览: ") + json.dumps(prev.get("prices", prev), ensure_ascii=False, default=str)[:800])
        if total is None and (keep or spec.price_cap is not None):
            safe_delete(client, cid)
            return CheckoutResult("rejected", reason=T("无法解析首期价格，拒绝下单"))
        if spec.price_cap is not None and total > spec.price_cap:
            safe_delete(client, cid)
            return CheckoutResult("rejected", reason=T("首期含税 {0:.2f} 超过上限 {1:.2f}", total, spec.price_cap), total=total)
        if not keep:
            safe_delete(client, cid)
            cid = None
        return CheckoutResult("preview", cid=cid, total=total, codes=codes)
    except Exception as e:
        safe_delete(client, cid)
        return CheckoutResult("failed", reason=f"{type(e).__name__}: {e}", rate_limited=RL.is_429(e),
                              retry_after=RL.retry_after_of(e))


def checkout(client, spec: OrderSpec, cid: str, since_iso: str, log: Logger) -> CheckoutResult:
    """POST /checkout. Definite rejection => 'rejected'; uncertain => 'pending' (stop for good)."""
    t0 = time.perf_counter()
    try:
        res = client.post(f"/order/cart/{cid}/checkout", autoPayWithPreferredPaymentMethod=bool(spec.auto_pay),
                          waiveRetractationPeriod=True)
    except DEFINITE_REJECT as e:
        safe_delete(client, cid)
        return CheckoutResult("rejected", reason=f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}")
    except Exception as e:
        log("PENDING_CONFIRM", T("结账请求异常 {0}: {1}；订单状态不确定，任务已停止，正在查询 /me/order", type(e).__name__, e))
        time.sleep(5)
        orders = recent_orders(client, since_iso)
        return CheckoutResult("pending", reason=f"{type(e).__name__}: {e}", recent=orders, cid=cid)
    ms = int((time.perf_counter() - t0) * 1000)
    return CheckoutResult("ordered", orderId=res.get("orderId"), url=res.get("url"),
                          total=first_invoice_total(res), checkout_ms=ms, raw=res)


def wait_paid(client, order_id, tries: int = 30, gap: float = 3.0, stop: threading.Event | None = None) -> str | None:
    st = None
    for _ in range(tries):
        try:
            st = client.get(f"/me/order/{order_id}/status")
        except Exception as e:
            st = f"err:{type(e).__name__}"
        if st in PAID:
            break
        if stop is not None and stop.wait(gap):
            break
        elif stop is None:
            time.sleep(gap)
    return st
