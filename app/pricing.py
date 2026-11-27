"""Price breakdown for the task form: monthly, setup fee, VAT and first-invoice total, from the public catalog.

OVH bills the first invoice as (rental for the period + one-off installation) x VAT; checked live against
/order/cart/{id}/summary (RISE-1, IE, P1M: (56.99 + 56.99) x 1.23 = 140.20)."""
from __future__ import annotations

from . import ovh_engine as E

# VAT applied by OVH per subsidiary. Outside the EU the catalog price is shown without tax (OVH adds local tax
# if any at checkout), so the rate is 1.0 there.
VAT = {"IE": 1.23, "FR": 1.20, "DE": 1.19, "GB": 1.20, "ES": 1.21, "IT": 1.22, "NL": 1.21, "PL": 1.23,
       "PT": 1.23, "CZ": 1.21, "FI": 1.255, "LT": 1.21, "SN": 1.18, "MA": 1.20, "TN": 1.19}
MONTHS = {"P1M": 1, "P3M": 3, "P6M": 6, "P12M": 12, "P24M": 24}


def vat_for(subsidiary: str) -> float:
    return VAT.get((subsidiary or "").upper(), 1.0)


def _mode_prices(obj: dict | None, mode: str, months: int) -> tuple[float | None, float, int]:
    """(rental for the period, installation, period months) for one plan/addon under a pricing mode."""
    rent = setup = None
    interval = 1
    rows = [x for x in (obj or {}).get("pricings") or [] if x.get("mode") == mode]
    if not rows and mode != "default":
        rows = [x for x in (obj or {}).get("pricings") or [] if x.get("mode") == "default"]
    for x in rows:
        caps = x.get("capacities") or []
        price = E.catalog_price(x.get("price") or 0)
        if "installation" in caps and (x.get("interval") in (0, None) or x.get("intervalUnit") == "none"):
            setup = price if setup is None else setup
        elif "renew" in caps and x.get("interval"):
            if x["interval"] == months or (rent is None and x["interval"] == 1):
                rent, interval = price, x["interval"]
    if rent is None:
        return None, setup or 0.0, months
    if interval != months:          # monthly price x months
        rent = rent * months / interval
    return rent, setup or 0.0, months


def breakdown(cat: dict, plan_code: str, memory: str = "", storage: str = "", bandwidth: str = "",
              extras: list[str] | None = None, duration: str = "P1M", pricing_mode: str = "default",
              subsidiary: str = "IE", vat: float | None = None) -> dict | None:
    plan = cat["plans"].get(plan_code)
    if not plan:
        return None
    months = MONTHS.get(duration, 1)
    mode = pricing_mode if pricing_mode in ("default", "upfront12", "upfront24") else "default"
    if mode.startswith("upfront"):
        months = int(mode[7:])
    vat = vat or vat_for(subsidiary)
    chosen = dict(E.default_addons(plan))                 # family -> default addon code
    for fam, want in (("memory", memory), ("storage", storage)):
        if want:
            code = E.addon_code(plan, fam, want, synthesize=False)
            if code:
                chosen[fam] = code
    if bandwidth:
        chosen["bandwidth"] = bandwidth
    items = []
    rent, setup, _ = _mode_prices(plan, mode, months)
    items.append({"kind": "plan", "code": plan_code, "label": plan.get("invoiceName", plan_code).split(" |")[0],
                  "rent": rent, "setup": setup})
    for fam, code in chosen.items():
        a = cat["addons"].get(code)
        if not a:
            continue
        r, s, _ = _mode_prices(a, mode, months)
        if (r or 0) == 0 and s == 0 and fam not in ("memory", "storage", "bandwidth"):
            continue
        label = (E.short_ram(E.strip_plan_suffix(code, plan_code)) if fam == "memory" else
                 E.short_disk(E.strip_plan_suffix(code, plan_code)) if fam == "storage" else (a.get("invoiceName") or code))
        items.append({"kind": fam, "code": code, "label": label, "rent": r or 0.0, "setup": s})
    for code in extras or []:
        a = cat["addons"].get(code)
        if a:
            r, s, _ = _mode_prices(a, mode, months)
            items.append({"kind": "extra", "code": code, "label": a.get("invoiceName") or code, "rent": r or 0.0, "setup": s})
    if items[0]["rent"] is None:
        return None
    period = round(sum(i["rent"] for i in items), 2)
    setup_total = round(sum(i["setup"] for i in items), 2)
    subtotal = round(period + setup_total, 2)
    total = round(subtotal * vat, 2)
    return {"planCode": plan_code, "name": items[0]["label"], "currency": cat.get("currency", "EUR"),
            "symbol": E_symbol(cat.get("currency", "EUR")), "mode": mode, "months": months,
            "monthly": round(period / months, 2), "period": period, "setup": setup_total, "subtotal": subtotal,
            "vatRate": vat, "tax": round(total - subtotal, 2), "total": total,
            "items": [{**i, "rent": round(i["rent"], 2), "setup": round(i["setup"], 2)} for i in items]}


def E_symbol(code: str) -> str:
    from .catalog import cur_symbol
    return cur_symbol(code)
