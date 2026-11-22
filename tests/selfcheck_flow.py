"""End-to-end self-check of the whole flow through the real web app + worker + tracker, with a fake OVH account
(availability comes from the LIVE public OVH API, like tests/mock_order_test.py):

  CLI create owner -> login -> create task (form) in each of the 3 modes -> start -> stock found -> cart ->
  price preview -> checkout (autoPay false/true) -> payment -> delivery tracking (+ lottery) ->
  categorized notifications (Telegram + webhook payloads captured) -> order history / detail pages / live log.

    SECRET_KEY=... python tests/selfcheck_flow.py
"""
import io, json, os, re, sys, time
os.environ.setdefault("MIN_POLL_INTERVAL", "3")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mock_order_test as M                      # sets DATA_DIR to a temp dir, patches make_client with FakeOVH
from app import ovh_engine as E, notify as N, tracker as T, cli
from app.db import SessionLocal
from app.models import User, Credential, NotifySetting, Order, Task, TaskLog
from app.security import enc

FAILS = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(extra)[:300]}]" if extra and not cond else ""))
    cond or FAILS.append(name)

# ---- notifications: capture what would be sent to Telegram / the webhook
SENT = []
class R:
    status_code = 200
    def json(self): return {}
N.httpx.post = lambda url, json=None, timeout=None: (SENT.append(("tg", json["text"])), R())[1]
N.post_json = lambda url, payload, **k: (SENT.append(("hook", payload)), R())[1]
def cats(): return [p["category"] for k, p in SENT if k == "hook"]
def hooks(cat): return [p for k, p in SENT if k == "hook" and p["category"] == cat]

# ---- fake OVH account side for the tracker (status / details / dedicated servers / hardware)
class FakeFlow(M.FakeOVH):
    status = {}          # orderId -> OVH status
    servers = {}         # orderId -> serviceName (appears once delivered)
    def get(self, path, **kw):
        m = re.fullmatch(r"/me/order/(\d+)/status", path)
        if m: return FakeFlow.status.get(m.group(1), "delivering")
        m = re.fullmatch(r"/me/order/(\d+)/details", path)
        if m: return [1]
        m = re.fullmatch(r"/me/order/(\d+)/details/1", path)
        if m: return {"domain": FakeFlow.servers.get(m.group(1), "*")}
        if path == "/dedicated/server": return [s for o, s in FakeFlow.servers.items() if FakeFlow.status.get(o) == "delivered"]
        if path.startswith("/dedicated/server/") and path.endswith("/specifications/hardware"):
            return {"processorName": "Xeon-E 2386G", "numberOfProcessors": 1, "coresPerProcessor": 6, "threadsPerProcessor": 12,
                    "memorySize": {"unit": "MB", "value": 65536},   # 64 GB delivered for a 32 GB plan -> lottery
                    "diskGroups": [{"numberOfDisks": 2, "diskSize": {"unit": "GB", "value": 512}, "diskType": "NVME"}]}
        if path.startswith("/dedicated/server/") and path.endswith("/serviceInfos"): return {"creation": "2099-01-01"}
        if path.startswith("/dedicated/server/"):
            return {"name": path.rsplit("/", 1)[1], "datacenter": "fra1", "ip": "203.0.113.10", "state": "ok"}
        return super().get(path, **kw)
E.make_client = M.W.E.make_client = lambda *a, **k: FakeFlow()
T.Tracker.start = lambda self: None              # the test drives check_order() itself (no background races)

# ---- 1. owner account via the CLI (the only way to create one)
M.Base.metadata.create_all(M.engine)
sys.stdin = io.StringIO("Selfcheck-Owner-9\n")
rc = cli.main(["create-admin", "-u", "boss", "--password-stdin"])
sys.stdin = sys.__stdin__
check("cli: create-admin", rc == 0)
sys.stdin = io.StringIO("Another-pass-77\n")
check("cli: second create-admin refused (single owner)", cli.main(["create-admin", "-u", "x2", "--password-stdin"]) == 2)
sys.stdin = sys.__stdin__
with SessionLocal() as db:
    owner = db.query(User).filter_by(username="boss").one(); UID = owner.id
    db.add(Credential(user_id=UID, endpoint="ovh-eu", app_key_enc=enc("ak"), app_secret_enc=enc("as"), consumer_key_enc=enc("ck")))
    db.add(NotifySetting(user_id=UID, telegram_token_enc=enc("123456:" + "A" * 30), telegram_chat_id="1",
                         webhook_url_enc=enc("https://hooks.example.com/x"), stock_debounce=0))
    db.commit()

from fastapi.testclient import TestClient
from app.main import app
B = "http://testserver"
def tok(c, path): return re.search(r'name="csrf" value="([^"]+)"', c.get(path).text).group(1)

PLAN = [{"planCode": "24rise01-v2", "type": "eco", "label": "RISE-1", "memory": "", "storage": "", "bandwidth": ""}]
RESULTS = {}
with TestClient(app, base_url=B) as c:
    check("web: /register removed", c.get("/register").status_code == 404)
    r = c.post("/login", data={"csrf": tok(c, "/login"), "username": "boss", "password": "Selfcheck-Owner-9"})
    check("web: owner login", str(r.url).rstrip("/") == B, r.url)

    for mode in ("dry", "order", "pay"):
        SENT.clear(); M.FakeOVH.calls = []; M.FakeOVH.mode = "ok"; M.FakeOVH.expect_autopay = (mode == "pay")
        # ---- 2. create the task through the real form
        r = c.post("/tasks/save", data={"csrf": tok(c, "/tasks/new"), "endpoint": "ovh-eu", "subsidiary": "IE",
                                        "plans_json": json.dumps(PLAN), "name": f"selfcheck {mode}", "any_config": "1",
                                        "pay_mode": mode, "max_orders": "1", "price_cap": "200", "poll_interval": "3",
                                        "quantity": "1", "duration": "P1M", "pricing_mode": "default"})
        tid = int(re.search(r"/tasks/(\d+)$", str(r.url)).group(1))
        with SessionLocal() as db:
            t = db.get(Task, tid)
            check(f"[{mode}] task saved with the right flags", (t.dry_run, t.auto_pay) == {"dry": (True, False), "order": (False, False), "pay": (False, True)}[mode])
        # ---- 3. start, wait for stock -> cart -> preview -> (checkout)
        c.post(f"/tasks/{tid}/start", data={"csrf": tok(c, f"/tasks/{tid}")})
        t0 = time.time()
        while time.time() - t0 < 60:
            with SessionLocal() as db:
                t = db.get(Task, tid); rows = db.query(Order).filter_by(task_id=tid).all()
            if (mode == "dry" and rows) or (mode != "dry" and t.status in ("done", "stopped", "error", "pending_confirm")):
                break
            time.sleep(1)
        if mode == "dry":
            c.post(f"/tasks/{tid}/stop", data={"csrf": tok(c, f"/tasks/{tid}")})
        time.sleep(1.5)   # notification threads
        with SessionLocal() as db:
            t = db.get(Task, tid); rows = db.query(Order).filter_by(task_id=tid).all()
            markers = [l.marker for l in db.query(TaskLog).filter_by(task_id=tid).order_by(TaskLog.id)]
        posts = [p for m, p in M.FakeOVH.calls if m == "POST"]
        checkouts = [p for p in posts if p.endswith("/checkout")]
        print(f"   [{mode}] status={t.status} markers={[m for m in markers if m not in ('STOCK', 'HEARTBEAT', 'NO_STOCK')]}")
        check(f"[{mode}] stock found + cart built", "STOCK_FOUND" in markers and "/order/cart" in posts, markers)
        check(f"[{mode}] price preview read from the cart", any(m in markers for m in ("PREVIEW", "PREVIEW_OK")), markers)
        if mode == "dry":
            check("[dry] never checks out", not checkouts, checkouts)
            check("[dry] cart deleted after preview", any(m == "DELETE" for m, _ in M.FakeOVH.calls))
            check("[dry] preview row in order history", rows and rows[0].kind == "preview" and rows[0].price_with_tax == 70.11, [(o.kind, o.price_with_tax) for o in rows])
            st = hooks("stock")
            check("[dry] 有货通知 with preview price + CNY", st and "购物车预览首期含税" in st[0]["text"] and "≈ ¥" in st[0]["text"], st)
        else:
            o = next((x for x in rows if x.kind == "order"), None)
            check(f"[{mode}] exactly one checkout", len(checkouts) == 1, checkouts)
            check(f"[{mode}] task done after max_orders=1", t.status == "done" and t.orders_done == 1, (t.status, t.orders_done))
            check(f"[{mode}] order row + tracking", o and o.order_id and o.track_state == "tracking" and o.url, o and (o.order_id, o.track_state))
            check(f"[{mode}] payMode + currency stored", o and json.loads(o.detail).get("payMode") == mode and json.loads(o.detail).get("currency") == "EUR")
            oc = hooks("order")
            check(f"[{mode}] 提交订单通知 with id, amount, CNY, link", oc and o.order_id in oc[0]["title"] and "≈ ¥" in oc[0]["text"] and oc[0]["link"], oc)
            if mode == "order":
                check("[order] status unpaid, no paid notification", o.status == "unpaid" and not hooks("paid"), (o.status, cats()))
                check("[order] tells the user to pay manually", oc and "手动" in oc[0]["text"] or "付款" in oc[0]["text"])
            else:
                check("[pay] auto-paid (paid_at, status paid)", o.status == "paid" and o.paid_at, o.status)
                check("[pay] 付款通知 sent", len(hooks("paid")) == 1, cats())
            RESULTS[mode] = o.id if o else None
        tg = [x for k, x in SENT if k == "tg"]
        check(f"[{mode}] telegram messages are categorized HTML", tg and all(x.startswith("<b>【") for x in tg), tg[:2])

    # ---- 4. payment + delivery tracking (order-only mode: user pays later in OVH)
    oid_row = RESULTS.get("order")
    with SessionLocal() as db:
        oo = db.get(Order, oid_row); ORDER_ID = oo.order_id
        po = db.get(Order, RESULTS.get("pay")); PAY_ID = po.order_id
    SENT.clear()
    FakeFlow.status[ORDER_ID] = "notPaid"; T.check_order(oid_row)
    check("[track] notPaid -> nothing sent", not SENT, cats())
    FakeFlow.status[ORDER_ID] = "checking"; T.check_order(oid_row); time.sleep(0.2)
    check("[track] user paid in OVH -> 付款通知 once", cats() == ["paid"] and "≈ ¥" in hooks("paid")[0]["text"], cats())
    FakeFlow.status[ORDER_ID] = "delivered"; FakeFlow.servers[ORDER_ID] = "ns1.selfcheck.example"; SENT.clear(); T.check_order(oid_row)
    dv = hooks("delivered")
    check("[track] delivered -> 交付通知 with actual vs official + lottery", dv and "实际配置" in dv[0]["text"] and "官方配置" in dv[0]["text"] and "恭喜中奖" in dv[0]["text"], dv)
    SENT.clear(); T.check_order(oid_row)
    check("[track] no duplicate notifications", not SENT, cats())
    FakeFlow.status[PAY_ID] = "delivered"; FakeFlow.servers[PAY_ID] = "ns2.selfcheck.example"; SENT.clear(); T.check_order(RESULTS["pay"])
    check("[track] auto-paid order: only 交付通知 (paid already sent)", cats() == ["delivered"], cats())
    check("[track] tracker only used GET", all(m == "GET" for m, p in M.FakeOVH.calls if "/me/order/" in p or "/dedicated/" in p))

    # ---- 5. order history + detail pages
    h = c.get("/orders").text
    check("[history] both orders listed with status labels", ORDER_ID in h and PAY_ID in h and "已交付" in h, "")
    check("[history] lottery tag + CNY under amounts", "中奖" in h and "≈ ¥" in h)
    d = c.get(f"/orders/{oid_row}").text
    check("[detail] lottery banner + comparison table", "恭喜中奖" in d and "实际交付" in d and "64 GB" in d)
    check("[detail] amount with CNY + payment mode", "≈ ¥" in d and "只下单不付款" in d)
    # ---- 6. live log + running strip APIs
    lg = c.get(f"/api/tasks/{RESULTS['pay'] and tid}/logs").json()
    check("[live] logs API returns lines + state", lg["logs"] and "lastPoll" in lg)
    check("[live] running strip API", isinstance(c.get("/api/tasks/running").json().get("tasks"), list))

print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
sys.stdout.flush(); sys.stderr.flush()
os._exit(1 if FAILS else 0)
