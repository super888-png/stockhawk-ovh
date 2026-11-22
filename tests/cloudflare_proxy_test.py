"""Behind Cloudflare, offline: CF headers trusted only from Cloudflare ranges, https scheme -> Secure cookies,
real client IP for login throttling/audit, same-origin POST check with https Origin.   python tests/cloudflare_proxy_test.py"""
import asyncio, os, re, sys, tempfile
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("SECRET_KEY", "unit-only-" + "k" * 40)
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="ovhsn-cf-")
os.environ["TRUST_CLOUDFLARE"] = "1"
os.environ["COOKIE_SECURE"] = "0"
import httpx  # noqa: E402
fails = []


def ok(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    cond or fails.append(name)


from app.db import SessionLocal, auto_migrate  # noqa: E402
from app.models import AuditLog, User  # noqa: E402
from app.security import hash_password  # noqa: E402
from app import main  # noqa: E402
from app.proxy import CloudflareProxy  # noqa: E402
auto_migrate()
with SessionLocal() as db:
    db.add(User(id=1, username="owner", password_hash=hash_password("Correct-Horse-9x!"), is_admin=True, lang=""))
    db.commit()

CF, DIRECT = ("162.158.10.20", 4321), ("203.0.113.9", 5555)
HOST = "panel.example.com"
CFH = {"CF-Connecting-IP": "198.51.100.77", "X-Forwarded-Proto": "http", "CF-Visitor": '{"scheme":"https"}', "Host": HOST}

p = CloudflareProxy(lambda *a: None, enabled=True)
ok("CF range match v4/v6/mapped", p.from_cloudflare("172.70.1.1") and p.from_cloudflare("2606:4700::1")
   and p.from_cloudflare("::ffff:104.16.1.1") and not p.from_cloudflare("8.8.8.8") and not p.from_cloudflare("x"))


async def run():
    async def client(peer, base):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app, client=peer), base_url=base)

    async def login_attempt(c, headers, origin, user, pw):
        r = await c.get("/login", headers=headers)
        tok = re.search(r'name="csrf" value="([^"]+)"', r.text).group(1)
        h = dict(headers, Origin=origin, **{"Content-Type": "application/x-www-form-urlencoded"})
        # the test client talks http to the origin and so withholds Secure cookies; a browser on https:// sends them
        jar = "; ".join(f"{k}={v}" for k, v in c.cookies.items())
        if jar:
            h["Cookie"] = jar
        return r, await c.post("/login", headers=h, data={"csrf": tok, "username": user, "password": pw})

    # 1) via Cloudflare (Flexible: origin hop is http, visitor was https)
    c = await client(CF, f"http://{HOST}")
    r, r2 = await login_attempt(c, CFH, f"https://{HOST}", "nobody-here", "wrong")
    sc = r.headers.get_list("set-cookie")
    ok("CF: login page 200", r.status_code == 200)
    ok("CF: session cookie gets Secure", sc and all("secure" in s.lower() for s in sc), str(sc))
    ok("CF: https Origin passes same-origin check (no 403)", r2.status_code == 303 or r2.status_code == 302, str(r2.status_code))
    ok("CF: redirect stays relative/https", not r2.headers.get("location", "").startswith("http://"), r2.headers.get("location"))
    jar = "; ".join(f"{k}={v}" for k, v in c.cookies.items())
    r3 = await c.get("/login", headers=dict(CFH, Cookie=jar))
    ok("CF: normal invalid-credentials flash", "invalid" in r3.text.lower() or "错误" in r3.text, r3.text[:200])
    with SessionLocal() as db:
        last = db.query(AuditLog).order_by(AuditLog.id.desc()).first()
    ok("CF: audit/throttle uses CF-Connecting-IP", last and last.ip == "198.51.100.77", last and last.ip)
    # 2) foreign Origin still refused
    r = await c.post("/login", headers=dict(CFH, Origin="https://evil.example"), data={"csrf": "x"})
    ok("CF: cross-site Origin -> 403", r.status_code == 403)
    await c.aclose()

    # 3) direct (not a CF edge) with spoofed CF headers: ignored, no Secure flag, plain http login keeps working
    c = await client(DIRECT, f"http://{HOST}")
    r, r2 = await login_attempt(c, CFH, f"http://{HOST}", "owner", "Correct-Horse-9x!")
    sc = r2.headers.get_list("set-cookie")
    ok("direct: spoofed CF-Visitor ignored -> cookie not Secure", sc and not any("secure" in s.lower() for s in sc), str(sc))
    ok("direct: http login works", r2.status_code in (302, 303) and r2.headers.get("location") == "/", str(r2.status_code))
    with SessionLocal() as db:
        last = db.query(AuditLog).order_by(AuditLog.id.desc()).first()
    ok("direct: spoofed CF-Connecting-IP ignored", last and last.ip == DIRECT[0], last and last.ip)
    await c.aclose()

    # 4) TRUST_CLOUDFLARE off: CF peer treated like anyone else
    q = CloudflareProxy(main.app.router, enabled=False)
    seen = {}

    async def app(scope, receive, send):
        seen.update(client=scope["client"], scheme=scope["scheme"])
    q.app = app
    await q({"type": "http", "client": CF, "scheme": "http", "headers": [(b"cf-connecting-ip", b"1.1.1.1"), (b"x-forwarded-proto", b"https")]}, None, None)
    ok("disabled: nothing rewritten", seen == {"client": CF, "scheme": "http"}, str(seen))

asyncio.run(run())
print("ALL PASS" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
