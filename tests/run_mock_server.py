"""Run the app against a fake OVH account API (for UI checks / screenshots) with a FRESH demo database.
python tests/run_mock_server.py [port] [mode]     mode: ok | denied | invalid | empty

Seeds /tmp/ovh-mock-data-<port>: one demo owner (random password, see tests/mockcred.py), fake OVH keys, four tasks
(one per order mode + a multi-plan dry run, all stopped) and order history rows, one of which matches a fake OVH
account order. Nothing from ./data is read; availability and catalogs come from the live public OVH API."""
import datetime as dt
import json
import os
import shutil
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
port = int(sys.argv[1]) if len(sys.argv) > 1 else 8810
mode = sys.argv[2] if len(sys.argv) > 2 else "ok"
dst = f"/tmp/ovh-mock-data-{port}"
shutil.rmtree(dst, ignore_errors=True)
os.makedirs(dst)
os.environ["DATA_DIR"] = dst
os.environ.setdefault("SECRET_KEY", "mock-only-" + "k" * 40)

import mockcred  # noqa: E402
from app import ovh_engine as E  # noqa: E402
from app.db import SessionLocal, auto_migrate  # noqa: E402
from app.models import Credential, Order, Task, User  # noqa: E402
from app.security import enc, hash_password  # noqa: E402
from fake_account import FakeAccountOVH  # noqa: E402

USER, PASSWORD = mockcred.ensure()
ZH = os.environ.get("DEFAULT_LANG", "en") == "zh"   # demo task names follow the demo language (English by default)
N = (["RISE-1 法国 只预览", "RISE-1 只下单", "ADVANCE-1 自动付款", "多型号 Rise+Advance"] if ZH else
     ["RISE-1 France preview", "RISE-1 order only", "ADVANCE-1 auto-pay", "Multi-model Rise+Advance"])
auto_migrate()
now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
RISE = {"type": "eco", "label": "RISE-1"}
ADV = {"type": "baremetalServers", "label": "ADVANCE-1"}


def task(name, plans, **kw):
    first = plans[0]
    return Task(user_id=1, name=name, endpoint="ovh-eu", subsidiary="IE", catalog_type=first[1]["type"],
                plan_code=first[0], plan_codes=",".join(p for p, _ in plans),
                plan_configs=json.dumps({p: c for p, c in plans}, ensure_ascii=False),
                plan_label=" / ".join(c["label"] for _, c in plans), status="stopped", enabled=False,
                poll_interval=10, **{"max_orders": 1, **kw})


with SessionLocal() as db:
    db.add(User(id=1, username=USER, password_hash=hash_password(PASSWORD), is_admin=True,
                last_login_at=now - dt.timedelta(hours=3), last_login_ip="203.0.113.50"))
    db.commit()
    db.add(Credential(user_id=1, endpoint="ovh-eu", app_key_enc=enc("fake-ak"), app_secret_enc=enc("fake-as"),
                      consumer_key_enc=enc("fake-ck")))
    db.add_all([
        task(N[0], [("24rise01-v2", RISE)], datacenters="gra,rbx", dry_run=True, auto_pay=False, any_config=True),
        task(N[1], [("24rise01-v2", RISE)], datacenters="gra,rbx,sbg", dry_run=False, auto_pay=False, price_cap=160.0),
        task(N[2], [("24adv01-v3", ADV)], dry_run=False, auto_pay=True, price_cap=300.0, prebuild=True),
        task(N[3], [("24rise01-v2", RISE), ("24adv01-v3", ADV)], dry_run=True, auto_pay=False, any_config=True,
             orders_done=1, max_orders=5),
        # created from raw codes without a label (what the screenshot showed): UI must still show KS-B + readable config
        task("", [("25skb012", {"type": "eco", "label": "", "memory": "ram-32g-ecc-1333-25skb01",
                                "storage": "noraid-1x120ssd-25skb01", "bandwidth": "bandwidth-500-25sk"})],
             dry_run=True, auto_pay=False),
    ])
    db.commit()
    if os.environ.get("MOCK_NOTIFY_TASK") == "1":   # v1.8 demo: a 仅提醒 / notify-only task (id 6)
        db.add(task("KS-2 Telegram alert" if not ZH else "KS-2 补货提醒", [("24sk202", {"type": "eco", "label": "KS-2", "memory": "",
                    "storage": "", "bandwidth": ""})], datacenters="gra,rbx,sbg", dry_run=True, auto_pay=False,
                    notify_only=True, alert_cooldown=15))
        db.commit()
    detail = {"subsidiary": "IE", "ctype": "eco", "payMode": "pay", "currency": "EUR"}
    db.add_all([
        Order(user_id=1, task_id=4, kind="order", plan_code="24rise01", dc="gra", memory="ram-32g-ecc-3200-24rise01",
              storage="softraid-2x512nvme-24rise01", order_id="231890455", endpoint="ovh-eu",
              url="https://www.ovh.com/cgi-bin/order/displayOrder.cgi?orderId=231890455", price_with_tax=140.2,
              status="paid", detail=json.dumps(detail), ts=now - dt.timedelta(days=2), paid_at=now - dt.timedelta(days=2),
              track_state="tracking"),
        Order(user_id=1, task_id=1, kind="preview", plan_code="24rise01-v2", dc="rbx", memory="ram-32g-ecc-3200",
              storage="softraid-2x512nvme", price_with_tax=140.2, status="dry-run",
              detail=json.dumps({**detail, "payMode": "dry"}), ts=now - dt.timedelta(hours=5)),
    ])
    db.commit()

# delivered order #1 with a "lottery" (better than catalog) config, for the order-detail page (needs the public catalog)
try:
    from app import specs as S
    from app.tracker import official_for
    with SessionLocal() as db:
        o = db.get(Order, 1)
        off, cats = official_for(o)
        hw = {"processorName": (off["cpu"]["model"] or "Xeon-E 2386G"), "numberOfProcessors": 1,
              "coresPerProcessor": off["cpu"].get("cores") or 6, "threadsPerProcessor": off["cpu"].get("threads") or 12,
              "memorySize": {"unit": "MB", "value": int((off["ramGb"] or 32) * 2 * 1024)},
              "diskGroups": [{"numberOfDisks": 2, "diskSize": {"unit": "GB", "value": 960}, "diskType": "NVME"}]}
        srv = {"datacenter": "gra3", "ip": "203.0.113.34", "state": "ok", "os": "debian12_64"}
        act = S.actual(hw, srv)
        cmp_ = S.compare(off, act, S.cpu_scores([cats[0]]))
        spec = {"official": off, "actual": act, "server": "ns3201234.ip-203-0-113.eu", "linkedBy": "订单明细" if ZH else "Order details", **cmp_,
                "serverState": "ok", "ip": "203.0.113.34"}
        o.spec_json = json.dumps(spec, ensure_ascii=False); o.server_name = spec["server"]
        o.delivered_at = o.ts + dt.timedelta(hours=2, minutes=14)
        o.ovh_status, o.track_state, o.status, o.notified = "delivered", "delivered", "delivered", "delivered,paid"
        db.commit()
        print("seeded lottery order:", cmp_.get("upgrades"), flush=True)
except Exception as e:  # offline: the page just shows "no official spec"
    print("lottery seed skipped:", type(e).__name__, e, flush=True)

FakeAccountOVH.mode = mode
FakeAccountOVH.delay = 0.15   # make the skeleton visible for a moment
E.make_client = lambda *a, **k: FakeAccountOVH()

import uvicorn  # noqa: E402
from app.main import app  # noqa: E402
from app import account as A  # noqa: E402
from app import tracker as T  # noqa: E402
T.Tracker.start = lambda self: None   # fake keys: don't track the seeded orders against the fake API


@app.get("/__mock/reset")
def _mock_reset():  # test launcher only: drop the account cache so the skeleton shows again
    A.cache.s.clear()
    return {"ok": True}


print(f"mock server http://127.0.0.1:{port}  login: {USER} / (password in {mockcred.PATH})", flush=True)
uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
