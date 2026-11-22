"""Hotfix v1.6: the real worker on KS-LE-3 (27skle03-v1) with LIVE public availability and a fake OVH account.
The 64 GB variant is listed in stock by OVH but has no catalog/cart addon => the task must keep running (no crash,
no checkout) and say why; the 32 GB task must explain that only a non-orderable config is in stock.
    python tests/ksle3_worker_test.py"""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mock_order_test as MT   # sets DATA_DIR/SECRET_KEY, fakes the OVH client (main() not run)
from mock_order_test import W, SessionLocal, Task, TaskLog, FakeOVH

FAILS = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond: FAILS.append(name)

async def case(mem, wait=14, **kw):
    FakeOVH.calls = []
    tid = MT.setup(plan_code="27skle03-v1", catalog_type="eco", memory=mem, storage="softraid-3x4000sa", datacenters="gra",
                   any_config=False, auto_pay=False, price_cap=200.0, **kw)
    W.manager.start(tid)
    await asyncio.sleep(wait)
    running = tid in W.manager.runners
    if running: await W.manager.stop(tid)
    with SessionLocal() as db:
        t = db.get(Task, tid); logs = [(l.marker, l.msg) for l in db.query(TaskLog).filter_by(task_id=tid).order_by(TaskLog.id)]
    for m, msg in logs: print(f"   {m:10s} {msg[:220]}")
    return running, t, logs, [p for m, p in FakeOVH.calls if m == "POST" and p.endswith("/checkout")]

async def main():
    running, t, logs, co = await case("ram-64g-ddr4-ecc-2666")
    check("64G task keeps running (no '任务停止')", running and not any(m == "ERROR" and "任务停止" in s for m, s in logs), t.status_text)
    check("64G task warns clearly", any(m == "WARN" and "无法下单" in s and "64 GB" in s for m, s in logs))
    check("64G task never checks out", not co, co)
    running, t, logs, co = await case("ram-32g-ddr4-ecc-2666")
    check("32G task running", running)
    stock64 = any(m == "STOCK" and "64 GB" in s for m, s in logs)
    if stock64:
        check("32G task: NO_MATCH explains only non-orderable 64G in stock",
              any(m == "NO_MATCH" and "64 GB" in s and "无法下单" in s for m, s in logs))
        check("32G task: heartbeat carries the hint", any(m == "HEARTBEAT" and "64 GB" in s for m, s in logs))
    else:
        print("   (64G variant not in stock right now; explanation checks skipped)")
    check("32G task: no checkout without matching stock", not co or any(m == "STOCK" and "32 GB" in s for m, s in logs), co)
    await W.hub.close()
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1 if FAILS else 0)

asyncio.run(main())
