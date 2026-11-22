"""SSE latency check: python sse_test.py BASE USER PASS TID  (password may also come from $PW)"""
import datetime as dt, json, os, re, sys, time, httpx
B, U, P, TID = sys.argv[1], sys.argv[2], (sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != "-" else os.environ["PW"]), int(sys.argv[4])
c = httpx.Client(base_url=B, timeout=30, follow_redirects=True)
tok = lambda path: re.search(r'name="csrf" value="([^"]+)"', c.get(path).text).group(1)
c.post("/login", data={"csrf": tok("/login"), "username": U, "password": P}, headers={"Origin": B})
d = c.get(f"/api/tasks/{TID}/logs").json(); after = d["logs"][-1]["id"] if d["logs"] else 0
if not d["running"]:
    c.post(f"/tasks/{TID}/start", data={"csrf": tok(f"/tasks/{TID}")}, headers={"Origin": B}); print("started task", TID)
lat, n_ev, t0, conns = [], 0, time.time(), 0
r0 = c.get(f"/api/tasks/{TID}/stream", params={"after": after}, headers={"Accept": "text/event-stream"}, timeout=5) if False else None
while time.time() - t0 < float(os.environ.get("DUR", 40)):
    conns += 1
    with c.stream("GET", f"/api/tasks/{TID}/stream", params={"after": after}, timeout=40) as r:
        if conns == 1:
            print("status", r.status_code, "content-type", r.headers.get("content-type"), "| x-accel-buffering", r.headers.get("x-accel-buffering"),
                  "| content-encoding", r.headers.get("content-encoding"), "| csp connect-src 'self'", "connect-src 'self'" in r.headers.get("content-security-policy", ""))
        for line in r.iter_lines():
            if line.startswith("data: "):
                ev = json.loads(line[6:]); n_ev += 1
                for l in ev["logs"]:
                    after = max(after, l["id"])
                    ts = dt.datetime.fromisoformat(l["iso"].replace("Z", "+00:00"))
                    lat.append((dt.datetime.now(dt.timezone.utc) - ts).total_seconds())
                    print(f"  +{time.time()-t0:5.1f}s log #{l['id']} {l['marker']:<12} latency {lat[-1]:.2f}s")
            if time.time() - t0 > float(os.environ.get("DUR", 40)):
                break
print(f"events {n_ev}, connections {conns}, log lines {len(lat)}, max latency {max(lat) if lat else '-'}s")
if os.environ.get("STOP", "1") == "1":
    c.post(f"/tasks/{TID}/stop", data={"csrf": tok(f"/tasks/{TID}")}, headers={"Origin": B, "X-Requested-With": "fetch"}); print("stopped")
