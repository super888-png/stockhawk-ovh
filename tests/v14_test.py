"""v1.4 checks: categorized notifications, SSRF guard, price breakdown, delivery tracker + lottery, security.
Offline except the public OVH catalog (cached copy is used when present). Run:
    SECRET_KEY=... python tests/v14_test.py"""
import os, sys, tempfile, shutil, json, re, datetime as dt, ipaddress
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ.setdefault("SECRET_KEY", "x" * 48)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
src = os.path.join(ROOT, "data", "catalog_cache")
if os.path.isdir(src):
    shutil.copytree(src, os.path.join(os.environ["DATA_DIR"], "catalog_cache"), dirs_exist_ok=True)

import ovh.exceptions as oe
from app import notify as N, netguard as NG, pricing as P, tracker as T, ovh_engine as E
from app.db import Base, engine, SessionLocal
from app.models import User, Credential, NotifySetting, Order, Task
from app.security import enc
from app.catalog import get_catalog

FAILS = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond: FAILS.append(name)

Base.metadata.create_all(engine)
from app.db import auto_migrate  # noqa
try: auto_migrate()
except Exception: pass
with SessionLocal() as db:
    u = User(username="alice", password_hash="x"); db.add(u); db.commit(); UID = u.id
    db.add(Credential(user_id=UID, endpoint="ovh-eu", app_key_enc=enc("a"), app_secret_enc=enc("b"), consumer_key_enc=enc("c")))
    TOKEN = "123456789:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKK"
    db.add(NotifySetting(user_id=UID, telegram_token_enc=enc(TOKEN), telegram_chat_id="42",
                         webhook_url_enc=enc("https://hooks.example.com/abc"), stock_debounce=600))
    db.commit()

# ------------------------------------------------------------------ notifications
SENT = []
class R:
    def __init__(self, code=200): self.status_code = code
    def json(self): return {}
def fake_tg(url, json=None, timeout=None):
    SENT.append(("tg", json["text"])); return R()
def fake_hook(url, payload, **k):
    SENT.append(("hook", payload)); return R()
N.httpx.post, N.post_json = fake_tg, fake_hook

def sends(fn):
    SENT.clear(); errs = fn(); return list(SENT), errs

s, e = sends(lambda: N.send(UID, "ORDER_PLACED", "RISE-1 法国 GRA\n首期 €140.20 ≈ ¥1029", title="订单已提交 #987", link="https://www.ovh.com/x?orderId=987&a=<b>"))
tg = [x for k, x in s if k == "tg"]; hk = [x for k, x in s if k == "hook"]
check("notify: telegram + webhook both sent", len(tg) == 1 and len(hk) == 1, s)
check("notify: category header in bold", tg and tg[0].startswith("<b>【提交订单通知】订单已提交 #987</b>"), tg)
check("notify: link escaped in HTML", tg and "&amp;a=&lt;b&gt;" in tg[0], tg)
check("notify: webhook payload has category/title/link", hk and hk[0]["category"] == "order" and hk[0]["title"] == "订单已提交 #987" and hk[0]["link"], hk)

with SessionLocal() as db:
    ns = db.get(NotifySetting, UID); ns.notify_paid = False; db.commit()
s, _ = sends(lambda: N.send(UID, "ORDER_PAID", "x", title="paid"))
check("notify: category switch off -> nothing sent", s == [], s)
s, _ = sends(lambda: N.send(UID, "ORDER_PAID", "x", title="paid", force=True))
check("notify: force (test button) bypasses switch", len(s) == 2, s)

s1, _ = sends(lambda: N.send(UID, "STOCK_FOUND", "a", dedupe_key="t1:plan"))
s2, _ = sends(lambda: N.send(UID, "STOCK_FOUND", "a", dedupe_key="t1:plan"))
s3, _ = sends(lambda: N.send(UID, "STOCK_FOUND", "a", dedupe_key="t1:other"))
check("notify: stock debounce per key", len(s1) == 2 and s2 == [] and len(s3) == 2, (len(s1), len(s2), len(s3)))

def boom(url, json=None, timeout=None): raise RuntimeError(f"connect fail {url}")
N.httpx.post = boom
_, errs = sends(lambda: N.send(UID, "TASK_ERROR", "x", title="err"))
check("notify: errors never contain the bot token", errs and all(TOKEN not in x and "AAAbbb" not in x for x in errs), errs)
N.httpx.post = fake_tg

s, errs = sends(lambda: N.send_test(UID, "delivered"))
check("notify: test send per category", any(k == "tg" and "【交付通知】" in x for k, x in s), s)

# ------------------------------------------------------------------ SSRF guard
for url in ["http://127.0.0.1/x", "http://169.254.169.254/latest/meta-data", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
            "http://10.0.0.5/", "http://192.168.1.1/", "http://100.100.100.200/", "file:///etc/passwd", "gopher://x/",
            "http://user:pw@1.1.1.1/", "http://localhost:8000/", "http://foo.internal/", "http://0.0.0.0/", "http://[fd00:ec2::254]/"]:
    check(f"ssrf: blocked {url}", NG.check_url(url) is not None)
check("ssrf: public IP allowed", NG.check_url("https://1.1.1.1/hook") is None, NG.check_url("https://1.1.1.1/hook"))
# hostname resolving to a private IP is refused at send time too
orig = NG.socket.getaddrinfo
NG.socket.getaddrinfo = lambda *a, **k: [(2, 1, 6, "", ("10.1.2.3", 443))]
try:
    NG.resolve_public("https://evil-rebind.example/"); check("ssrf: DNS to private IP blocked", False)
except NG.Blocked:
    check("ssrf: DNS to private IP blocked", True)
NG.socket.getaddrinfo = orig

# ------------------------------------------------------------------ price breakdown
cat = get_catalog("ovh-eu", "IE", "eco")
b = P.breakdown(cat, "24rise01-v2", "ram-32g-ecc-3200", "softraid-2x512nvme", None, [], "P1M", "default", "IE")
check("price: RISE-1 IE first invoice = 140.20 (verified real invoice)", b and abs(b["total"] - 140.20) < 0.02, b and b.get("total"))
check("price: VAT 23% for IE", b and abs(b["vatRate"] - 0.23) < 1e-9 if b and b["vatRate"] < 1 else b and abs(b["vatRate"] - 1.23) < 1e-9, b and b.get("vatRate"))
check("price: VAT table US=none, FR=20%", P.vat_for("US") in (0, 1.0) and P.vat_for("FR") in (0.2, 1.2), (P.vat_for("US"), P.vat_for("FR")))

# ------------------------------------------------------------------ delivery tracker + lottery
class FakeTrack:
    status = "notPaid"; servers = []; deny_servers = False; calls = []
    def __init__(self, *a, **k): pass
    def get(self, path, **kw):
        FakeTrack.calls.append(("GET", path))
        if path == "/me/order/555/status": return FakeTrack.status
        if path == "/me/order/555/details": return [1]
        if path == "/me/order/555/details/1": return {"domain": "ns1001.ip-203-0-113.eu" if FakeTrack.servers else "*"}
        if path == "/dedicated/server":
            if FakeTrack.deny_servers: raise oe.NotGrantedCall("This call has not been granted")
            return list(FakeTrack.servers)
        if path == "/dedicated/server/ns1001.ip-203-0-113.eu":
            return {"name": "ns1001.ip-203-0-113.eu", "datacenter": "gra3", "ip": "203.0.113.3", "state": "ok", "os": "debian12_64"}
        if path.endswith("/specifications/hardware"):
            return {"processorName": "Xeon-E 2386G", "numberOfProcessors": 1, "coresPerProcessor": 6, "threadsPerProcessor": 12,
                    "memorySize": {"unit": "MB", "value": 65536},
                    "diskGroups": [{"numberOfDisks": 2, "diskSize": {"unit": "GB", "value": 512}, "diskType": "NVME"}]}
        if path.endswith("/serviceInfos"): return {"creation": dt.date.today().isoformat()}
        raise oe.ResourceNotFoundError(path)
    def post(self, *a, **k): raise AssertionError("tracker must not POST")
    put = delete = post
E.make_client = lambda *a, **k: FakeTrack()

with SessionLocal() as db:
    o = Order(user_id=UID, kind="order", order_id="555", plan_code="24rise01-v2", dc="gra", memory="ram-32g-ecc-3200",
              storage="softraid-2x512nvme", endpoint="ovh-eu", status="unpaid", price_with_tax=140.2,
              url="https://www.ovh.com/cgi-bin/order/display-order.cgi?orderId=555",
              detail=json.dumps({"subsidiary": "IE", "ctype": "eco", "payMode": "order"}), track_state="tracking")
    db.add(o); db.commit(); OID = o.id
    ns = db.get(NotifySetting, UID); ns.notify_paid = True; db.commit()

def step(status, **kw):
    FakeTrack.status = status
    for k, v in kw.items(): setattr(FakeTrack, k, v)
    SENT.clear(); r = T.check_order(OID)
    with SessionLocal() as db:
        return r, db.get(Order, OID), [x for k, x in SENT if k == "tg"]

r, o, tg = step("notPaid")
check("tracker: notPaid -> no notification, rescheduled", not tg and o.next_check is not None and o.paid_at is None, (r, tg))
r, o, tg = step("checking")
check("tracker: paid -> 付款通知 once", len(tg) == 1 and "【付款通知】" in tg[0] and o.paid_at is not None, tg)
r, o, tg = step("delivering", servers=[], deny_servers=True)
check("tracker: no repeat paid notification; no server right flagged", not tg and r.get("no_server_right"), (r, tg))
r, o, tg = step("delivered", servers=["ns1001.ip-203-0-113.eu"], deny_servers=False)
spec = json.loads(o.spec_json or "{}")
check("tracker: delivered -> 交付通知 with actual/official", len(tg) == 1 and "【交付通知】" in tg[0] and "实际配置" in tg[0] and "官方配置" in tg[0], tg)
check("tracker: linked by order detail domain", spec.get("server") == "ns1001.ip-203-0-113.eu" and spec.get("linkedBy") == "订单明细", spec.get("linkedBy"))
check("tracker: 64GB vs 32GB -> lottery", spec.get("lottery") is True and any("64 GB" in x for x in spec.get("upgrades", [])), spec.get("upgrades"))
check("tracker: congratulation in delivery message", tg and "恭喜中奖" in tg[0], tg)
check("tracker: state delivered", o.track_state == "delivered" and o.delivered_at and o.status == "delivered", (o.track_state, o.status))
r, o, tg = step("delivered")
check("tracker: no duplicate delivery notification", not tg, tg)
check("tracker: GET only", all(m == "GET" for m, _ in FakeTrack.calls))

# ------------------------------------------------------------------ HTTP security (in-process app)
from fastapi.testclient import TestClient
from app.main import app
from app.security import password_problem as main_pw
B = "http://testserver"
def tok(c, path):
    m = re.search(r'name="csrf" value="([^"]+)"', c.get(path).text); return m.group(1) if m else ""
class _R:   # registration is gone (single-owner); accounts are created server-side
    def __init__(self, url): self.url = url
def register(c, name, pw):
    from app.security import password_problem, hash_password
    if password_problem(pw, name):
        return _R(B + "/register")
    with SessionLocal() as db:
        if db.query(User).filter_by(username=name).first():
            return _R(B + "/register")
        db.add(User(username=name, password_hash=hash_password(pw), is_admin=False)); db.commit()
    login(c, name, pw)
    return _R(B + "/keys")
def login(c, name, pw):
    t = tok(c, "/login"); return c.post("/login", data={"csrf": t, "username": name, "password": pw})

with TestClient(app, base_url=B) as c1, TestClient(app, base_url=B) as c2, TestClient(app, base_url=B) as c3:
    r = c1.get("/login")
    h = r.headers
    check("headers: CSP with nonce, no unsafe-inline scripts", "script-src 'self' 'nonce-" in h.get("content-security-policy", "")
          and "frame-ancestors 'none'" in h.get("content-security-policy", ""), h.get("content-security-policy"))
    check("headers: XFO/nosniff/Referrer-Policy/no-store", h.get("x-frame-options") == "DENY" and h.get("x-content-type-options") == "nosniff"
          and h.get("referrer-policy") and "no-store" in h.get("cache-control", ""))
    n1 = re.search(r"nonce-([\w-]+)", h["content-security-policy"]).group(1)
    check("headers: nonce changes per request", n1 != re.search(r"nonce-([\w-]+)", c1.get("/login").headers["content-security-policy"]).group(1))
    check("headers: every <script> carries the nonce", all(f'nonce="{n1}"' in s or "nonce=" in s for s in re.findall(r"<script[^>]*>", r.text)))

    r = register(c1, "bob", "short1")
    check("password policy: too short refused", "/register" in str(r.url))
    r = register(c1, "bob", "bobbob12345")
    check("password policy: contains username refused", "/register" in str(r.url))
    r = register(c1, "bob", "password123")
    check("password policy: common password refused", "/register" in str(r.url))
    r = register(c1, "bob", "Tr0ub4dor&3x")
    check("register strong password ok", str(r.url).endswith("/keys"), r.url)
    r = register(c2, "carol", "C0rrect-horse-9")
    check("register second user ok", str(r.url).endswith("/keys"), r.url)
    for bad in ("Password123!", "qwerty12345", "1111111111a"):
        check(f"password policy: {bad} refused", main_pw(bad, "zed") is not None)

    with SessionLocal() as db:
        bob = db.query(User).filter_by(username="bob").one()
        carol = db.query(User).filter_by(username="carol").one()
        bt = Task(user_id=bob.id, name="bob task", endpoint="ovh-eu", subsidiary="IE", catalog_type="eco", plan_code="24rise01-v2", poll_interval=10)
        db.add(bt); db.commit()
        bo = Order(user_id=bob.id, kind="order", order_id="777", plan_code="24rise01-v2", status="unpaid"); db.add(bo); db.commit()
        BT, BO = bt.id, bo.id

    check("IDOR: other user's order -> 404", c2.get(f"/orders/{BO}").status_code == 404)
    check("IDOR: other user's task -> 404", c2.get(f"/tasks/{BT}").status_code == 404)
    t2 = tok(c2, "/")
    check("IDOR: other user's order check POST -> 404", c2.post(f"/orders/{BO}/check", data={"csrf": t2}).status_code == 404)
    check("IDOR: owner sees own order", c1.get(f"/orders/{BO}").status_code == 200)

    r = c2.post(f"/tasks/{BT}/delete", data={})
    check("CSRF: POST without token refused", r.status_code in (400, 403), r.status_code)
    t2 = tok(c2, "/")
    r = c2.post("/notify/save", data={"csrf": t2}, headers={"Origin": "https://evil.example"})
    check("CSRF: cross-origin POST -> 403 even with token", r.status_code == 403, r.status_code)
    r = c2.post("/notify/save", data={"csrf": t2, "webhook": "http://169.254.169.254/latest"}, headers={"Origin": B})
    with SessionLocal() as db:
        ns = db.get(NotifySetting, carol.id)
    check("SSRF: metadata webhook not saved", not (ns and ns.webhook_url_enc), r.status_code)

    r = c2.get("/api/price", params={"endpoint": "evil", "plans": "[]"})
    check("api/price: bad endpoint 400", r.status_code == 400)
    r = c2.get("/api/price", params={"endpoint": "ovh-eu", "plans": "{\"a\":1}"})
    check("api/price: bad plans 400", r.status_code == 400)

    # session revocation: second browser for bob, then log out everywhere
    login(c3, "bob", "Tr0ub4dor&3x")
    check("session: second device logged in", c3.get("/account").status_code == 200 and "账号与安全" in c3.get("/account").text)
    t1 = tok(c1, "/account")
    c1.post("/account/logout-all", data={"csrf": t1})
    r = c3.get("/account")
    check("session: logout-all invalidates other devices", "/login" in str(r.url), r.url)
    # password change also revokes
    login(c3, "bob", "Tr0ub4dor&3x"); login(c1, "bob", "Tr0ub4dor&3x")
    t3 = tok(c3, "/account")
    c3.post("/account/password", data={"csrf": t3, "old": "Tr0ub4dor&3x", "new": "N3w-pass-phrase!", "new2": "N3w-pass-phrase!"})
    check("session: password change keeps current device", "/login" not in str(c3.get("/account").url))
    check("session: password change logs out other devices", "/login" in str(c1.get("/account").url))

    # login rate limit (per username)
    with TestClient(app, base_url=B) as c4:
        for i in range(8):
            login(c4, "carol", "wrong-password")
        r = login(c4, "carol", "C0rrect-horse-9")
        check("rate limit: 9th login after 8 failures refused even with right password", "过多" in r.text and "/login" in str(r.url))
        r = c4.get("/login")
    check("single-owner: /register is 404", c1.get("/register").status_code == 404)
    check("single-owner: no admin user toggles", c1.post("/admin/users/1/toggle", data={"csrf": tok(c1, "/account")}).status_code in (404, 405))
    # notify test rate limit
    hits = [c2.post("/notify/test", data={"csrf": tok(c2, "/notify"), "category": "stock"}, headers={"Origin": B}) for _ in range(8)]
    check("rate limit: notify test send limited", any("频繁" in h.text for h in hits))

print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
