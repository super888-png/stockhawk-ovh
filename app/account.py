"""Read-only view of a user's EXISTING OVH account: recent orders and dedicated servers.

Only GET calls. Per (user, endpoint) results are cached for CACHE_TTL seconds and fetched in the background, so
the page never waits on OVH; the browser polls /api/account until the data is in. Within one fetch at most
WORKERS calls run at the same time (polite to the OVH API rate limits).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import ovh.exceptions as oe

from . import fx as FX
from . import ordersum as OS
from . import ovh_engine as E
from .i18n import T, TDict, get_lang, lang_scope, tr

log = logging.getLogger("account")
CACHE_TTL = 300            # s, per user and endpoint
MIN_REFRESH_GAP = 20       # s, manual refresh is ignored while a fetch is younger than this
ORDER_DAYS = 90
ORDER_LIMIT = 25           # newest orders that get full details
SERVER_LIMIT = 50
DETAIL_LIMIT = 24          # order lines per order (OVH lists every item twice: setup + rental)
WORKERS = 4

# full OVH me.order.StatusEnum (+ "expired" defensively); anything else is shown raw
ORDER_STATUS_CN = TDict({"cancelled": "已取消", "cancelling": "取消中", "checking": "核查中", "delivered": "已交付",
                   "delivering": "交付中", "documentsRequested": "需提交资料", "notPaid": "未付款", "unknown": "未知",
                   "expired": "已过期"})
SERVER_STATE_CN = TDict({"ok": "正常", "error": "故障", "hacked": "被入侵", "hackedBlocked": "被入侵·已封锁"})
SERVICE_STATUS_CN = TDict({"ok": "正常", "expired": "已过期", "inCreation": "开通中", "unPaid": "未付款",
                     "pendingDebt": "欠费待付", "autorenewInProgress": "自动续费中", "suspended": "已暂停",
                     "terminated": "已终止", "toRenew": "待续费"})

# OVH payment mean types (me.payment / order payment "paymentType"); unknown values are shown raw
PAY_CN = TDict({"creditCard": "信用卡", "bankAccount": "银行账户", "fidelityAccount": "积分", "ovhAccount": "OVH 余额",
                "credit": "余额", "voucher": "代金券", "mandate": "授权扣款", "debtAccount": "欠款账户", "deposit": "预付款",
                "refund": "退款", "prepaidAccount": "预付账户", "multiple": "多种方式", "cheque": "支票", "transfer": "银行转账",
                "bankTransfer": "银行转账", "alipay": "支付宝", "wechatpay": "微信支付"})


class Denied(Exception):
    """The consumer key is valid but this call is not in its access rules (HTTP 403)."""


def _is_denied(e: Exception) -> bool:
    if isinstance(e, (oe.NotGrantedCall, oe.Forbidden)):
        return True
    if isinstance(e, (oe.InvalidKey, oe.InvalidCredential, oe.NotCredential)):
        return False
    resp = getattr(e, "response", None)
    return isinstance(e, oe.APIError) and getattr(resp, "status_code", 0) == 403


def _get(c, path, **kw):
    try:
        return c.get(path, **kw)
    except Exception as e:
        if _is_denied(e):
            raise Denied(str(e).splitlines()[0] if str(e) else "403") from None
        raise


def _soft(c, path, default=None):
    """Optional sub-resource: a 404 / missing right only blanks this field."""
    try:
        return c.get(path)
    except Exception:
        return default


def _price(p):
    if not isinstance(p, dict):
        return None, None
    return p.get("value"), p.get("text") or (f"{p.get('value')} {p.get('currencyCode', '')}".strip() if p.get("value") is not None else None)


def _size(v):
    """{'unit': 'MB', 'value': 65536} -> GB number; disk sizes may come in GB already."""
    if not isinstance(v, dict) or v.get("value") is None:
        return None
    val, unit = float(v["value"]), (v.get("unit") or "").upper()
    return {"MB": val / 1024, "GB": val, "TB": val * 1000, "B": val / 1024 ** 3}.get(unit, val)


def _fmt_gb(gb):
    if gb is None:
        return None
    if gb >= 1000:
        t = gb / 1000
        return f"{t:.1f}".rstrip("0").rstrip(".") + " TB"
    return f"{gb:.0f} GB"


def _bw(v):
    if not isinstance(v, dict) or v.get("value") is None:
        return None
    unit = (v.get("unit") or "Mbps").replace("Mbps", "Mbps")
    val = v["value"]
    if unit == "Mbps" and val >= 1000:
        return f"{val / 1000:g} Gbps"
    return f"{val:g} {unit}"


# ---------------------------------------------------------------- orders
def fetch_order(c, oid) -> dict:
    o = _soft(c, f"/me/order/{oid}", {}) or {}
    st = _soft(c, f"/me/order/{oid}/status")
    items = []
    for did in (_soft(c, f"/me/order/{oid}/details", []) or [])[:DETAIL_LIMIT]:
        d = _soft(c, f"/me/order/{oid}/details/{did}", {}) or {}
        items.append({"description": d.get("description") or "", "domain": d.get("domain") or "",
                      "quantity": d.get("quantity"), "total": _price(d.get("totalPrice"))[1],
                      "detailType": d.get("detailType") or ""})
    summary = OS.summarize(items)
    if summary.get("dc"):
        summary["dcLabel"] = E.dc_label(summary["dc"])
    pay = _soft(c, f"/me/order/{oid}/payment") if st not in ("notPaid", "cancelled") else None
    v, text = _price(o.get("priceWithTax"))
    return {"orderId": oid, "date": o.get("date"), "expiration": o.get("expirationDate"),
            "amount": v, "amountText": text, "amountExTax": _price(o.get("priceWithoutTax"))[1],
            "amountCny": FX.cny(v, (o.get("priceWithTax") or {}).get("currencyCode") or "EUR"),
            "status": st, "statusCn": ORDER_STATUS_CN.get(st, st or T("未知")), "url": o.get("url"),
            "paid": bool(pay and pay.get("paymentDate")),
            "payment": {"type": pay.get("paymentType"), "date": pay.get("paymentDate")} if pay else None,
            "items": items, "summary": summary}


def fetch_orders(c, pool) -> dict:
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=ORDER_DAYS)).date().isoformat()
    ids = _get(c, "/me/order", **{"date.from": since}) or []
    ids = sorted(ids, reverse=True)
    lang = get_lang()   # pool threads don't inherit the request's language (status texts came out in English)

    def one(oid):
        with lang_scope(lang):
            return fetch_order(c, oid)
    rows = list(pool.map(one, ids[:ORDER_LIMIT]))
    rows.sort(key=lambda r: r.get("date") or "", reverse=True)
    return {"total": len(ids), "shown": len(rows), "days": ORDER_DAYS, "rows": rows}


# ---------------------------------------------------------------- servers
def fetch_server(c, name) -> dict:
    s = _get(c, f"/dedicated/server/{name}") or {}
    si = _soft(c, f"/dedicated/server/{name}/serviceInfos", {}) or {}
    hw = _soft(c, f"/dedicated/server/{name}/specifications/hardware", {}) or {}
    net = _soft(c, f"/dedicated/server/{name}/specifications/network", {}) or {}
    ips = _soft(c, f"/dedicated/server/{name}/ips", []) or []
    disks = []
    for g in hw.get("diskGroups") or []:
        n, size = g.get("numberOfDisks"), _fmt_gb(_size(g.get("diskSize")))
        typ = (g.get("diskType") or "").upper()
        raid = g.get("defaultHardwareRaidType") or g.get("raidController") or ""
        disks.append(" ".join(x for x in (f"{n}×{size}" if n and size else size, typ, f"({raid})" if raid and raid not in ("N/A", "none") else "") if x))
    cpu = hw.get("processorName") or ""
    cores = hw.get("coresPerProcessor") and (hw.get("coresPerProcessor") * (hw.get("numberOfProcessors") or 1))
    threads = hw.get("threadsPerProcessor") and (hw.get("threadsPerProcessor") * (hw.get("numberOfProcessors") or 1))
    bw = net.get("bandwidth") or {}
    renew = si.get("renew") or {}
    if renew.get("deleteAtExpiration"):
        renew_cn = T("到期删除")
    elif renew.get("automatic"):
        renew_cn = T("自动续费") + (T(" · 每 {0} 个月", renew['period']) if renew.get("period") else "")
    elif renew.get("manualPayment") or si:
        renew_cn = T("手动续费")
    else:
        renew_cn = ""
    iam = s.get("iam") or {}
    return {"name": s.get("name") or name, "displayName": iam.get("displayName") or s.get("displayName") or "",
            "reverse": s.get("reverse") or "", "state": s.get("state"), "stateCn": SERVER_STATE_CN.get(s.get("state"), s.get("state") or T("未知")),
            "datacenter": s.get("datacenter") or "", "dcLabel": E.dc_label((s.get("datacenter") or "")[:3].lower()) if s.get("datacenter") else "",
            "os": s.get("os") or "", "ip": s.get("ip") or "", "ips": [x for x in ips if x.split("/")[0] != s.get("ip")][:6], "ipCount": max(len(ips), 1 if s.get("ip") else 0),
            "commercialRange": s.get("commercialRange") or "", "rack": s.get("rack") or "", "monitoring": s.get("monitoring"),
            "linkSpeed": s.get("linkSpeed"), "description": hw.get("description") or "",
            "cpu": cpu, "cores": cores, "threads": threads, "ram": _fmt_gb(_size(hw.get("memorySize"))), "disks": disks,
            "bandwidthOut": _bw(bw.get("OvhToInternet")), "bandwidthIn": _bw(bw.get("InternetToOvh")),
            "bandwidthType": bw.get("type") or "",
            "created": si.get("creation"), "expiration": si.get("expiration"), "serviceStatus": si.get("status"),
            "serviceStatusCn": SERVICE_STATUS_CN.get(si.get("status"), si.get("status") or ""), "renew": renew_cn}


def fetch_servers(c, pool) -> dict:
    names = sorted(_get(c, "/dedicated/server") or [])
    lang = get_lang()

    def one(n):
        try:
            with lang_scope(lang):
                return fetch_server(c, n)
        except Denied:
            raise
        except Exception as e:
            return {"name": n, "error": f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}"}
    rows = list(pool.map(one, names[:SERVER_LIMIT]))
    return {"total": len(names), "shown": len(rows), "rows": rows}


def fetch_account(endpoint: str, creds: tuple[str, str, str]) -> dict:
    """One endpoint, both sections. Each section fails independently (denied / error)."""
    t0 = time.time()
    c = E.make_client(endpoint, *creds, timeout=(5, 20))
    out = {"endpoint": endpoint, "nichandle": None, "orders": None, "servers": None, "denied": [], "errors": {}}
    try:
        me = _get(c, "/me")
        out["nichandle"] = me.get("nichandle")
    except Denied as e:
        out["denied"].append("me"); out["errors"]["me"] = str(e)
    except Exception as e:
        kind = "invalid" if E.is_auth_error(e) else "error"
        out["errors"]["me"] = f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}"
        out["authInvalid"] = kind == "invalid"
        out["ms"] = int((time.time() - t0) * 1000)
        return out
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for key, fn in (("orders", fetch_orders), ("servers", fetch_servers)):
            try:
                out[key] = fn(c, pool)
            except Denied as e:
                out["denied"].append(key); out["errors"][key] = str(e)
            except Exception as e:
                log.warning("account %s %s: %s", endpoint, key, e)
                out["errors"][key] = f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}"
    out["ms"] = int((time.time() - t0) * 1000)
    return out


# ---------------------------------------------------------------- background cache
class AccountCache:
    def __init__(self):
        self.s: dict[tuple[int, str], dict] = {}

    def get(self, user_id: int, endpoint: str, creds, refresh: bool = False) -> dict:
        """Return the cached entry immediately; start a background fetch when missing, stale or refreshed."""
        key = (user_id, endpoint, get_lang())   # cached text is rendered in the reader's language
        e = self.s.setdefault(key, {"data": None, "ts": 0.0, "task": None, "started": 0.0})
        now = time.time()
        running = e["task"] is not None and not e["task"].done()
        stale = now - e["ts"] > CACHE_TTL
        if not running and (e["data"] is None or stale or (refresh and now - e["started"] > MIN_REFRESH_GAP)):
            e["started"] = now
            e["task"] = asyncio.create_task(self._fetch(key, endpoint, creds))
            running = True
        return {"endpoint": endpoint, "loading": running, "data": e["data"],
                "fetchedAt": e["ts"] or None, "age": int(now - e["ts"]) if e["ts"] else None}

    async def _fetch(self, key, endpoint, creds):
        try:
            data = await asyncio.to_thread(fetch_account, endpoint, creds)
        except Exception as ex:  # never let a fetch kill the cache entry
            data = {"endpoint": endpoint, "errors": {"me": f"{type(ex).__name__}: {ex}"}, "denied": []}
        e = self.s[key]
        e["data"], e["ts"] = data, time.time()

    def drop_user(self, user_id: int) -> None:
        for k in [k for k in self.s if k[0] == user_id]:
            self.s.pop(k, None)


cache = AccountCache()
