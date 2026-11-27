"""Price cap: catalog units and checkout units are parsed by source, and a cap is applied
to OVH's VAT-inclusive checkout total before any POST /checkout (slow path and fast lane).

Offline.   DEFAULT_LANG=zh python tests/price_cap_test.py
"""
import asyncio, itertools, os, sys, tempfile, time
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="ovhsn-cap-")
os.environ.setdefault("SECRET_KEY", "x" * 48)
os.environ["MIN_POLL_INTERVAL"] = "3"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ovh.exceptions as oe
from app import ovh_engine as E, worker as W
from app.db import Base, engine, SessionLocal
from app.models import Credential, Task, TaskLog, User
from app.security import enc

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


QUOTE = {"value": 1402.2}  # currency units, not 1e8. A lower cap must reject this.


class FakeOVH:
    """Cart + checkout stand-in. Line totals are small; the checkout total is QUOTE (over the cap)."""
    calls = []
    carts = {}
    ids = itertools.count(100)
    quote = 1402.2
    noprice = False

    def __init__(self, *a, **k):
        pass

    def _log(self, m, p):
        FakeOVH.calls.append((m, p.split("?")[0]))

    def get(self, path, **kw):
        self._log("GET", path)
        parts = path.strip("/").split("/")
        if path == "/me":
            return {"nichandle": "fk123-ovh", "ovhSubsidiary": "IE", "currency": {"code": "EUR"}}
        if path == "/auth/time":
            return int(time.time())
        if parts[:2] == ["order", "cart"]:
            if len(parts) > 3 and parts[3] == "checkout":
                if FakeOVH.noprice:
                    return {"prices": {"weird": "n/a"}}
                return {"prices": {"withTax": {"value": FakeOVH.quote}, "withoutTax": {"value": 100.0}}}
            c = self.carts[parts[2]]
            if len(parts) == 3:
                return {"items": list(c["items"]), "expire": "2099-01-01T00:00:00+00:00"}
            iid = int(parts[4])
            it = c["items"][iid]
            if len(parts) == 5:
                return {"settings": {"planCode": it["planCode"]},
                        "prices": [{"label": "TOTAL", "price": {"value": it["price"]}}]}
            if len(parts) == 6:
                return list(range(len(it["cfg"])))
            lab = list(it["cfg"])[int(parts[6])]
            return {"label": lab, "value": it["cfg"][lab]}
        raise oe.ResourceNotFoundError(path)

    def post(self, path, **kw):
        self._log("POST", path)
        parts = path.strip("/").split("/")
        if path == "/order/cart":
            cid = f"cart{next(FakeOVH.ids)}"
            self.carts[cid] = {"items": {}}
            return {"cartId": cid, "expire": "2099-01-01T00:00:00+00:00"}
        c = self.carts[parts[2]]
        if parts[3] == "assign":
            return None
        if parts[3] in ("eco", "baremetalServers") and len(parts) == 4:
            iid = next(FakeOVH.ids)
            # Line total is deliberately far under the cap. The old fast lane estimated from this.
            c["items"][iid] = {"planCode": kw["planCode"], "price": 10.0, "cfg": {}}
            return {"itemId": iid}
        if parts[3] in ("eco", "baremetalServers") and parts[4] == "options":
            iid = next(FakeOVH.ids)
            c["items"][iid] = {"planCode": kw["planCode"], "price": 0.0, "cfg": {}}
            return {"itemId": iid}
        if parts[3] == "item" and parts[5] == "configuration":
            c["items"][int(parts[4])]["cfg"][kw["label"]] = kw["value"]
            return {}
        if parts[3] == "checkout":
            return {"orderId": 999, "url": "https://example.invalid/999",
                    "prices": {"withTax": {"value": FakeOVH.quote}}}
        raise oe.ResourceNotFoundError(path)

    def delete(self, path, **kw):
        self._log("DELETE", path)
        self.carts.pop(path.strip("/").split("/")[2], None)


def posts():
    return [p for m, p in FakeOVH.calls if m == "POST" and p.endswith("/checkout")]


def gets():
    return [p for m, p in FakeOVH.calls if m == "GET" and p.endswith("/checkout")]


PLAN = {
    "planCode": "plan1", "invoiceName": "KS-TEST",
    "pricings": [{"mode": "default", "interval": 1, "capacities": ["renew"], "price": 10 * 10 ** 8}],
    "configurations": [{"name": "dedicated_datacenter", "values": ["gra"]},
                       {"name": "region", "values": ["europe"]}],
    "addonFamilies": [
        {"name": "memory", "mandatory": True, "default": "ram-32g-plan1", "addons": ["ram-32g-plan1"]},
        {"name": "storage", "mandatory": True, "default": "disk-a-plan1", "addons": ["disk-a-plan1"]},
    ],
}
CAT = {"currency": "EUR", "plans": {"plan1": PLAN}, "addons": {}}
AVAIL = [{"planCode": "plan1", "fqn": "plan1.ram-32g.disk-a", "memory": "ram-32g", "storage": "disk-a",
          "datacenters": [{"datacenter": "gra", "availability": "1H-low"}]}]


def spec(cap):
    return E.OrderSpec(subsidiary="IE", catalog_type="eco", plan_obj=PLAN, price_cap=cap,
                       auto_pay=True, vat_rate=1.23, addon_pricing={})


def reset():
    FakeOVH.calls, FakeOVH.carts = [], {}
    FakeOVH.quote, FakeOVH.noprice = 1402.2, False


# ---------------------------------------------------------------- parsers
def test_parsers():
    check("catalog 56.99 (1e8) stays 56.99", abs(E.catalog_price(56_99000000) - 56.99) < 1e-9)
    check("catalog 1402.20 (1e8) stays 1402.20", abs(E.catalog_price(1402_20000000) - 1402.2) < 1e-6)
    # Below the old heuristic cutoff: a catalog amount must still be divided, a checkout amount must not.
    check("catalog 999 micros is not 999 currency", E.catalog_price(999) == 999 / 1e8)
    check("checkout 999 stays 999", E.checkout_price(999) == 999)
    check("checkout 1000 stays 1000", E.checkout_price(1000) == 1000)
    check("checkout 1402.20 stays 1402.20", E.checkout_price({"value": 1402.2}) == 1402.2)
    total = E.first_invoice_total({"prices": {"withTax": {"value": 1402.2}}})
    check("first invoice >= 1000 is not scaled to ~0", total == 1402.2, total)


# ---------------------------------------------------------------- slow path
def test_slow_path_rejects_large_total():
    reset()
    client = FakeOVH()
    r = E.preview_cart(client, spec(200.0), {"dc": "gra", "memory": "ram-32g", "storage": "disk-a"},
                       lambda *a: None, True)
    check("slow path rejects a 1402.20 total against cap 200", r.outcome == "rejected" and r.info.get("total") == 1402.2, r.info)
    check("slow path does not POST /checkout", posts() == [], posts())
    check("rejection names the real total", "1402.20" in str(r.info.get("reason")), r.info)


def test_slow_path_unparseable():
    reset()
    FakeOVH.noprice = True
    r = E.preview_cart(FakeOVH(), spec(200.0), {"dc": "gra", "memory": "ram-32g", "storage": "disk-a"},
                       lambda *a: None, True)
    check("slow path refuses an unparseable total", r.outcome == "rejected", r.info)
    check("unparseable total does not POST /checkout", posts() == [], posts())


# ---------------------------------------------------------------- fast lane
def test_fast_lane_build_uses_ovh_total():
    reset()
    logs = []
    slots = E.Slots(FakeOVH(), spec(200.0), "ram-32g", "disk-a", ["gra"], lambda m, s: logs.append((m, str(s))))
    slot = slots.build("gra")
    check("prebuild stores OVH withTax, not the 10.00 line estimate", slot and slot["price"] == 1402.2 and slot["ok"] is False, slot)
    check("prebuild over cap does not POST /checkout", posts() == [], posts())
    cart, reason = slots.prepare_checkout("gra")
    check("fresh over-cap quote is not handed out", cart is None and reason and "1402.20" in reason, reason)
    check("fresh over-cap quote does not POST /checkout", posts() == [], posts())
    # The reading is fresh, so the refusal must not spend another GET.
    n = len(gets())
    cart, reason = slots.prepare_checkout("gra")
    check("second refusal reuses the fresh OVH total", cart is None and len(gets()) == n, (reason, len(gets()), n))


def test_fast_lane_stale_requote_blocks_post():
    reset()
    slots = E.Slots(FakeOVH(), spec(200.0), "ram-32g", "disk-a", ["gra"], lambda *a: None)
    slots.s["gra"] = {"cid": "cartSTALE", "iid": 1, "codes": [], "exp": time.time() + 10 ** 6,
                      "price": 12.3, "checked": time.time() - E.FRESH_PRICE_MAX - 5,
                      "ok": True, "quoted": True}
    before = len(gets())
    cart, reason = slots.prepare_checkout("gra")
    check("stale under-cap cache is re-read and then refused", cart is None and reason and "1402.20" in reason, reason)
    check("stale re-check is one GET and no POST", len(gets()) == before + 1 and posts() == [], (gets(), posts()))


def test_fast_lane_fresh_under_cap_is_one_post_later():
    """A fresh OVH total within the cap is returned without another GET. prepare itself never POSTs."""
    reset()
    FakeOVH.quote = 80.0
    slots = E.Slots(FakeOVH(), spec(200.0), "ram-32g", "disk-a", ["gra"], lambda *a: None)
    built = slots.build("gra")
    gets_after_build = len(gets())
    check("under-cap prebuild is usable", built and built["ok"] and built["price"] == 80.0, built)
    cart, reason = slots.prepare_checkout("gra")
    check("fresh under-cap cart is handed out with no extra GET and no POST",
          cart and cart["price"] == 80.0 and reason is None and len(gets()) == gets_after_build and posts() == [],
          (cart, reason, len(gets()), gets_after_build, posts()))


def test_fast_lane_unparseable():
    reset()
    FakeOVH.noprice = True
    slots = E.Slots(FakeOVH(), spec(200.0), "ram-32g", "disk-a", ["gra"], lambda *a: None)
    slot = slots.build("gra")
    check("unparseable prebuild is not ok", slot and slot["ok"] is False and slot["price"] is None, slot)
    cart, reason = slots.prepare_checkout("gra")
    check("unparseable quote aborts without a cart or a POST", cart is None and reason and posts() == [], (reason, posts()))


# ---------------------------------------------------------------- worker: fast lane and slow path both refuse to POST
class Feed:
    def __init__(self):
        self.data, self.error, self.last_ms = AVAIL, None, 5


def subscribe(sid, endpoint, plan, interval, event):
    event.set()
    return Feed()


def setup_task(prebuild):
    Base.metadata.create_all(engine)
    cfg = {"plan1": {"type": "eco", "label": "KS-TEST", "memory": "ram-32g", "storage": "disk-a"}}
    with SessionLocal() as db:
        u = db.query(User).first()
        if not u:
            u = User(username="cap", password_hash="x", lang="zh")
            db.add(u)
            db.commit()
            db.add(Credential(user_id=u.id, endpoint="ovh-eu", app_key_enc=enc("a"),
                              app_secret_enc=enc("b"), consumer_key_enc=enc("c")))
            db.commit()
        t = Task(user_id=u.id, endpoint="ovh-eu", subsidiary="IE", catalog_type="eco", plan_code="plan1",
                 plan_codes="plan1", plan_configs=__import__("json").dumps(cfg),
                 memory="ram-32g", storage="disk-a", datacenters="gra",
                 poll_interval=3, dry_run=False, auto_pay=True, max_orders=1, enabled=True,
                 any_config=False, prebuild=prebuild, price_cap=200.0, vat_rate=1.23)
        db.add(t)
        db.commit()
        return t.id


async def _run_worker(prebuild, wait=8):
    reset()
    E.make_client = lambda *a, **k: FakeOVH()
    W.get_catalog = lambda *a, **k: CAT
    W.hub.subscribe = subscribe
    tid = setup_task(prebuild)
    W.manager.start(tid)
    t0 = time.time()
    while time.time() - t0 < wait:
        await asyncio.sleep(0.25)
        with SessionLocal() as db:
            logs = db.query(TaskLog).filter_by(task_id=tid).all()
        if any(l.marker == "ORDER_FAIL" and "1402.20" in l.msg for l in logs):
            break
    if tid in W.manager.runners:
        await W.manager.stop(tid)
    with SessionLocal() as db:
        t = db.get(Task, tid)
        logs = [(l.marker, l.msg) for l in db.query(TaskLog).filter_by(task_id=tid).order_by(TaskLog.id)]
    return t, logs


def test_worker_paths():
    for prebuild, name in ((False, "slow"), (True, "fast")):
        t, logs = asyncio.run(_run_worker(prebuild))
        text = "\n".join(m for _, m in logs)
        check(f"worker {name} lane: no checkout POST when OVH total is 1402.20", posts() == [], posts())
        check(f"worker {name} lane: refusal shows 1402.20, not ~0", "1402.20" in text, text[-500:])
        check(f"worker {name} lane: orders_done stays 0", t.orders_done == 0 and t.status != "done", (t.orders_done, t.status, t.status_text))


if __name__ == "__main__":
    test_parsers()
    test_slow_path_rejects_large_total()
    test_slow_path_unparseable()
    test_fast_lane_build_uses_ovh_total()
    test_fast_lane_stale_requote_blocks_post()
    test_fast_lane_fresh_under_cap_is_one_post_later()
    test_fast_lane_unparseable()
    test_worker_paths()
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1 if FAILS else 0)
