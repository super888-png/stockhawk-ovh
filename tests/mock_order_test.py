"""Drive the real worker + engine through the ordering path with a fake OVH client.
Availability comes from the LIVE public OVH API; only authenticated calls are faked."""
import asyncio, os, sys, tempfile, time, json, itertools
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ.setdefault("SECRET_KEY", "x" * 40)
os.environ["MIN_POLL_INTERVAL"] = "3"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ovh.exceptions as oe, requests
from app import ovh_engine as E, worker as W
from app.db import Base, engine, SessionLocal
from app.models import User, Task, Credential, TaskLog, Order
from app.security import enc

class FakeOVH:
    mode = "ok"  # ok | reject | timeout | noprice
    fail_cfg_dc = None  # make cart configuration fail for this datacenter
    expect_autopay = True
    calls = []
    def __init__(self, *a, **k):
        pass
    carts, ids = {}, itertools.count(100)
    def _log(self, m, p, kw): FakeOVH.calls.append((m, p.split("?")[0]))
    def get(self, path, **kw):
        self._log("GET", path, kw)
        parts = path.strip("/").split("/")
        if path == "/me": return {"nichandle": "fk123-ovh", "ovhSubsidiary": "IE", "currency": {"code": "EUR"}}
        if path.startswith("/me/order/") and path.endswith("/status"): return "delivering"
        if path == "/me/order": return [987]
        if path.startswith("/me/order/"): return {"date": "now", "priceWithTax": {"text": "€70"}, "url": "https://x/987"}
        if parts[:2] == ["order", "cart"]:
            c = self.carts[parts[2]]
            if len(parts) == 3: return {"items": list(c["items"]), "expire": "2099-01-01T00:00:00+00:00"}
            if parts[3] == "checkout":
                if FakeOVH.mode == "noprice": return {"prices": {"weird": "n/a"}}
                return {"prices": {"withTax": {"value": 70.11}, "withoutTax": {"value": 57.0}}}
            if parts[3] == "item":
                iid = int(parts[4]); it = c["items"][iid]
                if len(parts) == 5: return {"settings": {"planCode": it["planCode"]}, "prices": [{"label": "TOTAL", "price": {"value": it["price"]}}]}
                if len(parts) == 6: return list(range(len(it["cfg"])))
                lab = list(it["cfg"])[int(parts[6])]; return {"label": lab, "value": it["cfg"][lab]}
        raise oe.ResourceNotFoundError(path)
    def post(self, path, **kw):
        self._log("POST", path, kw)
        parts = path.strip("/").split("/")
        if path == "/order/cart":
            cid = f"cart{next(FakeOVH.ids)}"; self.carts[cid] = {"items": {}}; return {"cartId": cid, "expire": "2099-01-01T00:00:00+00:00"}
        c = self.carts[parts[2]]
        if parts[3] == "assign": return None
        if parts[3] in ("eco", "baremetalServers") and len(parts) == 4:
            iid = next(FakeOVH.ids); c["items"][iid] = {"planCode": kw["planCode"], "price": 56.99, "cfg": {}}; return {"itemId": iid}
        if parts[3] in ("eco", "baremetalServers") and parts[4] == "options":
            iid = next(FakeOVH.ids); c["items"][iid] = {"planCode": kw["planCode"], "price": 0.0, "cfg": {}}; return {"itemId": iid}
        if parts[3] == "item" and parts[5] == "configuration":
            if FakeOVH.fail_cfg_dc and kw["value"] == FakeOVH.fail_cfg_dc:
                raise oe.BadParametersError(f"{kw['value']} refused (test)")
            c["items"][int(parts[4])]["cfg"][kw["label"]] = kw["value"]; return {}
        if parts[3] == "checkout":
            if FakeOVH.mode == "reject": raise oe.BadParametersError("Item is out of stock")
            if FakeOVH.mode == "timeout": raise requests.exceptions.ReadTimeout("read timed out")
            assert kw["autoPayWithPreferredPaymentMethod"] is FakeOVH.expect_autopay and kw["waiveRetractationPeriod"] is True
            FakeOVH.oid = getattr(FakeOVH, "oid", 987653) + 1
            return {"orderId": FakeOVH.oid, "url": "https://www.ovh.com/cgi-bin/order/display-order.cgi?orderId=987654", "prices": {"withTax": {"value": 70.11}}}
        raise oe.ResourceNotFoundError(path)
    def delete(self, path, **kw):
        self._log("DELETE", path, kw); self.carts.pop(path.strip("/").split("/")[2], None)

E.make_client = lambda *a, **k: FakeOVH()
W.E.make_client = E.make_client

def setup(**kw):
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        u = db.query(User).first()
        if not u:
            u = User(username="t", password_hash="x"); db.add(u); db.commit()
            db.add(Credential(user_id=u.id, endpoint="ovh-eu", app_key_enc=enc("a"), app_secret_enc=enc("b"), consumer_key_enc=enc("c"))); db.commit()
        base = dict(user_id=u.id, endpoint="ovh-eu", subsidiary="IE", catalog_type="eco", plan_code="24rise01-v2",
                    datacenters="", poll_interval=3, dry_run=False, auto_pay=True, max_orders=1, enabled=True, any_config=True)
        base.update(kw); t = Task(**base); db.add(t); db.commit(); return t.id

async def run_case(name, mode, wait=40, pre=None, **kw):
    FakeOVH.mode, FakeOVH.calls = mode, []
    tid = setup(**kw)
    if pre: pre(tid)
    W.manager.start(tid)
    t0 = time.time()
    while time.time() - t0 < wait and tid in W.manager.runners:
        await asyncio.sleep(0.5)
    if tid in W.manager.runners: await W.manager.stop(tid)
    with SessionLocal() as db:
        t = db.get(Task, tid); logs = db.query(TaskLog).filter_by(task_id=tid).order_by(TaskLog.id).all()
        orders = db.query(Order).filter_by(task_id=tid).all()
        print(f"\n##### {name}: status={t.status} orders_done={t.orders_done} text={t.status_text}")
        for l in logs:
            if l.marker in ("STOCK",): continue
            print(f"  {l.marker:15s} {l.msg[:260]}")
        print("  orders:", [(o.kind, o.order_id, o.dc, o.memory, o.storage, o.price_with_tax, o.status) for o in orders])
        print("  OVH calls:", len(FakeOVH.calls), "checkout POSTs:", sum(1 for m, p in FakeOVH.calls if m == "POST" and p.endswith("/checkout")),
              "deleted:", [p.split("/")[-1] for m, p in FakeOVH.calls if m == "DELETE"][:6])
    return tid

PIN = dict(memory="ram-32g-ecc-3200", storage="softraid-2x512nvme")

async def round3():
    # G: fast path prefers an in-stock DC whose prebuilt cart is ready (GRA prebuild fails => RBX fast path);
    #    L: a cart id left in the registry by a "crash" is deleted at start
    FakeOVH.fail_cfg_dc = "gra"
    await run_case("G 快速通道优先选有预建购物车的机房 + L 清理遗留购物车", "ok", price_cap=200.0, prebuild=True,
                   any_config=False, datacenters="gra,rbx", wait=45, poll_interval=8,
                   pre=lambda tid: W.registry_set(tid, ["cartLEFTOVER"]), **PIN)
    FakeOVH.fail_cfg_dc = None
    print("  registry after stop:", W.registry_get(1), open(W.REGISTRY).read())
    # K: unparseable first-invoice price, no cap => refuse (autobuy_focus "refusing to order")
    await run_case("K 首期价格无法解析且无上限→拒绝下单", "noprice", price_cap=None, wait=12)
    # J: an exception inside one poll iteration is logged and polling continues
    orig_ls, n = E.live_stock, {"c": 0}
    def boom(*a, **k):
        n["c"] += 1
        if n["c"] == 1: raise ValueError("synthetic poll error")
        return orig_ls(*a, **k)
    W.E.live_stock = boom
    await run_case("J 单轮异常→记录并继续→下单", "ok", price_cap=200.0, wait=30)
    W.E.live_stock = orig_ls
    # M: task stopped while the cart is being built => no checkout, cart deleted
    orig_pv = E.preview_cart
    def pv(*a, **k):
        r = orig_pv(*a, **k)
        for r_ in W.manager.runners.values(): r_.stop_evt.set()
        return r
    W.E.preview_cart = pv
    await run_case("M 结账前任务被停止→放弃结账并删除购物车", "ok", price_cap=200.0, wait=15)
    W.E.preview_cart = orig_pv
    # N: crash after a successful checkout => pending_confirm, never retried / restarted
    orig_ro = W.TaskRunner.record_order
    def ro(self, kind, *a, **k):
        if kind == "order": raise OSError("disk full (test)")
        return orig_ro(self, kind, *a, **k)
    W.TaskRunner.record_order = ro
    await run_case("N 结账成功后出错→待人工确认，不重复下单", "ok", price_cap=200.0, max_orders=3, wait=25)
    W.TaskRunner.record_order = orig_ro
    # I: process died during checkout => on restart the task is NOT resumed
    tid = setup(price_cap=200.0, status="checkout")
    await W.manager.resume_all()
    with SessionLocal() as db:
        t = db.get(Task, tid)
        print(f"\n##### I 结账中重启→不自动恢复: status={t.status} enabled={t.enabled} running={tid in W.manager.runners}")
        print("  ", [l.msg for l in db.query(TaskLog).filter_by(task_id=tid)])
    for x in list(W.manager.runners): await W.manager.stop(x)

async def main():
    if os.environ.get("ONLY_UNPAID"):
        FakeOVH.expect_autopay = False
        tid = await run_case("O 只下单不付款 max_orders=2", "ok", price_cap=200.0, auto_pay=False, max_orders=2, wait=60, poll_interval=3)
        with SessionLocal() as db:
            t = db.get(Task, tid); os_ = db.query(Order).filter_by(task_id=tid, kind="order").all()
            ok = (t.status == "done" and t.orders_done == 2 and len(os_) == 2 and all(o.status == "unpaid" for o in os_)
                  and all(o.track_state == "tracking" and o.next_check for o in os_)
                  and all(json.loads(o.detail).get("payMode") == "order" for o in os_)
                  and not any(p.endswith("/status") for m, p in FakeOVH.calls))
            print("UNPAID CHECK:", "PASS" if ok else "FAIL", [(o.order_id, o.status, o.track_state, o.detail) for o in os_])
        await W.hub.close(); return
    if os.environ.get("ONLY_R3"):
        await round3(); await W.hub.close(); return
    # RISE-1 default config is live in stock right now (GRA/RBX 1H-low) per the dry-run test
    if os.environ.get("ONLY_F"):
        cfg = {"24adv01-v3": {"type": "baremetalServers", "label": "ADVANCE-1"}, "24rise01-v2": {"type": "eco", "label": "RISE-1"}}
        await run_case("F 多型号 max_orders=2（跨型号计数）", "ok", price_cap=200.0, max_orders=2, any_config=False,
                       plan_code="24adv01-v3", plan_codes="24adv01-v3,24rise01-v2", plan_configs=json.dumps(cfg),
                       catalog_type="baremetalServers", wait=60)
        await W.hub.close(); return
    if os.environ.get("ONLY_E"):
        await run_case("E2 快速通道(上限200)", "ok", price_cap=200.0, prebuild=True, any_config=False,
                       memory="ram-32g-ecc-3200", storage="softraid-2x512nvme", datacenters="gra,rbx", wait=60, poll_interval=8)
        await run_case("A2 any_config 优先基础配置", "ok", price_cap=80.0, wait=20)
        await W.hub.close(); return
    await run_case("A 慢路径下单成功+自动扣款", "ok", price_cap=80.0)
    await run_case("B 首期超上限→拒绝下单", "ok", price_cap=50.0, wait=12)
    await run_case("C 结账被明确拒绝→ORDER_FAIL→BACKOFF", "reject", price_cap=80.0, wait=25)
    await run_case("D 结账超时→PENDING_CONFIRM 永久停止", "timeout", price_cap=80.0)
    await run_case("E 快速通道(预建购物车)", "ok", price_cap=80.0, prebuild=True, any_config=False,
                   memory="ram-32g-ecc-3200", storage="softraid-2x512nvme", datacenters="gra,rbx", wait=60)
    await W.hub.close()
if __name__ == "__main__":
    asyncio.run(main())
