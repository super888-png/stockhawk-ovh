"""Live dry run against the real public OVH API (no keys, no orders): CLI owner -> form task -> start -> SSE -> stop."""
import json, os, re, subprocess, sys, time, secrets, httpx
B = "http://127.0.0.1:8820"; ENV = {**os.environ, "DATA_DIR": os.environ.get("LIVE_DATA_DIR", "/tmp/ovh-live-dry"), "COOKIE_SECURE": "0"}
PY = sys.executable; ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
bad = 0
if "LIVE_DATA_DIR" not in os.environ:   # default scratch dir: start from an empty database every run
    import shutil; shutil.rmtree("/tmp/ovh-live-dry", ignore_errors=True); os.makedirs("/tmp/ovh-live-dry")
def ok(n, c, x=""):
    global bad; print(("PASS " if c else "FAIL ") + n + (f"  ({x})" if x and not c else "")); bad += (not c)
U, P = "owner", "Lv-" + secrets.token_urlsafe(12)
r = subprocess.run([PY, "-m", "app.cli", "has-owner"], cwd=ROOT, env=ENV); ok("cli has-owner = 1 on empty db", r.returncode == 1)
r = subprocess.run([PY, "-m", "app.cli", "create-admin", "-u", U, "--password-stdin"], input=P + "\n", text=True, cwd=ROOT, env=ENV, capture_output=True)
ok("cli create-admin", r.returncode == 0, r.stderr)
r = subprocess.run([PY, "-m", "app.cli", "create-admin", "-u", "second", "--password-stdin"], input=P + "\n", text=True, cwd=ROOT, env=ENV, capture_output=True)
ok("cli refuses a second account (exit 2)", r.returncode == 2)
srv = subprocess.Popen([PY, "-m", "uvicorn", "app.main:app", "--port", "8820", "--workers", "1", "--timeout-graceful-shutdown", "10"],
                       cwd=ROOT, env=ENV, stdout=open("/tmp/live_dry_srv.log", "w"), stderr=subprocess.STDOUT)
try:
    for _ in range(60):
        try:
            if httpx.get(B + "/healthz").status_code == 200: break
        except Exception: pass
        time.sleep(0.5)
    c = httpx.Client(base_url=B, timeout=60, follow_redirects=True)
    tok = lambda path: re.search(r'name="csrf" value="([^"]+)"', c.get(path).text).group(1)
    ok("/register is 404", c.get("/register").status_code == 404)
    c.post("/login", data={"csrf": tok("/login"), "username": U, "password": P}, headers={"Origin": B})
    ok("login", "控制台" in c.get("/").text)
    plans = [{"planCode": "25skb012", "type": "eco", "label": "", "memory": "ram-32g-ecc-1333-25skb01",
              "storage": "noraid-1x120ssd-25skb01", "bandwidth": "bandwidth-500-25sk"},
             {"planCode": "24sk202", "type": "eco", "label": ""}]
    # price preview the form uses (live catalog)
    pr = c.get("/api/price", params={"endpoint": "ovh-eu", "subsidiary": "IE", "plans": json.dumps(plans), "pricing_mode": "default", "quantity": 1})
    pj = pr.json() if pr.status_code == 200 else {}
    ok("live price preview with CNY", pr.status_code == 200 and pj.get("plans") and pj.get("fx", {}).get("rate"), pr.text[:200])
    r = c.post("/tasks/save", data={"csrf": tok("/tasks/new"), "endpoint": "ovh-eu", "subsidiary": "IE", "name": "",
               "plans_json": json.dumps(plans), "pay_mode": "dry", "any_config": "1", "poll_interval": "5", "max_orders": "1"},
               headers={"Origin": B})
    tid = int(re.search(r"/tasks/(\d+)", str(r.url)).group(1)); ok("task created via form", tid > 0)
    page = c.get(f"/tasks/{tid}").text
    ok("task page: KS-B / KS-2 + CPU + readable config", all(x in page for x in ("KS-B", "KS-2", "Intel Xeon E5-1620v2", "32 GB ECC 1333", "1×120 GB SSD（无 RAID）", "500 Mbps")))
    out = subprocess.run([PY, os.path.join(ROOT, "tests", "sse_check.py"), B, U, "-", str(tid)], env={**ENV, "PW": P, "DUR": "35", "STOP": "0"}, capture_output=True, text=True, timeout=120)
    print(out.stdout[-2500:])
    m = re.search(r"log lines (\d+), max latency ([\d.]+)", out.stdout)
    ok("SSE delivered log lines with latency < 2s", m and int(m.group(1)) >= 2 and float(m.group(2)) < 2.0, out.stdout[-300:])
    run = c.get("/api/tasks/running").json()["tasks"]
    ok("running strip lists the task with model names", run and run[0]["id"] == tid and [p["name"] for p in run[0]["plans"]] == ["KS-B", "KS-2"]
       and run[0]["polls"] >= 3, json.dumps(run)[:300])
    logs = c.get(f"/api/tasks/{tid}/logs").json()
    text = "\n".join(l["marker"] + " " + l["msg"] for l in logs["logs"])
    print("---- log ----\n" + text[-1800:])
    ok("log uses model names + readable config", "KS-B" in text and "32 GB ECC 1333" in text and "ram-32g-ecc-1333-25skb01" not in text)
    ok("no orders placed / no carts (no keys)", "ORDER_PLACED" not in text and "checkout" not in text.lower())
    r = c.post(f"/tasks/{tid}/stop", data={"csrf": tok(f"/tasks/{tid}")}, headers={"Origin": B, "X-Requested-With": "fetch"})
    ok("quick stop (fetch) -> json ok", r.status_code == 200 and r.json().get("ok"))
    time.sleep(2)
    ok("strip empty after stop", c.get("/api/tasks/running").json()["tasks"] == [])
finally:
    srv.terminate(); srv.wait(20)
print("ALL PASS" if not bad else f"BAD {bad}")
