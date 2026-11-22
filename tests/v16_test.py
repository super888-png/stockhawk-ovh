"""v1.6 checks: datacenter stock map (app/dcmap.py + GET /api/stock/map). The shared poller and FX are faked; only the public OVH catalog is fetched (cached copy used when present).
    python tests/v16_test.py"""
import os, sys, tempfile, shutil, re, time
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ.setdefault("SECRET_KEY", "x" * 48)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
src = os.path.join(ROOT, "data", "catalog_cache")
if os.path.isdir(src):
    shutil.copytree(src, os.path.join(os.environ["DATA_DIR"], "catalog_cache"), dirs_exist_ok=True)

from app import dcmap as D
from app.db import Base, engine, SessionLocal, auto_migrate
from app.models import User, Task
from app.security import hash_password

FAILS = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond: FAILS.append(name)

# ------------------------------------------------------------------ mapping (single source of truth)
for av, lv in [("1H-high", "high"), ("1H-low", "low"), ("24H", "low"), ("72H", "wait"), ("240H", "wait"), ("480H", "wait"),
               ("1440H", "wait"), ("unavailable", "none"), ("unknown", "unknown"), ("comingSoon", "unknown"), (None, "unknown"), ("", "unknown")]:
    check(f"level({av!r}) = {lv}", D.level(av) == lv, D.level(av))
check("legend: 5 levels best-first", [l["key"] for l in D.legend()] == ["high", "low", "wait", "none", "unknown"])
for dc, site in [("gra", "gra"), ("eu-west-par-a", "par"), ("eu-west-par-c", "par"), ("ca-east-tor-a", "tor"), ("yyz", "tor"),
                 ("mum", "ynm"), ("BHS", "bhs")]:
    check(f"site_of({dc}) = {site}", D.site_of(dc) == site, D.site_of(dc))
check("every core site has coordinates", all(c in D.SITES for c in D.CORE))
check("coordinates sane", all(-90 <= v[2] <= 90 and -180 <= v[3] <= 180 for v in D.SITES.values()))

AVR = {"1H-high": 0, "1H-low": 1, "24H": 2, "72H": 3, "240H": 4, "unavailable": 10, "unknown": 9}
rows = [
    {"endpoint": "ovh-eu", "plan": "p1", "name": "KS-B", "cpu": "E5", "dc": "gra", "availability": "unavailable"},
    {"endpoint": "ovh-eu", "plan": "p1", "name": "KS-B", "cpu": "E5", "dc": "gra", "availability": "1H-high"},
    {"endpoint": "ovh-eu", "plan": "p2", "name": "RISE-1", "cpu": "", "dc": "gra", "availability": "72H"},
    {"endpoint": "ovh-eu", "plan": "p1", "name": "KS-B", "cpu": "E5", "dc": "eu-west-par-a", "availability": "1H-low"},
    {"endpoint": "ovh-eu", "plan": "p1", "name": "KS-B", "cpu": "E5", "dc": "eu-west-par-b", "availability": "unavailable"},
    {"endpoint": "ovh-eu", "plan": "p1", "name": "KS-B", "cpu": "E5", "dc": "rbx", "availability": "unavailable"},
    {"endpoint": "ovh-eu", "plan": "p1", "name": "KS-B", "cpu": "E5", "dc": "zzz", "availability": "1H-high"},  # unmapped
]
S = {s["code"]: s for s in D.aggregate(rows, AVR)}
check("aggregate: core sites always present", all(c in S for c in D.CORE), sorted(S))
check("aggregate: unmapped code dropped", "zzz" not in S)
check("aggregate: best level wins (gra = high)", S["gra"]["level"] == "high" and S["gra"]["inStock"] == 1, S["gra"])
check("aggregate: counts per level", S["gra"]["counts"] == {"high": 1, "low": 0, "wait": 1, "none": 1, "unknown": 0}, S["gra"]["counts"])
check("aggregate: per-plan best + in-stock count", [(p["name"], p["best"], p["inStock"], p["total"]) for p in S["gra"]["plans"]]
      == [("KS-B", "1H-high", 1, 2), ("RISE-1", "72H", 0, 1)], S["gra"]["plans"])
check("aggregate: Paris zones fold into par", S["par"]["level"] == "low" and S["par"]["zones"] == ["eu-west-par-a", "eu-west-par-b"], S["par"])
check("aggregate: rbx none", S["rbx"]["level"] == "none" and S["rbx"]["monitored"])
check("aggregate: unmonitored site = unknown", not S["sgp"]["monitored"] and S["sgp"]["level"] == "unknown")
order = list(S)
check("aggregate: sorted best first", order.index("gra") < order.index("par") < order.index("rbx") < order.index("sgp"), order[:6])

# ------------------------------------------------------------------ endpoint (in-process app, fake poller)
from fastapi.testclient import TestClient
from app import main as M

class FakeFeed:
    def __init__(self, plan):
        self.plan, self.last_ok, self.error = plan, time.time(), None
        self.data = [{"planCode": plan, "memory": "ram-32g", "storage": "softraid-2x512nvme", "datacenters": [
            {"datacenter": "gra", "availability": "1H-high"}, {"datacenter": "bhs", "availability": "72H"},
            {"datacenter": "eu-west-par-a", "availability": "unavailable"}, {"datacenter": "sgp", "availability": "1H-low"}]}]
    def interval(self): return 10.0
WATCHED = []
def fake_watch(ep, pc):
    WATCHED.append((ep, pc)); return FakeFeed(pc)
M.hub.watch = fake_watch
M.FX.rate = lambda code="EUR": {"base": code, "rate": 7.3, "source": "test", "live": False, "fetched": None}

Base.metadata.create_all(engine)
try: auto_migrate()
except Exception: pass
with SessionLocal() as db:
    for n in ("owner", "other"):
        db.add(User(username=n, password_hash=hash_password("Tr0ub4dor&3x-" + n), is_admin=(n == "owner")))
    db.commit()
    OID = db.query(User).filter_by(username="owner").one().id

B = "http://testserver"
def login(c, name):
    t = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    c.post("/login", data={"csrf": t, "username": name, "password": "Tr0ub4dor&3x-" + name})

with TestClient(M.app, base_url=B) as c, TestClient(M.app, base_url=B) as c2:
    r = c.get("/api/stock/map")
    check("auth: anonymous -> 401 JSON", r.status_code == 401, r.status_code)
    login(c, "owner")
    r = c.get("/api/stock/map"); d = r.json()
    check("no tasks: demo scope (RISE-1 / ADVANCE-1)", d["scope"]["kind"] == "demo" and {p for _, p in WATCHED} == {"24rise01-v2", "24adv01-v3"}, (d["scope"], WATCHED))
    check("response: levels + sites + time", [l["key"] for l in d["levels"]][0] == "high" and d["sites"] and d["nowTs"])
    s = {x["code"]: x for x in d["sites"]}
    check("endpoint: gra high, sgp low, bhs wait, par none", (s["gra"]["level"], s["sgp"]["level"], s["bhs"]["level"], s["par"]["level"]) == ("high", "low", "wait", "none"),
          {k: s[k]["level"] for k in ("gra", "sgp", "bhs", "par")})
    check("endpoint: no-store + CSP headers", "no-store" in r.headers.get("cache-control", "") and "default-src 'self'" in r.headers.get("content-security-policy", ""))
    check("endpoint: GET only (POST refused)", c.post("/api/stock/map", headers={"Origin": B}).status_code in (403, 405))

    with SessionLocal() as db:
        db.add(Task(user_id=OID, name="ks", endpoint="ovh-eu", subsidiary="IE", catalog_type="eco", plan_code="25skb01", poll_interval=10))
        db.commit()
    WATCHED.clear()
    d = c.get("/api/stock/map").json()
    check("with tasks: task-plan scope", d["scope"]["kind"] == "tasks" and d["scope"]["plans"] == 1 and WATCHED == [("ovh-eu", "25skb01")], (d["scope"], WATCHED))
    plans = [p["plan"] for p in {x["code"]: x for x in d["sites"]}["gra"]["plans"]]
    check("tooltip data lists the task plan", plans == ["25skb01"], plans)

    login(c2, "other"); WATCHED.clear()
    d2 = c2.get("/api/stock/map").json()
    check("isolation: another user's tasks are not in their scope", d2["scope"]["kind"] != "tasks" and ("ovh-eu", "25skb01") not in WATCHED, (d2["scope"], WATCHED))
    html = c.get("/").text
    check("dashboard renders the map card + nonce'd script", 'id="dcmap"' in html and re.search(r'<script nonce="[^"]+" src="/static/dcmap\.js', html) is not None)
    svg = c.get("/static/worldmap.svg")
    check("worldmap.svg served from app static", svg.status_code == 200 and 'id="world"' in svg.text and 'id="europe"' in svg.text and "Natural Earth" in svg.text)

print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
