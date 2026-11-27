"""v2.1.1 safety fixes, offline:   python tests/safety_v211_test.py
1 prices parsed by source (no magnitude guessing; > €1000)   2 real OVH total checked before every checkout (fast lane)
3 exact config matching   4 409 / 5xx / timeout at checkout => pending (no retry)   5 pending_confirm needs explicit
confirmation + OVH order lookup   6 cross-task / cross-process order lock."""
import asyncio, inspect, json, os, re, subprocess, sys, tempfile
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("SECRET_KEY", "unit-only-" + "k" * 40)
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="ovhsn-v211-")
import httpx  # noqa: E402
import requests  # noqa: E402
import ovh.exceptions as oe  # noqa: E402
fails = []


def ok(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    cond or fails.append(name)


from app import ovh_engine as E, worker as W  # noqa: E402
from app.orderlock import OrderLock, order_lock_for  # noqa: E402

# ---------------------------------------------------------------- 1 price parsing
ok("1 catalog price 1699000000 -> 16.99", E.catalog_price(1699000000) == 16.99)
ok("1 catalog price 123456000000 -> 1234.56 (no guessing)", abs(E.catalog_price(123456000000) - 1234.56) < 1e-9)
ok("1 catalog price 500 is 0.000005, not €500", E.catalog_price(500) == 5e-06)
ok("1 cart price object value 1234.56 stays 1234.56", E.price_value({"value": 1234.56, "text": "€1,234.56", "currencyCode": "EUR"}) == 1234.56)
ok("1 cart price object value 999.99", E.price_value({"value": 999.99}) == 999.99)
ok("1 priceInUcents 123456000000 -> 1234.56", abs(E.price_value({"priceInUcents": 123456000000}) - 1234.56) < 1e-9)
ok("1 bare number is not guessed", E.price_value(1234.56) is None and E.price_value("12") is None)
big = {"prices": {"withTax": {"value": 1518.51, "currencyCode": "EUR"}, "withoutTax": {"value": 1234.56}}}
ok("1 first_invoice_total > €1000", E.first_invoice_total(big) == 1518.51)
ok("1 first_invoice_total missing -> None", E.first_invoice_total({"prices": {"withTax": {"text": "?"}}}) is None
   and E.first_invoice_total({"prices": {"withTax": 1518.51}}) is None)
ok("1 monthly_and_setup uses catalog units",
   E.monthly_and_setup({"pricings": [{"mode": "default", "interval": 1, "capacities": ["renew"], "price": 125000000000},
                                     {"mode": "default", "interval": 0, "capacities": ["installation"], "price": 0}]}) == (1250.0, 0.0))
ok("1 old magnitude-guessing euro() is gone", not hasattr(E, "euro"))


class Cli:
    """Minimal fake OVH client for cart/checkout calls."""
    def __init__(self, total=None, checkout_exc=None):
        self.total, self.exc, self.calls = total, checkout_exc, []

    def get(self, path, **kw):
        self.calls.append(("GET", path))
        if path.endswith("/checkout"):
            return {"prices": {"withTax": {"value": self.total}}} if self.total is not None else {"prices": {}}
        if path.startswith("/me/order"):
            return [] if path == "/me/order" else {}
        return {}

    def post(self, path, **kw):
        self.calls.append(("POST", path))
        if self.exc:
            raise self.exc
        return {"orderId": 1, "url": "u", "prices": {"withTax": {"value": self.total}}}

    def delete(self, path, **kw):
        self.calls.append(("DELETE", path))


class Spec:
    def __init__(self, cap):
        self.price_cap, self.auto_pay = cap, True


log = lambda *a: None
# ---------------------------------------------------------------- 2 real price before checkout
c = Cli(total=1518.51)
r = E.check_cart_price(c, Spec(200.0), "cartX", log)
ok("2 real total €1518.51 > cap €200 -> rejected, cart deleted, no checkout POST",
   r.outcome == "rejected" and ("DELETE", "/order/cart/cartX") in c.calls and not any(m == "POST" for m, _ in c.calls), r.info)
c = Cli(total=None)
r = E.check_cart_price(c, Spec(None), "cartY", log)
ok("2 unparseable total -> rejected even without cap (fail closed)", r.outcome == "rejected" and ("DELETE", "/order/cart/cartY") in c.calls)
c = Cli(total=70.11)
r = E.check_cart_price(c, Spec(80.0), "cartZ", log)
ok("2 total under cap -> ok, cart kept", r.outcome == "preview" and r.info["total"] == 70.11 and not any(m == "DELETE" for m, _ in c.calls))
src = inspect.getsource(W.TaskRunner._run)
fast = src[src.index("slot = p[\"slots\"].take"):src.index("E.checkout, client")]
ok("2 worker fast lane calls check_cart_price before checkout", "E.check_cart_price" in fast)

# ---------------------------------------------------------------- 3 exact matching
plan = {"planCode": "24rise01-v2", "addonFamilies": [
    {"name": "memory", "mandatory": True, "default": "ram-32g-ecc-3200-24rise01-v2",
     "addons": ["ram-32g-ecc-3200-24rise01-v2", "ram-32g-ecc-3200-ext-24rise01-v2", "ram-64g-ecc-3200-24rise01-v2"]},
    {"name": "storage", "mandatory": True, "default": "softraid-2x512nvme-24rise01-v2",
     "addons": ["softraid-2x512nvme-24rise01-v2", "softraid-2x5120nvme-24rise01-v2"]}]}
ok("3 addon_code exact (plan suffix)", E.addon_code(plan, "memory", "ram-32g-ecc-3200", synthesize=False) == "ram-32g-ecc-3200-24rise01-v2")
ok("3 addon_code: short 'ram-32g' does NOT match by substring", E.addon_code(plan, "memory", "ram-32g", synthesize=False) is None)
ok("3 addon_code: 2x512 does not pick 2x5120", E.addon_code(plan, "storage", "softraid-2x512nvme", synthesize=False) == "softraid-2x512nvme-24rise01-v2"
   and E.addon_code(plan, "storage", "softraid-2x51nvme", synthesize=False) is None)
ok("3 addon_code: DDR generation ignored (documented canonical form)",
   E.addon_code({"planCode": "27skle03-v1", "addonFamilies": [{"name": "memory", "addons": ["ram-64g-ddr4-ecc-2666-27skle03-v1"]}]},
                "memory", "ram-64g-ecc-2666", synthesize=False) == "ram-64g-ddr4-ecc-2666-27skle03-v1")
ok("3 is_default_config exact", E.is_default_config(plan, "ram-32g-ecc-3200", "softraid-2x512nvme")
   and not E.is_default_config(plan, "ram-32g", "softraid-2x512nvme"))
rows = [{"memory": "ram-32g-ecc-3200-ext", "storage": "softraid-2x512nvme", "datacenters": [("gra", "1H-low")], "is_default": False},
        {"memory": "ram-32g-ecc-3200", "storage": "softraid-2x5120nvme", "datacenters": [("gra", "1H-low")], "is_default": False}]
ok("3 pick_stock: prefix-similar rows are NOT picked", E.pick_stock(rows, [], False, "ram-32g-ecc-3200", "softraid-2x512nvme", "24rise01-v2") is None)
rows.append({"memory": "RAM-32G-ECC-3200 ", "storage": "softraid-2x512nvme", "datacenters": [("rbx", "1H-low")], "is_default": False})
pk = E.pick_stock(rows, [], False, "ram-32g-ecc-3200", "softraid-2x512nvme", "24rise01-v2")
ok("3 pick_stock: exact row (case/space normalised) picked", pk is not None and pk["dc"] == "rbx", pk)

# ---------------------------------------------------------------- 4 409 / uncertain checkout
E.time.sleep = lambda s: None   # skip the 5 s pause in checkout()'s pending branch
for name, exc in (("409 Conflict", oe.ResourceConflictError("cart already checked out")),
                  ("timeout", requests.exceptions.ReadTimeout("read timed out")),
                  ("5xx", oe.APIError("500 internal"))):
    r = E.checkout(Cli(total=10, checkout_exc=exc), Spec(None), "cartQ", "2026-01-01T00:00:00Z", log)
    ok(f"4 checkout {name} -> pending (uncertain, stop, no retry)", r.outcome == "pending", r.outcome)
r = E.checkout(Cli(total=10, checkout_exc=oe.BadParametersError("Item is out of stock")), Spec(None), "cartQ", "x", log)
ok("4 checkout 400 BadParameters -> rejected (definite, no order)", r.outcome == "rejected")
ok("4 ResourceConflictError not in DEFINITE_REJECT", oe.ResourceConflictError not in E.DEFINITE_REJECT)

# ---------------------------------------------------------------- 6 lock
a, b = order_lock_for(1, "ovh-eu", "24rise01-v2"), order_lock_for(1, "ovh-eu", "24rise01-v2")
ok("6 first task gets the lock", a.acquire())
ok("6 second task (same process, same account+plan) is refused", not b.acquire())
other = order_lock_for(1, "ovh-eu", "24adv01-v3")
ok("6 another plan is independent", other.acquire()); other.release()
probe = ("import sys; sys.path.insert(0, %r); from app.orderlock import order_lock_for; "
         "print('GOT' if order_lock_for(1, 'ovh-eu', '24rise01-v2').acquire() else 'BUSY')" % ROOT)
out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=os.environ).stdout.strip()
ok("6 another process is refused while held", out == "BUSY", out)
a.release()
out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=os.environ).stdout.strip()
ok("6 released -> another process gets it", out == "GOT", out)
ok("6 worker takes the lock around cart -> checkout", "order_lock_for(t.user_id, t.endpoint" in src
   and src.index("olock.acquire()") < src.index("E.checkout, client") < src.index("olock.release()"))


# ---------------------------------------------------------------- 7 429 back-off + minimum poll interval (t85u)
from app import ratelimit as RL, config as CFG  # noqa: E402
import types  # noqa: E402
ok("7 Retry-After seconds", RL.parse_retry_after("12") == 12.0)
import email.utils as _eu, time as _t  # noqa: E402
ra = RL.parse_retry_after(_eu.formatdate(_t.time() + 30, usegmt=True))
ok("7 Retry-After HTTP-date", ra is not None and 25 <= ra <= 31, ra)
resp = types.SimpleNamespace(status_code=429, headers={"Retry-After": "20"})
ok("7 429 detected on response and exception", RL.is_429(resp) and RL.is_429(oe.APIError("HTTP 429 Too Many Requests"))
   and not RL.is_429(oe.APIError("500")))
err = oe.APIError("rate"); err.response = resp
ok("7 retry_after from exception.response", RL.retry_after_of(err) == 20.0)
key = ("acct", 99, "ovh-eu")
w1 = RL.hit(key, 20.0)
ok("7 first 429 honours Retry-After (20s +/-20%)", 16 <= w1 <= 24, w1)
w2, w3 = RL.hit(key), RL.hit(key)
ok("7 exponential back-off grows (5,10,20... x jitter)", 8 <= w2 <= 12 and 16 <= w3 <= 24, (w2, w3))
for _ in range(10): RL.hit(key)
ok("7 back-off capped at 300s", RL.remaining(key) <= RL.MAX_S + 0.01)
ok("7 shared: any task of the account sees the back-off", RL.remaining(("acct", 99, "ovh-eu")) > 0 and RL.remaining(("acct", 98, "ovh-eu")) == 0)
RL.ok(key)
ok("7 success resets", RL.remaining(key) == 0)
ok("7 worker skips ordering while the account is backed off", "RL.remaining(rl_key)" in src and src.index("RL.remaining(rl_key)") < src.index("olock.acquire()"))
import app.poller as PO  # noqa: E402
psrc = inspect.getsource(PO.Feed.run)
ok("7 poller: shared per-endpoint back-off before every request", "RL.remaining(rl_key)" in psrc and "RL.hit(rl_key, RL.retry_after_of(r))" in psrc)
os.environ["MIN_POLL_INTERVAL"] = "0.2"
import importlib  # noqa: E402
cfg2 = importlib.reload(CFG)
ok("7 MIN_POLL_INTERVAL cannot go below the hard floor", cfg2.MIN_POLL_INTERVAL == cfg2.HARD_MIN_POLL == 3.0, cfg2.MIN_POLL_INTERVAL)
os.environ["MIN_POLL_INTERVAL"] = "5"; importlib.reload(CFG)

# ---------------------------------------------------------------- (a) warm cart: real price pre-read into the slot
class SlotCli(Cli):
    def get(self, path, **kw):
        if path.endswith("/checkout"):
            return {"prices": {"withTax": {"value": 1518.51}}}
        return super().get(path, **kw)
sl = E.Slots(SlotCli(), types.SimpleNamespace(price_cap=200.0), "m", "s", ["gra"], log)
ok("a slot price = real OVH total pre-read while warm (not the estimate)", sl._real_price("c1", 99.0) == (1518.51, True))
ok("a slot falls back to the estimate if OVH gives no price", E.Slots(Cli(total=None), None, "m", "s", [], log)._real_price("c", 99.0) == (99.0, False))

# ---------------------------------------------------------------- 5 pending_confirm resume (HTTP)
from app.db import SessionLocal, auto_migrate  # noqa: E402
from app.models import Order, Task, TaskLog, User  # noqa: E402
from app.security import hash_password  # noqa: E402
from app import main  # noqa: E402
auto_migrate()
PW = "Correct-Horse-9x!"
with SessionLocal() as db:
    db.add(User(id=1, username="owner", password_hash=hash_password(PW), is_admin=True, lang=""))
    db.commit()
    for i in (1, 2, 3):
        db.add(Task(id=i, user_id=1, endpoint="ovh-eu", subsidiary="IE", catalog_type="eco", plan_code="24rise01-v2",
                    dry_run=False, auto_pay=True, max_orders=1 if i != 3 else 2, status="pending_confirm", enabled=False))
        db.commit()
        db.add(TaskLog(task_id=i, marker="PENDING_CONFIRM", msg="x"))
    db.add(Order(user_id=1, task_id=1, kind="pending", status="pending-confirm"))
    db.commit()
started = []
main.manager.start = lambda tid: started.append(tid)
LOOKUP = {"orders": [], "fail": False}


def fake_lookup(t, ts):
    if LOOKUP["fail"]:
        raise ConnectionError("no network")
    return LOOKUP["orders"]


main._orders_around_pending = fake_lookup


async def http():
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app, client=("127.0.0.1", 1)), base_url="http://t")
    tok = re.search(r'name="csrf" value="([^"]+)"', (await c.get("/login")).text).group(1)
    await c.post("/login", data={"csrf": tok, "username": "owner", "password": PW})
    page = (await c.get("/tasks/1")).text
    tok = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    ok("5 task page shows the confirm form, not the plain start button",
       "/tasks/1/resume-confirmed" in page and 'name="confirm"' in page and 'action="/tasks/1/start"' not in page)
    dash = (await c.get("/")).text
    ok("5 dashboard has no one-click start for a pending task", 'action="/tasks/1/start"' not in dash)
    r = await c.post("/tasks/1/start", data={"csrf": tok})
    with SessionLocal() as db:
        ok("5 plain start refused for pending_confirm", db.get(Task, 1).status == "pending_confirm" and not started)
    r = await c.post("/tasks/1/resume-confirmed", data={"csrf": tok})
    with SessionLocal() as db:
        ok("5 resume without the checkbox refused", db.get(Task, 1).status == "pending_confirm" and not started)
    LOOKUP["fail"] = True
    await c.post("/tasks/1/resume-confirmed", data={"csrf": tok, "confirm": "yes"})
    with SessionLocal() as db:
        ok("5 OVH lookup fails -> stays stopped (fail closed)", db.get(Task, 1).status == "pending_confirm" and not started)
    LOOKUP.update(fail=False, orders=[{"orderId": 263868667, "date": "d", "url": "u", "priceWithTax": 1518.51}])
    await c.post("/tasks/1/resume-confirmed", data={"csrf": tok, "confirm": "yes"})
    with SessionLocal() as db:
        t = db.get(Task, 1)
        o = db.query(Order).filter_by(task_id=1, order_id="263868667").first()
        ok("5 order found around the checkout is recorded and counted -> done, not restarted",
           t.status == "done" and t.orders_done == 1 and o is not None and not started, (t.status, t.orders_done))
        ok("5 pending order row resolved", db.query(Order).filter_by(task_id=1, kind="pending").first().status == "pending-resolved")
    # same OVH order already known (e.g. task 2 checks the same window) -> not double-counted, task resumes
    await c.post("/tasks/2/resume-confirmed", data={"csrf": tok, "confirm": "yes"})
    with SessionLocal() as db:
        t = db.get(Task, 2)
        ok("5 already-recorded order not counted twice; confirmed task resumes", t.orders_done == 0 and t.status == "starting" and started == [2])
    LOOKUP["orders"] = []
    await c.post("/tasks/3/resume-confirmed", data={"csrf": tok, "confirm": "yes"})
    ok("5 no order found -> resumes", started == [2, 3])

asyncio.run(http())
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
