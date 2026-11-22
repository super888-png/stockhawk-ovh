"""python tests/mock_account_test.py  - account view (orders + servers) against the fake OVH API, incl. 403."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SECRET_KEY", "x" * 40)
os.environ.setdefault("DATA_DIR", "/tmp/ovh-acct-test")
os.makedirs(os.environ["DATA_DIR"], exist_ok=True)

from app import account as A  # noqa: E402
from app import ovh_engine as E  # noqa: E402
from tests.fake_account import FakeAccountOVH as F  # noqa: E402

E.make_client = lambda *a, **k: F()
CREDS = ("ak", "as", "ck")
ok = True


def check(cond, msg):
    global ok
    print(("PASS " if cond else "FAIL ") + msg)
    ok &= bool(cond)


# rules
paths = {(r["method"], r["path"]) for r in E.ACCESS_RULES}
check(("GET", "/dedicated/server") in paths and ("GET", "/dedicated/server/*") in paths, "access rules include dedicated server GETs")
check("GET=/dedicated/server/*" in E.create_token_url("ovh-eu"), "createToken URL carries the new rules")
check(all(r["method"] == "GET" for r in E.ACCESS_RULES if r["path"].startswith("/dedicated")), "server rules are read-only")

# ok
F.mode, F.calls, F.max_inflight, F.delay = "ok", [], 0, 0.02
t0 = time.time()
d = A.fetch_account("ovh-eu", CREDS)
check(d["nichandle"] == "ab12345-ovh" and not d["denied"] and not d["errors"], f"ok: no errors ({d['ms']} ms, {len(F.calls)} GET calls)")
check(d["orders"]["total"] == 8 and d["orders"]["shown"] == 8, "ok: 8 orders")
byid = {r["orderId"]: r for r in d["orders"]["rows"]}
ks = byid[231960777]["summary"]
check(ks["model"] == "KS-LE-3" and ks["dc"] == "bhs" and ks["cpu"] == "Intel Xeon E-2274G" and ks["period"] == "1 个月"
      and ks["ram"].startswith("32GB") and ks["disks"] == ["2×SSD SATA 480GB Soft RAID"] and ks["bandwidth"] == "500 Mbps"
      and ks["domain"] == "ns1234567.ip-203-0-113.net" and len(byid[231960777]["items"]) == 8, f"summary: KS-LE-3 order de-duplicated {ks}")
check(byid[231960100]["summary"]["kindCn"] == "账单" and byid[231950042]["summary"]["kindCn"] == "欠款支付", "summary: invoice / debt labels")
check(byid[231890455]["summary"]["model"] == "Rise-1" and byid[231355790]["summary"]["kind"] == "renew", "summary: rise + renew")
o = next(r for r in d["orders"]["rows"] if r["orderId"] == 231890455)
check(o["orderId"] == 231890455 and o["status"] == "delivered" and o["statusCn"] == "已交付" and o["paid"] and o["amountText"] == "140.20 €", "ok: newest order fields")
check(o["items"][0]["description"].startswith("Rise-1") and o["url"].startswith("https://"), "ok: order items and url")
np_ = next(r for r in d["orders"]["rows"] if r["status"] == "notPaid")
check(not np_["paid"] and np_["payment"] is None and np_["statusCn"] == "未付款", "ok: notPaid order has no payment")
check(not any(c.endswith(f"/{231874012}/payment") for c in F.calls), "ok: payment not requested for notPaid")
s = {x["name"]: x for x in d["servers"]["rows"]}
r1 = s["ns3141592.ip-203-0-113.eu"]
check(r1["state"] == "ok" and r1["stateCn"] == "正常" and r1["dcLabel"] == "法国 GRA" and r1["displayName"] == "rise-paris-01", "ok: server state/dc/name")
check(r1["cpu"] == "Intel Xeon-E 2386G" and r1["cores"] == 6 and r1["threads"] == 12 and r1["ram"] == "32 GB", "ok: cpu/ram")
check(r1["disks"] == ["2×512 GB NVME"] and r1["bandwidthOut"] == "1 Gbps" and r1["renew"] == "自动续费 · 每 1 个月", f"ok: disks/bw/renew {r1['disks']} {r1['bandwidthOut']}")
check(r1["ip"] == "203.0.113.18" and r1["ips"] == ["2001:db8:403:3c76::/64"] and r1["ipCount"] == 2, f"ok: ips (main not repeated as /32) {r1['ips']}")
r2 = s["ns5012345.ip-198-51-100.net"]
check(r2["state"] == "error" and r2["stateCn"] == "故障" and r2["disks"] == ["4×2 TB SATA (raid5)"] and r2["renew"] == "手动续费" and r2["bandwidthOut"] == "250 Mbps", f"ok: second server {r2['disks']}")
check(all(not c.startswith(("/order/cart", "/me/order/") ) or c.startswith("/me/order/") for c in F.calls), "ok: GET only (fake has no POST)")
check(F.max_inflight <= A.WORKERS, f"ok: polite concurrency (max {F.max_inflight} in flight <= {A.WORKERS})")

# denied: servers 403, orders still shown
F.mode, F.calls = "denied", []
d = A.fetch_account("ovh-eu", CREDS)
check(d["denied"] == ["servers"] and d["servers"] is None and d["orders"]["total"] == 8, "denied: servers flagged, orders still loaded")
check("granted" in d["errors"]["servers"], "denied: message kept")

# invalid key
F.mode = "invalid"
d = A.fetch_account("ovh-eu", CREDS)
check(d.get("authInvalid") and d["orders"] is None and d["servers"] is None and not d["denied"], "invalid: authInvalid, not reported as missing rule")

# empty account
F.mode = "empty"
d = A.fetch_account("ovh-eu", CREDS)
check(d["orders"]["total"] == 0 and d["servers"]["total"] == 0 and not d["errors"], "empty: zero orders/servers")


# cache: immediate return, background fetch, TTL, refresh gap
async def cache_test():
    F.mode, F.delay = "ok", 0.05
    c = A.AccountCache()
    first = c.get(1, "ovh-eu", CREDS)
    check(first["loading"] and first["data"] is None, "cache: first call returns at once with loading")
    t0 = time.time()
    while c.get(1, "ovh-eu", CREDS)["loading"]:
        await asyncio.sleep(0.05)
    e = c.get(1, "ovh-eu", CREDS)
    check(e["data"]["orders"]["total"] == 8 and not e["loading"], f"cache: data after {time.time() - t0:.2f}s")
    n = len(F.calls)
    check(not c.get(1, "ovh-eu", CREDS, refresh=True)["loading"], "cache: refresh within gap is ignored")
    c.s[next(k for k in c.s if k[:2] == (1, "ovh-eu"))]["started"] -= A.MIN_REFRESH_GAP + 1
    check(c.get(1, "ovh-eu", CREDS, refresh=True)["loading"], "cache: manual refresh starts a fetch")
    while c.get(1, "ovh-eu", CREDS)["loading"]:
        await asyncio.sleep(0.05)
    check(len(F.calls) > n, "cache: refresh re-fetched")
    c.s[next(k for k in c.s if k[:2] == (1, "ovh-eu"))]["ts"] -= A.CACHE_TTL + 1
    check(c.get(1, "ovh-eu", CREDS)["loading"], "cache: stale after TTL")
    c.drop_user(1)
    check(not c.s, "cache: drop_user clears")
    F.delay = 0

asyncio.run(cache_test())
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
