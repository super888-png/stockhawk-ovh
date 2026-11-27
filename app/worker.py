"""In-process background worker: one asyncio TaskRunner per enabled task.

Maps the original autobuy_focus.py main loop onto per-task runners:
  poll (shared feed) -> live_stock -> pick_stock -> [fast prebuilt cart | slow preview] -> checkout
  FAIL_LIMIT consecutive failures => BACKOFF; PENDING_CONFIRM => task stopped for good (the
  DB status replaces the old lock files); max_orders reached => DONE.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import threading
import time

from sqlalchemy import delete, select, func

from .catalog import get_catalog, cur_symbol
from .config import DATA_DIR, LOG_KEEP_PER_TASK, MIN_POLL_INTERVAL
from .db import SessionLocal
from .models import Credential, Order, Task, TaskLog, User, task_plans, utcnow
from .fx import cny as fx_cny
from .labels import bw_label, split_invoice
from .notify import IMPORTANT, send as notify_send, send_card as notify_card, mark_sold as notify_sold
from .poller import hub
from .security import dec
from . import ovh_engine as E
from .i18n import T, ljoin, set_lang, tr

log = logging.getLogger("worker")
FAIL_LIMIT, BACKOFF_S = 3, 45
AUTH_EVERY = 600
HEARTBEAT_EVERY = 60   # seconds: a heartbeat line in the live log at least once a minute
KEEPALIVE_EVERY = 15          # s, cheap signed GET keeps the checkout connection warm (original)
KA_TIMEOUT = (3, 5)
RESTART_DELAY, MAX_RESTARTS = 10, 5   # run_autobuy.sh: restart a crashed hunter after 10s
WORKER_CLIENTS = 4            # parallel clients for the slow-path cart build (original used 6)
REGISTRY = os.path.join(DATA_DIR, "prebuilt_carts.json")
_reg_lock = threading.Lock()


class Restartable(Exception):
    """Transient failure of a whole runner; the supervisor loop restarts it."""


def registry_set(task_id: int, cids: list[str]) -> None:
    """Persist this task's prebuilt cart ids (original prebuilt_carts.json) so a crash/kill never leaks them."""
    with _reg_lock:
        try:
            data = json.load(open(REGISTRY))
        except Exception:
            data = {}
        if cids:
            data[str(task_id)] = cids
        else:
            data.pop(str(task_id), None)
        tmp = REGISTRY + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, REGISTRY)
        except Exception:
            pass


def registry_get(task_id: int) -> list[str]:
    with _reg_lock:
        try:
            return list(json.load(open(REGISTRY)).get(str(task_id)) or [])
        except Exception:
            return []


def tlog(task_id: int, marker: str, msg: str = "") -> None:
    with SessionLocal() as db:
        db.add(TaskLog(task_id=task_id, marker=marker, msg=msg[:4000]))
        db.commit()
        n = db.scalar(select(func.count(TaskLog.id)).where(TaskLog.task_id == task_id))
        if n and n > LOG_KEEP_PER_TASK + 200:
            cutoff = db.scalar(select(TaskLog.id).where(TaskLog.task_id == task_id)
                               .order_by(TaskLog.id.desc()).offset(LOG_KEEP_PER_TASK).limit(1))
            if cutoff:
                db.execute(delete(TaskLog).where(TaskLog.task_id == task_id, TaskLog.id <= cutoff))
                db.commit()


def set_status(task_id: int, status: str, text: str = "", enabled: bool | None = None, **fields) -> None:
    with SessionLocal() as db:
        t = db.get(Task, task_id)
        if not t:
            return
        t.status, t.status_text = status, text[:1000]
        if enabled is not None:
            t.enabled = enabled
        for k, v in fields.items():
            setattr(t, k, v)
        db.commit()


def load_creds(user_id: int, endpoint: str) -> tuple[str, str, str] | None:
    with SessionLocal() as db:
        c = db.scalar(select(Credential).where(Credential.user_id == user_id, Credential.endpoint == endpoint))
        if not c:
            return None
        ak, asec, ck = dec(c.app_key_enc), dec(c.app_secret_enc), dec(c.consumer_key_enc)
        return (ak, asec, ck) if ak and asec and ck else None


class TaskRunner:
    def __init__(self, task_id: int):
        self.task_id = task_id
        self.stop_evt = threading.Event()
        self.atask: asyncio.Task | None = None
        self.plans: list[dict] = []
        self.slots_list: list = []
        self.wake: asyncio.Event | None = None
        self.bg: list[asyncio.Task] = []
        self.in_checkout = False
        self.after_checkout = False
        self.state = {"polls": 0, "last_poll": None, "last_ms": 0, "in_stock": False, "phase": "starting",
                      "stock": [], "feed_error": None, "prebuilt": "-"}

    # ------------------------------------------------------------ helpers
    def L(self, marker: str, msg: str = "", *, notify: bool = True, title: str = "", text: str | None = None,
          link: str | None = None, dedupe: str | None = None) -> None:
        tlog(self.task_id, marker, msg)
        if notify and marker in IMPORTANT and marker != "TEST":
            self._notify(marker, text if text is not None else msg, title=title, link=link, dedupe=dedupe)

    @property
    def label(self) -> str:
        return T("任务 #{0}", self.task_id) + (f" {self.t.name}" if getattr(self, "t", None) and self.t.name else "")

    def _notify(self, marker, msg, title="", link=None, dedupe=None):
        default_titles = {"PENDING_CONFIRM": T("结账结果不确定，请人工确认"), "AUTH_FAIL": T("OVH 密钥被拒绝"), "DONE": T("任务已完成"),
                          "ORDER_OVER_CAP": T("订单金额超过上限"), "ORDER_UNPAID": T("订单未付款"), "STOCK_FOUND": T("匹配到库存"),
                          "ORDER_PAID": T("订单已付款"), "ORDER_PLACED": T("订单已提交")}
        title = title or default_titles.get(marker, marker)
        threading.Thread(target=notify_send, args=(self.t.user_id, marker, self.label + "\n" + msg),
                         kwargs={"title": title, "link": link, "dedupe_key": dedupe}, daemon=True).start()

    # ------------------------------------------------------------ restock cards (Telegram: send, strike through when sold out)
    def _price(self, p: dict, pick: dict) -> tuple[float | None, float | None]:
        """Monthly ex-VAT price and setup fee of the picked config (base plan + RAM/disk addons), like 实时库存."""
        obj = p["obj"]
        mo, setup = E.monthly_and_setup(obj)
        if mo is None:
            return None, None
        mo, setup = mo, setup or 0.0
        for fam in ("memory", "storage"):
            code = E.addon_code(obj, fam, pick.get(fam) or "", synthesize=False)
            a = p["addons"].get(code or "")
            if a:
                m2, s2 = E.monthly_and_setup(a)
                mo += m2 or 0.0
                setup += s2 or 0.0
        return round(mo, 2), round(setup, 2)

    def _send_card(self, p: dict, pick: dict, mode: str, dedupe: str, extra: dict | None = None) -> None:
        t = self.t
        mo, setup = self._price(p, pick)
        dc = pick.get("dc") or ""
        others = [d for d, _a in pick.get("datacenters", []) if d != dc and d in p["dcs"]]
        self.cards = getattr(self, "cards", 0) + 1
        ref = f"{self.task_id}:{p.get('key') or p['planCode']}:{self.cards}:{int(time.time())}"
        info = {"name": p["label"], "cpu": p.get("cpu") or "", "memory_code": pick.get("memory") or "",
                "storage_code": pick.get("storage") or "", "dc": dc,
                "availability": pick.get("availability") or "", "sym": self.sym, "price": mo, "setup": setup,
                "cny_price": fx_cny(mo, self.cur) if mo is not None else "", "mode": mode, "task": self.label,
                "others_codes": others[:8], "ts": time.time(),
                "order": {"endpoint": t.endpoint, "subsidiary": t.subsidiary, "plan_code": p["planCode"],
                          "catalog_type": p.get("ctype") or t.catalog_type, "dc": dc,
                          "region": E.region_for(dc, E.allowed_regions(p["obj"])) if dc else "",
                          "duration": t.duration, "pricing_mode": t.pricing_mode, "os_code": t.os_code},
                **(extra or {})}
        p["card"] = ref
        threading.Thread(target=notify_card, args=(t.user_id, info), kwargs={"ref": ref, "dedupe_key": dedupe}, daemon=True).start()

    def _cards_sold(self, plans: list[dict], picks: list, now: float) -> None:
        """A plan whose card is open and no longer has matching stock: edit that Telegram message (strikethrough)."""
        hit = {id(p) for p, _pick in picks}
        for p in plans:
            if p.get("card") and id(p) not in hit and p["feed"] and p["feed"].data is not None:
                ref, p["card"] = p["card"], None
                threading.Thread(target=notify_sold, args=(ref, now), daemon=True).start()

    def record_order(self, kind: str, pick: dict, **kw) -> int:
        detail = {**(kw.get("detail") or {}), "subsidiary": self.t.subsidiary, "ctype": kw.get("ctype") or "",
                  "payMode": self.t.pay_mode,
                  "currency": getattr(self, "cur", "EUR")}
        tracked = kind == "order" and bool(kw.get("order_id"))
        with SessionLocal() as db:
            o = Order(user_id=self.t.user_id, task_id=self.task_id, kind=kind, plan_code=kw.get("plan") or self.t.plan_code,
                      dc=pick.get("dc", ""), memory=pick.get("memory", ""), storage=pick.get("storage", ""),
                      order_id=str(kw.get("order_id") or ""), url=kw.get("url") or "",
                      price_with_tax=kw.get("total"), status=kw.get("status") or "",
                      detail=json.dumps(detail, ensure_ascii=False, default=str)[:8000], endpoint=self.t.endpoint,
                      track_state="tracking" if tracked else "",
                      next_check=utcnow() + dt.timedelta(seconds=90) if tracked else None)
            db.add(o)
            db.commit()
            return o.id

    # ------------------------------------------------------------ main
    async def _cleanup(self):
        for p in getattr(self, "plans", []):
            hub.unsubscribe(self.task_id, self.t.endpoint, p["planCode"])
        self.plans = []
        for sl in getattr(self, "slots_list", []):
            await asyncio.to_thread(sl.delete_all)
        self.slots_list = []
        for bg in getattr(self, "bg", []):
            bg.cancel()
        self.bg = []

    async def run(self):
        """Supervisor (run_autobuy.sh): a crash restarts the hunter after 10s, a config error (RuntimeError)
        stops it, and an uncertain checkout never restarts (handled inside _run as pending_confirm)."""
        restarts = 0
        with SessionLocal() as db:   # task logs / status text are written in the owner's panel language
            row = db.execute(select(User.lang).join(Task, Task.user_id == User.id).where(Task.id == self.task_id)).first()
        set_lang(row[0] if row else None)
        try:
            while True:
                try:
                    await self._run()
                    return
                except asyncio.CancelledError:
                    raise
                except RuntimeError as e:
                    log.warning("task %s stopped: %s", self.task_id, e)
                    tlog(self.task_id, "ERROR", T("任务停止: {0}", e))
                    set_status(self.task_id, "error", str(e), enabled=False)
                    if getattr(self, "t", None):
                        self._notify("TASK_ERROR", T("任务因配置问题停止：{0}", e), title=T("任务已停止"))
                    return
                except Exception as e:
                    log.exception("task %s crashed", self.task_id)
                    if self.in_checkout or self.after_checkout:
                        tlog(self.task_id, "PENDING_CONFIRM", T("结账后任务异常 {0}: {1}；不会自动重启，请人工确认订单", type(e).__name__, e))
                        set_status(self.task_id, "pending_confirm", T("结账后异常，请人工确认"), enabled=False)
                        return
                    await self._cleanup()
                    restarts += 1
                    if self.stop_evt.is_set() or restarts > MAX_RESTARTS:
                        tlog(self.task_id, "ERROR", T("任务异常退出: {0}: {1}", type(e).__name__, e))
                        set_status(self.task_id, "error", f"{type(e).__name__}: {e}", enabled=False)
                        if getattr(self, "t", None) and not self.stop_evt.is_set():
                            self._notify("TASK_ERROR", T("任务多次异常后停止：{0}", type(e).__name__), title=T("任务已停止"))
                        return
                    tlog(self.task_id, "ERROR", T("任务异常 {0}: {1}；{2}s 后自动重启（第 {3}/{4} 次）", type(e).__name__, e, RESTART_DELAY, restarts, MAX_RESTARTS))
                    set_status(self.task_id, "running", T("异常重启中（{0}/{1}）", restarts, MAX_RESTARTS))
                    if await asyncio.to_thread(self.stop_evt.wait, RESTART_DELAY):
                        return
        finally:
            self.stop_evt.set()
            await self._cleanup()
            manager.runners.pop(self.task_id, None)

    async def _run(self):
        with SessionLocal() as db:
            self.t = t = db.get(Task, self.task_id)
            db.expunge(t)
        if t.orders_done >= t.max_orders:  # original: lock present at startup => do not buy
            self.L("DONE", T("已下单 {0}/{1}，不再下单", t.orders_done, t.max_orders))
            set_status(self.task_id, "done", T("完成：已下单 {0}", t.orders_done), enabled=False)
            return
        interval = max(MIN_POLL_INTERVAL, float(t.poll_interval or MIN_POLL_INTERVAL))
        cfg = task_plans(t)
        if not cfg:
            raise RuntimeError(T("任务没有选择型号"))
        try:
            types = json.loads(t.plan_configs or "{}")
        except Exception:
            types = {}
        user_dcs = [d for d in (t.datacenters or "").split(",") if d]
        plans: list[dict] = []
        self.sym, self.cur = "€", "EUR"
        dup = {pc for pc in (c["planCode"] for c in cfg) if sum(1 for x in cfg if x["planCode"] == pc) > 1}
        for idx, c in enumerate(cfg):
            ctype = c.get("type") or (types.get(c["planCode"]) or {}).get("type") or t.catalog_type
            cat = await asyncio.to_thread(get_catalog, t.endpoint, t.subsidiary, ctype)
            plan_obj = cat["plans"].get(c["planCode"])
            if not plan_obj:
                raise RuntimeError(T("目录中没有 planCode {0}（{1}/{2}/{3}）", c['planCode'], t.endpoint, t.subsidiary, ctype))
            self.cur = cat.get("currency", "EUR") or "EUR"
            self.sym = cur_symbol(self.cur)
            allowed = E.allowed_dcs(plan_obj)
            want = c["dcs"] if c.get("dcs") is not None else user_dcs   # v1.9 items carry their own datacenters
            dcs = [d for d in want if d in allowed] if want else allowed
            if want and not dcs:
                self.L("INFO", T("{0} 不提供所选机房 {1}，该型号将被跳过", c['planCode'], ','.join(want)))
                continue
            spec = E.OrderSpec(subsidiary=t.subsidiary, catalog_type=ctype, plan_obj=plan_obj, os_code=t.os_code,
                               duration=t.duration, pricing_mode=t.pricing_mode, quantity=t.quantity,
                               bandwidth=c["bandwidth"], extra_addons=(t.extra_addons or "").split(",") if len(cfg) == 1 else [],
                               price_cap=t.price_cap, auto_pay=t.auto_pay, vat_rate=t.vat_rate,
                               addon_pricing={k: v.get("pricings") for k, v in cat["addons"].items()})
            blocked = ""
            if c["memory"] or c["storage"]:
                ok, why = E.orderable(plan_obj, c["memory"], c["storage"])
                if not ok:
                    # listed by the availability API (so it shows "in stock") but OVH sells no such addon: keep
                    # monitoring with a clear message instead of stopping the whole task
                    blocked = why
                    self.L("WARN", T("{0} [{1}] 所选配置无法下单：{2}。该型号只监控、不会下单；想下单请把内存/硬盘改成目录里的选项（或「任意」）", split_invoice(plan_obj.get('invoiceName') or '')[0] or c['label'] or c['planCode'], c['planCode'], why))
                elif c["memory"] and c["storage"]:
                    spec.addon_codes(c["memory"], c["storage"])  # refuse unknown RAM/disk up-front
            plans.append({**c, "label": split_invoice(plan_obj.get("invoiceName") or "")[0] or c["label"] or c["planCode"],
                          "cpu": split_invoice(plan_obj.get("invoiceName") or "")[1],
                          "obj": plan_obj, "spec": spec, "dcs": dcs, "slots": None, "feed": None, "blocked": blocked,
                          "stock": [], "nomatch": "", "ctype": ctype, "addons": cat.get("addons") or {}, "card": None,
                          # identity of this selection item (same planCode may be selected with other configs / DCs)
                          "key": f"{c['planCode']}#{idx + 1}" if c["planCode"] in dup else c["planCode"],
                          "last_sig": None, "last_preview_sig": None})
        if not plans:
            raise RuntimeError(T("所选型号都不提供所选机房"))

        # 仅提醒 / notify-only: never load keys, never talk to the OVH account (availability is public)
        creds = None if t.notify_only else load_creds(t.user_id, t.endpoint)
        client = E.make_client(t.endpoint, *creds, timeout=(5, 30)) if creds else None
        if not t.dry_run and not client:
            raise RuntimeError(T("真实下单需要先在「OVH 密钥」页保存 {0} 的完整密钥（含 Consumer Key）", t.endpoint))
        if client:
            try:
                me = await asyncio.to_thread(client.get, "/me")
                self.L("AUTH_OK", T("已登录 OVH 账号 {0}（子公司 {1}）", me.get('nichandle'), me.get('ovhSubsidiary')))
            except Exception as e:
                if not E.is_auth_error(e):
                    self.L("AUTH_SLOW", T("OVH 鉴权检查网络错误 {0}: {1}", type(e).__name__, e))
                elif not t.dry_run:
                    raise RuntimeError(T("OVH 密钥被拒绝: {0}: {1}", type(e).__name__, e))
                else:
                    self.L("AUTH_FAIL", T("OVH 密钥无效（{0}: {1}）；DRY-RUN 继续，只监控库存、不预览购物车", type(e).__name__, str(e).splitlines()[0]))
                    client = None

        mode = T("仅提醒（只发 Telegram，不碰 OVH 账号）") if t.notify_only else T("只预览（不下单）") if t.dry_run else (T("下单并自动付款") if t.auto_pay else T("只下单不付款（需手动付款）"))
        desc_plans = T("；").join(
            T("#{0} {1} [{2}] 机房 {3} ", i + 1, p['label'], p['planCode'], ','.join(p['dcs']))
            + (T("内存 {0} · 硬盘 {1}", E.short_ram(p['memory']) if p['memory'] else T("任意"), E.short_disk(p['storage']) if p['storage'] else T("任意"))
               + (T(" · 带宽 {0}", bw_label(p['bandwidth'])) if p.get('bandwidth') else "") if (p['memory'] or p['storage'] or p.get('bandwidth'))
               else (T("任意配置") if t.any_config else T("仅基础配置")))
            for i, p in enumerate(plans))
        if t.notify_only:
            self.L("START", T("{0}/{1} 共 {2} 个型号（按优先级）：{3}；间隔 {4:g}s 模式 {5}；提醒冷却 {6} 分钟", t.endpoint, t.subsidiary,
                              len(plans), desc_plans, interval, mode, t.alert_cooldown or 0))
        else:
            self.L("START", T("{0}/{1} 共 {2} 个型号（按优先级）：{3}；间隔 {4:g}s 模式 {5} 已下单 {6}/{7}", t.endpoint, t.subsidiary, len(plans), desc_plans, interval, mode, t.orders_done, t.max_orders)
                   + (T(" 首期上限 {0}{1}", self.sym, t.price_cap) if t.price_cap is not None else ""))
        set_status(self.task_id, "running", T("监控中"))

        workers = None
        if client and not t.dry_run:
            workers = [E.make_client(t.endpoint, *creds, timeout=(5, 30)) for _ in range(WORKER_CLIENTS)]
            self.bg.append(asyncio.create_task(self._auth_loop(creds)))
            old = registry_get(self.task_id)
            if old:  # prebuilt carts left behind by a crash / container kill
                cl2 = E.make_client(t.endpoint, *creds, timeout=(5, 15))
                for cid in old:
                    await asyncio.to_thread(E.safe_delete, cl2, cid)
                registry_set(self.task_id, [])
                self.L("CARTS", T("已删除上次遗留的 {0} 个预建购物车", len(old)))

        if t.prebuild and not t.dry_run and client:
            def _persist():
                registry_set(self.task_id, [c for sl in self.slots_list for c in sl.cart_ids()])
            for p in plans:
                if p["memory"] and p["storage"]:
                    p["slots"] = E.Slots(E.make_client(t.endpoint, *creds, timeout=(5, 15)), p["spec"],
                                         p["memory"], p["storage"], p["dcs"], self.L, on_change=_persist)
            n = sum(1 for p in plans if p["slots"])
            if n:
                self.slots_list = [p["slots"] for p in plans if p["slots"]]
                self.bg.append(asyncio.create_task(self._maintain_slots()))
                self.L("CARTS", T("快速通道：{0} 个型号预建购物车", n))
            if n < len(plans):
                self.L("CARTS", T("未同时指定内存和硬盘的型号走普通路径（预览后下单）"))

        since_iso = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=2)).isoformat(timespec="seconds")
        self.wake = asyncio.Event()
        for p in plans:
            p["feed"] = hub.subscribe(self.task_id, t.endpoint, p["planCode"], interval, self.wake)
        self.plans = plans
        last_handled, last_ka, fails, backoff_until, last_flush, last_hb = 0.0, time.time(), 0, 0.0, 0.0, 0.0
        orders_done = t.orders_done

        while not self.stop_evt.is_set():
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=max(30.0, interval * 3))
            except asyncio.TimeoutError:
                pass
            self.wake.clear()
            if self.stop_evt.is_set():
                break
            try:
                now = time.time()
                if now - last_handled < interval - 0.5:
                    continue  # feeds run faster for another subscriber; respect this task's own interval
                last_handled = now
                polls = self.state["polls"] + 1
                self.state.update(polls=polls, last_poll=now, phase="polling",
                                  last_ms=max((p["feed"].last_ms for p in plans), default=0))
                stock_view, picks, errors = [], [], []
                shown = set()   # a planCode selected twice is listed / logged once per poll
                waiting = False  # a higher-priority plan has no data yet
                for p in plans:
                    feed = p["feed"]
                    if feed.data is None and not feed.error:
                        waiting = True  # first response for this plan not in yet
                        continue
                    first = p["planCode"] not in shown
                    shown.add(p["planCode"])
                    if feed.error:
                        if first:
                            errors.append(f"{p['planCode']}: {feed.error}")
                        if feed.data is None:
                            continue
                    stock = E.live_stock(p["planCode"], p["obj"], feed.data)
                    for r in (stock if first else []):
                        stock_view.append({"plan": p["planCode"], "label": p["label"], "memory": r["memory"],
                                           "storage": r["storage"], "ramL": E.short_ram(r["memory"]),
                                           "diskL": E.short_disk(r["storage"]), "dcs": r["datacenters"],
                                           "default": r["is_default"]})
                    sig = E.stock_signature(stock)
                    if sig != p["last_sig"] and first:
                        if not stock:
                            self.L("NO_STOCK", T("{0} [{1}] 当前无货", p['label'], p['planCode'])
                                               + ("" if p["last_sig"] is None else T("（已售罄）")))
                        else:
                            self.L("STOCK", T("{0} [{1}] 库存变化:\n", p['label'], p['planCode'])
                                   + "\n".join("  " + E.format_row(p["obj"], r) for r in stock))
                    p["last_sig"] = sig
                    p["sig"] = sig
                    p["stock"] = stock
                    pick = None if p["blocked"] else E.pick_stock(stock, p["dcs"], t.any_config, p["memory"] or None,
                                                                 p["storage"] or None)
                    nomatch = "" if pick else E.explain_no_match(stock, p["dcs"], t.any_config, p["memory"] or None,
                                                                  p["storage"] or None)
                    if nomatch and nomatch != p["nomatch"]:
                        self.L("NO_MATCH", f"{p['label']} [{p['planCode']}] {nomatch}")
                    p["nomatch"] = nomatch
                    if pick and not waiting:
                        # the original fetched every plan in one batch per poll; never let a lower-priority plan buy
                        # while a higher-priority plan's first availability answer is still outstanding
                        picks.append((p, pick))
                self.state["stock"] = stock_view
                self.state["feed_error"] = "; ".join(errors) or None
                self.state["in_stock"] = bool(picks)
                if errors and (polls == 1 or polls % 10 == 0):
                    self.L("ERROR", T("查询库存出错: ") + "; ".join(errors))
                slots_all = [p["slots"] for p in plans if p["slots"]]
                if slots_all:
                    self.state["prebuilt"] = " ".join(s.summary() for s in slots_all)
                if now - last_flush > 15:
                    last_flush = now
                    set_status(self.task_id, "running",
                               T("有货 {0} {1}", picks[0][0]['label'], picks[0][1]['dc']) if picks else
                               (T("监控中（有货，但不是所选配置）") if any(p["nomatch"] for p in plans) else T("监控中（无匹配库存）")),
                               polls=t.polls + polls, last_poll_at=utcnow())
                if polls == 1 or now - last_hb >= HEARTBEAT_EVERY:
                    last_hb = now
                    hint = "" if picks else "".join(T("\n  {0}：{1}", p['label'], p['nomatch']) for p in plans if p["nomatch"])
                    self.L("HEARTBEAT", T("已轮询 {0} 次 · {1} · ", polls, T("有匹配库存") if picks else T("无匹配库存")) + " ".join(
                        f"{p['planCode']}:{p['feed'].last_ms}ms" for p in plans) + hint)
                self._cards_sold(plans, picks, now)
                if t.notify_only:
                    self._notify_only(plans, picks, now)
                    continue
                if not picks:
                    if workers and now - last_ka > KEEPALIVE_EVERY:
                        last_ka = now
                        await asyncio.to_thread(self._keepalive, client)
                    continue
                p, pick = picks[0]  # highest-priority plan with matching stock
                spec = p["spec"]
                if p["slots"] and not t.dry_run and not p["slots"].ready(pick["dc"]):
                    have = dict(pick["datacenters"])
                    alt = next((d for d in p["dcs"] if d in have and p["slots"].ready(d)), None)
                    if alt:  # autobuy_focus: next((d for d in order if slots.ready(d)), order[0])
                        pick = {**pick, "dc": alt, "availability": have[alt]}
                desc = (f"{p['label']}{(' · ' + p['cpu']) if p.get('cpu') else ''} [{p['planCode']}] {E.dc_label(pick['dc'])} {pick['availability']} "
                        f"{E.short_ram(pick['memory'])} / {E.short_disk(pick['storage'])}")

                # ---------------- dry run: notify + (with keys) preview once per stock change
                if t.dry_run:
                    if p["sig"] != p["last_preview_sig"]:
                        p["last_preview_sig"] = p["sig"]
                        self.L("STOCK_FOUND", T("[只预览] 匹配到库存: {0}（不会下单）", desc), notify=False)
                        price_line = ""
                        if client:
                            self.state["phase"] = "previewing"
                            r = await asyncio.to_thread(E.preview_cart, client, spec, pick, self.L, False)
                            if r.outcome == "preview":
                                self.L("PREVIEW_OK", T("[只预览] {0} 购物车可建，首期含税 {1}，已删除购物车", p['label'], E.fmt_price(r.info.get('total'), self.sym)), notify=False)
                                price_line = (T("\n购物车预览首期含税 {0}", E.fmt_price(r.info.get('total'), self.sym))
                                              + (T("（{0}）", c) if (c := fx_cny(r.info.get('total'), self.cur)) else ""))
                                self.record_order("preview", pick, plan=p["planCode"], total=r.info.get("total"),
                                                  status="dry-run", detail={"codes": r.info.get("codes")}, ctype=spec.catalog_type)
                            else:
                                self.L("PREVIEW_FAIL", T("[只预览] 预览失败: {0}", r.info.get('reason')))
                        self._send_card(p, pick, T("只预览，不会下单"), f"{self.task_id}:{p.get('key') or p['planCode']}",
                                        {"cart": price_line})
                    continue

                # ---------------- real buy
                if now < backoff_until:
                    continue
                if p["sig"] != p["last_preview_sig"]:
                    p["last_preview_sig"] = p["sig"]
                    self.L("STOCK_FOUND", T("匹配到库存，开始下单: {0}", desc), notify=False)
                    self._send_card(p, pick, T("正在下单（{0}）", mode), f"{self.task_id}:{p.get('key') or p['planCode']}")
                self.state["phase"] = "ordering"
                slot, blocked = (None, None)
                if p["slots"]:
                    slot, blocked = await asyncio.to_thread(p["slots"].prepare_checkout, pick["dc"])
                if blocked:
                    # OVH with-tax total is over the cap or unreadable: do not build another cart and do not POST.
                    self.L("ORDER_FAIL", T("{0} 结账前失败: {1}", desc, blocked))
                    fails += 1
                    if fails >= FAIL_LIMIT:
                        self.L("BACKOFF", T("连续 {0} 次失败，暂停 {1}s", fails, BACKOFF_S))
                        fails, backoff_until = 0, time.time() + BACKOFF_S
                    continue
                if slot:
                    shown = slot["price"] if slot.get("price") is not None else "?"
                    self.L("ORDER_TRY", T("快速通道：使用预建购物车 {0}（OVH 首期含税 {1}{2}）直接结账", slot['cid'], self.sym, shown))
                    cid = slot["cid"]
                else:
                    r = await asyncio.to_thread(E.preview_cart, client, spec, pick, self.L, True, workers)
                    if r.outcome != "preview":
                        self.L("ORDER_FAIL", T("{0} 结账前失败: {1}", desc, r.info.get('reason')))
                        fails += 1
                        if fails >= FAIL_LIMIT:
                            self.L("BACKOFF", T("连续 {0} 次失败，暂停 {1}s", fails, BACKOFF_S))
                            fails, backoff_until = 0, time.time() + BACKOFF_S
                        continue
                    cid = r.info["cid"]
                if self.stop_evt.is_set() or orders_done >= t.max_orders:
                    # original do_checkout(): group lock re-checked right before POST /checkout
                    await asyncio.to_thread(E.safe_delete, client, cid)
                    self.L("INFO", T("任务已停止/已达上限，放弃本次结账并删除购物车"))
                    break
                self.in_checkout = True
                set_status(self.task_id, "checkout", T("结账请求进行中"))
                try:
                    res = await asyncio.to_thread(E.checkout, client, spec, cid, since_iso, self.L)
                    self.after_checkout = res.outcome in ("ordered", "pending")
                finally:
                    self.in_checkout = False
                if res.outcome == "rejected":
                    set_status(self.task_id, "running", T("监控中"))
                    self.L("ORDER_FAIL", T("{0} 结账被 OVH 拒绝（未生成订单）: {1}", desc, res.info.get('reason')))
                    fails += 1
                    if fails >= FAIL_LIMIT:
                        self.L("BACKOFF", T("连续 {0} 次失败，暂停 {1}s", fails, BACKOFF_S))
                        fails, backoff_until = 0, time.time() + BACKOFF_S
                    continue
                if res.outcome == "pending":
                    self.record_order("pending", pick, plan=p["planCode"], status="pending-confirm",
                                      detail={"error": res.info.get("reason"), "recentOrders": res.info.get("recent")})
                    self.L("PENDING_CONFIRM", T("最近订单: ") + json.dumps(res.info.get("recent"), ensure_ascii=False, default=str)[:1500]
                           + T("\n请到 OVH 控制台确认是否已下单。任务已永久停止，确认后可手动重新启动。"))
                    set_status(self.task_id, "pending_confirm", T("结账结果不确定，请人工确认"), enabled=False)
                    return
                fails = 0
                orders_done += 1
                oid, url, total = res.info.get("orderId"), res.info.get("url"), res.info.get("total")
                set_status(self.task_id, "running", T("已下单 {0}/{1}", orders_done, t.max_orders), orders_done=orders_done)
                order_row = self.record_order("order", pick, plan=p["planCode"], order_id=oid, url=url, total=total,
                                              status="ordered" if t.auto_pay else "unpaid", ctype=spec.catalog_type,
                                              detail={k: v for k, v in res.info.items() if k != "raw"})
                cny = T("（{0}）", c) if (c := fx_cny(total, self.cur)) else ""
                self.L("ORDER_PLACED", T("{0} orderId={1} 首期含税 {2} 结账耗时 {3}ms 链接 {4}", desc, oid, E.fmt_price(total, self.sym), res.info.get('checkout_ms'), url),
                       title=T("订单已提交 #{0}", oid), link=url,
                       text=ljoin("\n", [desc, T("首期含税 {0}{1}", E.fmt_price(total, self.sym), cny),
                                        T("付款方式：自动扣款中…") if t.auto_pay else T("付款方式：只下单不付款，请尽快到 OVH 付款（点下方链接）"),
                                        T("本任务已下单 {0}/{1}", orders_done, t.max_orders)]))
                if t.price_cap is not None and (total is None or total > t.price_cap):
                    self.L("ORDER_OVER_CAP", T("订单 {0} 首期 {1} 对比上限 {2}，请检查", oid, total, t.price_cap))
                if t.auto_pay and oid:
                    st = await asyncio.to_thread(E.wait_paid, client, oid, 30, 3.0, None)
                    with SessionLocal() as db:
                        o = db.get(Order, order_row)
                        if o:
                            o.ovh_status = str(st or "")
                            o.status = "paid" if st in E.PAID else "unpaid"
                            if st in E.PAID:
                                o.paid_at = utcnow()
                                o.notified = "paid"
                            db.commit()
                    if st in E.PAID:
                        self.L("ORDER_PAID", T("订单 {0} 已扣款，状态 {1}", oid, st), title=T("订单 #{0} 已付款", oid),
                               text=T("{0}\n首期含税 {1}{2}\n自动扣款成功，等待 OVH 交付（交付后会再通知你）", desc, E.fmt_price(total, self.sym), cny))
                    else:
                        self.L("ORDER_UNPAID", T("订单 {0} 状态 {1}，自动扣款未确认，请立即手动付款: {2}", oid, st, url),
                               title=T("订单 #{0} 自动扣款未成功", oid), link=url,
                               text=T("{0}\nOVH 状态 {1}，请立即到 OVH 手动付款", desc, st))
                else:
                    self.L("ORDER_UNPAID", T("只下单不付款：订单 {0} 已提交，请尽快到 OVH 手动付款: {1}", oid, url), notify=False)
                if orders_done >= t.max_orders:  # max_orders counts across all plans of this task
                    self.L("DONE", T("已达到最大下单数 {0}，任务结束", t.max_orders))
                    set_status(self.task_id, "done", T("完成：已下单 {0}", orders_done), enabled=False)
                    return
                p["last_preview_sig"] = None
                self.after_checkout = False
            except Exception as e:
                if self.in_checkout or self.after_checkout:
                    # never retry after a checkout was sent: treat like the original exit-3 rule
                    self.L("PENDING_CONFIRM", T("结账后处理出错 {0}: {1}；为避免重复下单任务已永久停止，请到 OVH 控制台确认订单", type(e).__name__, e))
                    set_status(self.task_id, "pending_confirm", T("结账后出错，请人工确认"), enabled=False)
                    return
                log.exception("task %s poll error", self.task_id)
                self.L("ERROR", T("本轮处理出错 {0}: {1}（继续监控）", type(e).__name__, e))
                await asyncio.sleep(1)
        set_status(self.task_id, "stopped", T("已停止"), polls=t.polls + self.state["polls"])
        tlog(self.task_id, "STOPPED", T("任务已停止"))

    def _notify_only(self, plans: list[dict], picks: list, now: float) -> None:
        """仅提醒: alert once when a plan's matching stock appears; after it sells out and comes back, alert again,
        but never more often than the task's cooldown. Every matching plan alerts (not only the top priority one)."""
        t = self.t
        cd = max(0, int(t.alert_cooldown or 0)) * 60
        hit = {id(p): pick for p, pick in picks}
        for p in plans:
            if p["feed"].data is None:
                continue
            pick = hit.get(id(p))
            if not pick:
                if p.get("alerted"):
                    self.L("INFO", T("{0} [{1}] 已无匹配库存；再次有货时会重新提醒", p['label'], p['planCode']))
                p["alerted"] = p["cd_logged"] = False
                continue
            if p.get("alerted"):
                continue   # still in stock since the last alert
            wait = p.get("last_alert", 0) + cd - now
            if wait > 0:
                if not p.get("cd_logged"):
                    p["cd_logged"] = True
                    self.L("INFO", T("{0} [{1}] 又有货了，但距上次提醒不到 {2} 分钟（冷却），约 {3} 分钟后仍有货会再提醒",
                                     p['label'], p['planCode'], t.alert_cooldown, max(1, round(wait / 60))))
                continue
            p["alerted"], p["cd_logged"], p["last_alert"] = True, False, now
            p["alerts"] = p.get("alerts", 0) + 1
            desc = (f"{p['label']}{(' · ' + p['cpu']) if p.get('cpu') else ''} [{p['planCode']}] {E.dc_label(pick['dc'])} {pick['availability']} "
                    f"{E.short_ram(pick['memory'])} / {E.short_disk(pick['storage'])}")
            self.L("STOCK_FOUND", T("[仅提醒] 匹配到库存: {0}", desc), notify=False)
            # unique dedupe key: this task's own cooldown (above) replaces the global 有货 debounce
            self._send_card(p, pick, T("仅提醒"), f"notify:{self.task_id}:{p.get('key') or p['planCode']}:{p['alerts']}:{int(now)}")

    def _keepalive(self, client) -> None:
        old = getattr(client, "_timeout", None)
        try:
            if old is not None:
                client._timeout = KA_TIMEOUT
            client.get("/auth/time")
        except Exception:
            pass
        finally:
            if old is not None:
                client._timeout = old

    async def _auth_loop(self, creds) -> None:
        """Credential check every AUTH_EVERY s on its own client, so a slow OVH API never pauses polling
        (original auth_loop thread). Network errors are retried once (AUTH_SLOW); only a rejection is AUTH_FAIL."""
        c = None
        while not await asyncio.to_thread(self.stop_evt.wait, AUTH_EVERY):
            for attempt in (1, 2):
                try:
                    if c is None:
                        c = E.make_client(self.t.endpoint, *creds, timeout=(5, 10))
                    await asyncio.to_thread(c.get, "/me")
                    break
                except Exception as e:
                    if E.is_auth_error(e):
                        self.L("AUTH_FAIL", T("API 密钥被拒绝: {0}: {1}", type(e).__name__, e))
                        break
                    c = None
                    if attempt == 2:
                        self.L("AUTH_SLOW", T("鉴权检查两次超时/网络错误（{0}），不影响库存轮询", type(e).__name__))
                    elif await asyncio.to_thread(self.stop_evt.wait, 5):
                        return

    async def _maintain_slots(self):
        while not self.stop_evt.is_set():
            try:
                for sl in self.slots_list:
                    await asyncio.to_thread(sl.maintain_once, self.stop_evt)
            except Exception as e:
                self.L("ERROR", T("预建购物车维护出错 {0}: {1}", type(e).__name__, e))
            await asyncio.sleep(5)


class Manager:
    def __init__(self):
        self.runners: dict[int, TaskRunner] = {}

    def start(self, task_id: int) -> None:
        if task_id in self.runners:
            return
        r = TaskRunner(task_id)
        self.runners[task_id] = r
        r.atask = asyncio.create_task(r.run(), name=f"task:{task_id}")

    async def stop(self, task_id: int, wait: float = 5.0) -> None:
        r = self.runners.get(task_id)
        if not r:
            return
        r.stop_evt.set()
        if getattr(r, "wake", None):
            r.wake.set()
        try:
            await asyncio.wait_for(asyncio.shield(r.atask), timeout=wait)
        except Exception:
            pass

    def state(self, task_id: int) -> dict | None:
        r = self.runners.get(task_id)
        return dict(r.state) if r else None

    async def resume_all(self) -> None:
        with SessionLocal() as db:
            ids = list(db.scalars(select(Task.id).where(Task.enabled.is_(True))))
        with SessionLocal() as db:
            for t in db.scalars(select(Task).where(Task.status == "checkout")):
                t.status, t.status_text, t.enabled = "pending_confirm", T("服务在结账过程中重启，请人工确认订单"), False
                db.add(TaskLog(task_id=t.id, marker="PENDING_CONFIRM",
                               msg=T("服务重启时该任务正在结账，结果不确定；任务不会自动恢复，请到 OVH 控制台确认是否已下单")))
                if t.id in ids:
                    ids.remove(t.id)
            db.commit()
        for tid in ids:
            tlog(tid, "INFO", T("服务重启，自动恢复任务"))
            self.start(tid)

    async def shutdown(self) -> None:
        for tid in list(self.runners):
            r = self.runners.get(tid)
            if r:
                r.stop_evt.set()
        waits = {tid: (35 if getattr(r, "in_checkout", False) else 8) for tid, r in self.runners.items()}
        await asyncio.gather(*(self.stop(tid, w) for tid, w in waits.items()), return_exceptions=True)


manager = Manager()
