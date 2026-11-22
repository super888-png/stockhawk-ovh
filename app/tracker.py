"""After a real order: follow it on OVH until the server is delivered (or a timeout), then compare the delivered
hardware with the official catalog configuration. Sends 付款通知 / 交付通知 (with a congratulation if the
machine is better than the spec). Read-only GET calls; state lives in the orders table, so it survives restarts."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re

from sqlalchemy import select

from . import ovh_engine as E
from . import specs as S
from .catalog import get_catalog
from .db import SessionLocal
from .models import Order, Task, User, utcnow
from .notify import send as notify_send
from . import fx as FX
from .catalog import cur_symbol
from .i18n import T, TDict, lang_scope, ljoin, tr

log = logging.getLogger("tracker")
TICK = 30                    # s between scans
FAST_FOR = dt.timedelta(hours=2)
FAST, SLOW = dt.timedelta(minutes=2), dt.timedelta(minutes=10)
TIMEOUT = dt.timedelta(days=14)
NO_SERVER_GRACE = dt.timedelta(hours=2)   # order says delivered but no server visible: give up linking after this
PER_TICK = 6
CANCELLED = {"cancelled", "cancelling"}
STATUS_CN = TDict({"cancelled": "已取消", "cancelling": "取消中", "checking": "核查中", "delivered": "已交付", "delivering": "交付中",
             "documentsRequested": "需提交资料", "notPaid": "未付款", "unknown": "未知", "expired": "已过期"})


def _detail(o: Order) -> dict:
    try:
        return json.loads(o.detail or "{}")
    except Exception:
        return {}


def money_line(o: Order) -> str:
    if o.price_with_tax is None:
        return ""
    d = _detail(o)
    code = FX.currency_for(d.get("subsidiary"), code=d.get("currency"))
    c = FX.cny(o.price_with_tax, code, wait=True)
    return T("首期含税：{0}{1:.2f}", cur_symbol(code), o.price_with_tax) + (T("（{0}）", c) if c else "")


def _date(s) -> dt.datetime | None:
    try:
        d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return (d.astimezone(dt.timezone.utc) if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)).replace(tzinfo=None)
    except Exception:
        try:
            return dt.datetime.fromisoformat(str(s)[:10])
        except Exception:
            return None


def _notified(o: Order, what: str) -> bool:
    return what in (o.notified or "").split(",")


def _mark(o: Order, what: str) -> None:
    s = {x for x in (o.notified or "").split(",") if x}
    s.add(what)
    o.notified = ",".join(sorted(s))


def find_server(c, o: Order, taken: set[str]) -> tuple[str | None, str]:
    """Link an order to its dedicated server. -> (serviceName | None, how)."""
    names = c.get("/dedicated/server") or []
    free = [n for n in names if n not in taken]
    # 1) order details carry the service name once delivered
    for did in (c.get(f"/me/order/{o.order_id}/details") or [])[:20]:
        try:
            d = c.get(f"/me/order/{o.order_id}/details/{did}")
        except Exception:
            continue
        dom = (d.get("domain") or "").strip()
        if dom and dom != "*" and dom in free:
            return dom, T("订单明细")
    # 2) a server created after the order, preferring the ordered datacenter
    since = (o.ts or utcnow()) - dt.timedelta(days=1)
    best = None
    for n in free[:60]:
        try:
            si = c.get(f"/dedicated/server/{n}/serviceInfos")
        except Exception:
            continue
        created = _date(si.get("creation"))
        if not created or created < since.replace(hour=0, minute=0, second=0, microsecond=0):
            continue
        try:
            srv = c.get(f"/dedicated/server/{n}")
        except Exception:
            srv = {}
        dc_ok = bool(o.dc) and (srv.get("datacenter") or "").lower().startswith(o.dc.lower()[:3])
        key = (not dc_ok, created)
        if best is None or key < best[0]:
            best = (key, n)
    return (best[1], T("开通时间匹配")) if best else (None, "")


def official_for(o: Order) -> tuple[dict | None, list[dict]]:
    d = _detail(o)
    endpoint, sub = o.endpoint or "ovh-eu", d.get("subsidiary") or "IE"
    cats = []
    for ct in ([d.get("ctype")] if d.get("ctype") else []) + ["eco", "baremetalServers"]:
        if ct and ct not in [x[0] for x in cats]:
            try:
                cats.append((ct, get_catalog(endpoint, sub, ct)))
            except Exception:
                pass
    for _, cat in cats:
        if o.plan_code in cat["plans"]:
            return S.official(cat, o.plan_code, o.memory, o.storage), [c for _, c in cats]
    for _, cat in cats:   # retired plan code -> successor
        off = S.official(cat, o.plan_code, o.memory, o.storage)
        if off:
            return off, [c for _, c in cats]
    return None, [c for _, c in cats]


def check_order(order_id: int) -> dict:
    """One tracking step for one order (blocking). Returns a small result dict for tests/logging."""
    with SessionLocal() as db:
        row = db.execute(select(User.lang).join(Order, Order.user_id == User.id).where(Order.id == order_id)).first()
    with lang_scope(row[0] if row else None):
        return _check_order(order_id)


def _check_order(order_id: int) -> dict:
    from .worker import load_creds
    with SessionLocal() as db:
        o = db.get(Order, order_id)
        if not o or o.kind != "order" or not o.order_id:
            return {"skip": True}
        now = utcnow()
        age = now - (o.ts or now)
        task = db.get(Task, o.task_id) if o.task_id else None
        label = T("任务 #{0} {1}", o.task_id, task.name) if task and task.name else (T("任务 #{0}", o.task_id) if o.task_id else "")
        creds = load_creds(o.user_id, o.endpoint or (task.endpoint if task else "ovh-eu"))
        if not creds:
            o.next_check = now + dt.timedelta(minutes=30)
            db.commit()
            return {"nocreds": True}
        c = E.make_client(o.endpoint or "ovh-eu", *creds, timeout=(5, 20))
        res = {}
        try:
            st = c.get(f"/me/order/{o.order_id}/status")
        except Exception as e:
            log.info("track %s status: %s", o.order_id, type(e).__name__)
            o.next_check = now + SLOW
            db.commit()
            return {"error": type(e).__name__}
        changed = st != o.ovh_status
        o.ovh_status = str(st)
        res["status"] = st
        from .labels import plan_info
        _pi = plan_info(o.endpoint or "ovh-eu", "IE", o.plan_code or "")
        _nm = f"{_pi['name']} [{o.plan_code}]" if _pi["known"] else o.plan_code
        desc = f"{_nm} {E.dc_label(o.dc)}" if o.dc else _nm
        if st in E.PAID and not o.paid_at:
            o.paid_at = now
        if st in E.PAID and not _notified(o, "paid"):
            _mark(o, "paid")
            o.status = "paid" if st != "delivered" else "delivered"
            notify_send(o.user_id, "ORDER_PAID", ljoin("\n", [x for x in (label, desc, money_line(o), T("OVH 状态：{0}", STATUS_CN.get(st, st))) if x]),
                        title=T("订单 #{0} 已付款", o.order_id), link=o.url or None)
            res["notified_paid"] = True
        if st in CANCELLED:
            o.track_state, o.status = "cancelled", "cancelled"
            db.commit()
            return res
        server, how, spec = None, "", None
        if st in ("delivered", "delivering") or st in E.PAID:
            taken = {x for x in db.scalars(select(Order.server_name).where(Order.user_id == o.user_id, Order.id != o.id,
                                                                              Order.server_name != ""))}
            try:
                server, how = find_server(c, o, taken)
            except Exception as e:
                if "granted" in str(e).lower() or "403" in str(e):
                    res["no_server_right"] = True
                else:
                    log.info("track %s servers: %s", o.order_id, type(e).__name__)
            if server:
                try:
                    srv = c.get(f"/dedicated/server/{server}")
                    hw = c.get(f"/dedicated/server/{server}/specifications/hardware")
                except Exception:
                    srv, hw = None, None
                if hw:
                    off, cats = official_for(o)
                    act = S.actual(hw, srv)
                    cmp_ = S.compare(off, act, S.cpu_scores(cats))
                    spec = {"official": off, "actual": act, "server": server, "linkedBy": how, **cmp_,
                            "serverState": (srv or {}).get("state"), "ip": (srv or {}).get("ip")}
        delivered = bool(spec) and (st == "delivered" or (spec.get("serverState") == "ok"))
        if delivered or (st == "delivered" and age > NO_SERVER_GRACE and not spec):
            o.track_state, o.delivered_at, o.status = "delivered", now, "delivered"
            if spec:
                o.server_name, o.spec_json = spec["server"], json.dumps(spec, ensure_ascii=False, default=str)
            if not _notified(o, "delivered"):
                _mark(o, "delivered")
                lines = [x for x in (label, T("{0} · 订单 #{1}", desc, o.order_id), money_line(o)) if x]
                title = T("服务器已交付 {0}", spec['server']) if spec else T("订单 #{0} 已交付", o.order_id)
                if spec:
                    lines += [T("实际配置：{0}", spec['actual']['text'])]
                    if spec.get("official"):
                        lines += [T("官方配置：{0}", spec['official']['text'])]
                    if spec.get("ip"):
                        lines += [T("IP：{0}", spec['ip'])]
                    if spec["lottery"]:
                        lines += [T("恭喜中奖！实际配置比官方更好："), *("  · " + u for u in spec["upgrades"])]
                elif res.get("no_server_right"):
                    lines += [T("当前密钥没有查看服务器的权限，无法对比实际配置（到「订单记录」页按提示重新授权）")]
                notify_send(o.user_id, "DELIVERED", ljoin("\n", lines), title=title, data={"orderId": o.order_id, "spec": spec})
                res["notified_delivered"] = True
            res["delivered"] = True
        elif age > TIMEOUT:
            o.track_state = "timeout"
        else:
            o.next_check = now + (FAST if age < FAST_FOR else SLOW)
        if spec and not o.spec_json:
            o.server_name, o.spec_json = spec["server"], json.dumps(spec, ensure_ascii=False, default=str)
        db.commit()
        res["changed"] = changed
        return res


class Tracker:
    def __init__(self):
        self.task: asyncio.Task | None = None
        self.running: set[int] = set()

    def start(self):
        if not self.task:
            self.task = asyncio.create_task(self._loop(), name="tracker")

    async def stop(self):
        if self.task:
            self.task.cancel()
            self.task = None

    async def _loop(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("tracker tick")
            await asyncio.sleep(TICK)

    async def tick(self) -> int:
        now = utcnow()
        with SessionLocal() as db:
            ids = list(db.scalars(select(Order.id).where(Order.kind == "order", Order.track_state == "tracking",
                                                         (Order.next_check.is_(None)) | (Order.next_check <= now))
                                  .order_by(Order.next_check).limit(PER_TICK)))
        ids = [i for i in ids if i not in self.running]
        for i in ids:
            self.running.add(i)
        try:
            await asyncio.gather(*(asyncio.to_thread(check_order, i) for i in ids), return_exceptions=True)
        finally:
            for i in ids:
                self.running.discard(i)
        return len(ids)


tracker = Tracker()
