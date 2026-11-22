"""v1.9 task items, offline: several machines per task (same model with different config / datacenters), per-item
datacenters, priority order, legacy rows unchanged, deep links.   python tests/task_items_test.py"""
import asyncio, json, os, re, sys, tempfile
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("SECRET_KEY", "unit-only-" + "k" * 40)
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="ovhsn-items-")
import httpx  # noqa: E402
from starlette.datastructures import QueryParams  # noqa: E402
fails = []


def ok(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    cond or fails.append(name)


from app.db import SessionLocal, auto_migrate  # noqa: E402
from app.models import Task, User, task_plans  # noqa: E402
from app.security import hash_password  # noqa: E402
from app import main  # noqa: E402
auto_migrate()
PW = "Correct-Horse-9x!"
with SessionLocal() as db:
    db.add(User(id=1, username="owner", password_hash=hash_password(PW), is_admin=True, lang=""))
    # a pre-v1.9 task: plan_codes + plan_configs + task-wide datacenters
    db.add(Task(id=1, user_id=1, endpoint="ovh-eu", subsidiary="IE", catalog_type="eco", plan_code="24rise01-v2",
                plan_codes="24rise01-v2,24adv01-v3", datacenters="gra,rbx",
                plan_configs=json.dumps({"24rise01-v2": {"label": "RISE-1", "memory": "ram-32g", "storage": "", "bandwidth": "", "type": "eco"},
                                         "24adv01-v3": {"label": "ADVANCE-1", "memory": "", "storage": "", "bandwidth": "", "type": "baremetalServers"}})))
    db.commit()
    legacy = db.get(Task, 1)
    lp = task_plans(legacy)
    ok("legacy task: plans read from plan_codes/plan_configs", [p["planCode"] for p in lp] == ["24rise01-v2", "24adv01-v3"] and lp[0]["memory"] == "ram-32g")
    ok("legacy task: no per-item dcs (worker keeps using the task-wide list)", all("dcs" not in p for p in lp))
    fp = main._form_plans(legacy)
    ok("legacy task in the form: task-wide datacenters copied onto every item", all(p["dcs"] == ["gra", "rbx"] for p in fp) and fp[1]["type"] == "baremetalServers", fp)
    nice = main.nice_plans(legacy)
    ok("legacy task detail: datacenter label from the task-wide list", all(p["dcL"] for p in nice) and "→" in nice[0]["dcL"], nice[0].get("dcL"))

dl = main._deep_link_items(QueryParams("plan=24sk202&type=eco&dc=gra&memory=ram-32g-noecc-2133&storage=softraid-2x2000sa"))
ok("deep link (Live stock 'New task'): one item with its config + datacenter",
   dl == [{"planCode": "24sk202", "type": "eco", "label": "", "memory": "ram-32g-noecc-2133", "storage": "softraid-2x2000sa", "bandwidth": "", "dcs": ["gra"]}], dl)
dl = main._deep_link_items(QueryParams("item=24rise01-v2|eco|ram-64g|softraid-2x512nvme|gra+rbx&item=24adv01-v3|baremetalServers|||&item=bad plan|eco|||gra&item=24sk202|weird|||sbg,XX1"))
ok("deep link: repeated item= keeps order, several DCs, any config, drops bad codes",
   [(x["planCode"], x["type"], x["dcs"]) for x in dl] == [("24rise01-v2", "eco", ["gra", "rbx"]), ("24adv01-v3", "baremetalServers", []), ("24sk202", "eco", ["sbg"])], dl)


async def run():
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app, client=("127.0.0.1", 1)), base_url="http://t")
    r = await c.get("/login"); tok = re.search(r'name="csrf" value="([^"]+)"', r.text).group(1)
    r = await c.post("/login", data={"csrf": tok, "username": "owner", "password": PW})
    ok("login", r.status_code == 303 and r.headers["location"] == "/")
    r = await c.get("/tasks/new?plan=24sk202&type=eco&dc=gra&memory=ram-32g-noecc-2133&storage=softraid-2x2000sa")
    ok("new-task page renders the shared stock browser", r.status_code == 200 and 'id="rows"' in r.text and "stocktable.js" in r.text and 'id="tray"' in r.text)
    m = re.search(r"const INIT = \{plans: (\[.*?\]), extras", r.text)
    ok("deep link prefills the selection", m and json.loads(m.group(1))[0]["dcs"] == ["gra"], m and m.group(1))
    r = await c.get("/stock")
    ok("live stock page renders the same component", r.status_code == 200 and 'id="rows"' in r.text and "stocktable.js" in r.text and "new StockBrowser" in r.text)
    tok = re.search(r'name="csrf" value="([^"]+)"', (await c.get("/tasks/new")).text).group(1)
    items = [
        {"planCode": "24rise01-v2", "type": "eco", "label": "RISE-1", "memory": "ram-32g-ecc-3200", "storage": "softraid-2x512nvme", "bandwidth": "", "dcs": ["gra", "rbx"]},
        {"planCode": "24rise01-v2", "type": "eco", "label": "RISE-1", "memory": "ram-64g-ecc-3200", "storage": "softraid-2x512nvme", "bandwidth": "", "dcs": ["bhs"]},
        {"planCode": "24adv01-v3", "type": "baremetalServers", "label": "ADVANCE-1", "memory": "", "storage": "", "bandwidth": "", "dcs": []},
        {"planCode": "24rise01-v2", "type": "eco", "label": "RISE-1", "memory": "ram-32g-ecc-3200", "storage": "softraid-2x512nvme", "bandwidth": "", "dcs": ["gra", "rbx"]},  # exact dup
        {"planCode": "24rise01-v2", "type": "eco", "label": "x", "memory": "", "storage": "", "bandwidth": "", "dcs": ["gra", "<b>", "GRA"]},
    ]
    r = await c.post("/tasks/save", data={"csrf": tok, "endpoint": "ovh-eu", "subsidiary": "IE", "plans_json": json.dumps(items),
                                          "pay_mode": "dry", "quantity": "1", "max_orders": "1", "poll_interval": "10"})
    ok("save multi-machine task", r.status_code == 303 and re.fullmatch(r"/tasks/\d+", r.headers["location"]), r.headers.get("location"))
    tid = int(r.headers["location"].rsplit("/", 1)[1])
    with SessionLocal() as db:
        t = db.get(Task, tid)
        tp = task_plans(t)
        ok("items kept in order, same model twice with different config", [(p["planCode"], p["memory"]) for p in tp][:3] ==
           [("24rise01-v2", "ram-32g-ecc-3200"), ("24rise01-v2", "ram-64g-ecc-3200"), ("24adv01-v3", "")], tp)
        ok("exact duplicate dropped; bad datacenter codes dropped", len(tp) == 4 and tp[3]["dcs"] == ["gra"], [p["dcs"] for p in tp])
        ok("per-item datacenters stored", tp[0]["dcs"] == ["gra", "rbx"] and tp[1]["dcs"] == ["bhs"] and tp[2]["dcs"] == [])
        ok("type stored per item", tp[2]["type"] == "baremetalServers")
        ok("legacy columns still filled (plan_codes unique, plan_code = first)", t.plan_codes == "24rise01-v2,24adv01-v3" and t.plan_code == "24rise01-v2")
        ok("task-wide datacenters empty when an item takes any DC", t.datacenters == "")
        fp = main._form_plans(t)
        ok("edit form gets the same items back", [p["dcs"] for p in fp] == [["gra", "rbx"], ["bhs"], [], ["gra"]])
    r = await c.get(f"/tasks/{tid}")
    ok("task detail lists every item with its datacenters", r.status_code == 200 and r.text.count('class="small wrap"') >= 4 and "Per model" in r.text)
    # legacy form post (datacenters list, items without dcs) still works
    r = await c.post("/tasks/save", data={"csrf": tok, "endpoint": "ovh-eu", "subsidiary": "IE", "pay_mode": "dry", "datacenters": ["sbg", "gra"],
                                          "plans_json": json.dumps([{"planCode": "24sk202", "type": "eco"}])})
    tid2 = int(r.headers["location"].rsplit("/", 1)[1])
    with SessionLocal() as db:
        t = db.get(Task, tid2)
        ok("legacy form fields: task-wide datacenters applied to items", task_plans(t)[0]["dcs"] == ["sbg", "gra"] and t.datacenters == "sbg,gra")
    await c.aclose()

asyncio.run(run())
print("ALL PASS" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
