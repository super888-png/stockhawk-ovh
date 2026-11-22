"""Dashboard 我的服务器 + orders page 账号订单 UI check and screenshots against tests/run_mock_server.py.
# bypass_csp: these layout checks use string wait_for_function predicates (eval); CSP itself is covered by ui_v14.py
Start mock servers first:  run_mock_server.py 8810 ok ; 8811 denied ; 8812 invalid
python tests/ui_orders.py"""
import os
import sys

from playwright.sync_api import sync_playwright
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mockcred
MU, MP = mockcred.get()

OUT = os.path.join(os.path.dirname(__file__), "..", "docs", "screenshots")
PORTS = {"ok": 8810, "denied": 8811, "invalid": 8812}
fails = []


def ok(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    cond or fails.append(name)


def ctx_for(b, theme, mobile=False):
    kw = dict(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True) if mobile \
        else dict(viewport={"width": 1360, "height": 900}, device_scale_factor=1)
    c = b.new_context(bypass_csp=True, color_scheme="light", **kw)
    c.add_init_script(f"try{{localStorage.setItem('ovhsn.theme','{theme}')}}catch(e){{}}")
    return c


def login(pg, base):
    pg.goto(base + "/login")
    if pg.locator("input[name=username]").count():   # cookies are per host (not port): may already be logged in
        pg.fill("input[name=username]", MU); pg.fill("input[name=password]", MP)
        pg.click("button[type=submit], form button.primary"); pg.wait_for_load_state()


def loaded(pg):
    pg.wait_for_function("!document.querySelector('.sk') && !document.querySelector('#acctRefresh').disabled", timeout=30000)


with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    for theme in ("light", "dark"):
        d = os.path.join(OUT, theme); os.makedirs(d, exist_ok=True)
        c = ctx_for(b, theme); pg = c.new_page(); errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        base = f"http://127.0.0.1:{PORTS['ok']}"
        login(pg, base)
        for port in PORTS.values():
            pg.goto(f"http://127.0.0.1:{port}/__mock/reset")
        # v1.5: account servers live on the dashboard (我的服务器), account orders on /orders
        pg.goto(base + "/")
        ok(f"[{theme}] dashboard: skeleton shown while loading", pg.locator("#myservers .sk").count() > 0)
        loaded(pg)
        ok(f"[{theme}] dashboard: 2 server cards under 我的服务器", pg.locator("#myservers .srv").count() == 2, str(pg.locator(".srv").count()))
        txt = pg.inner_text("#myservers")
        for s_ in ("我的服务器", "法国 GRA", "Intel Xeon-E 2386G", "32 GB", "2×512 GB NVME", "1 Gbps", "自动续费", "故障", "手动续费",
                   "4×2 TB SATA (raid5)", "203.0.113.18", "9 天后"):
            ok(f"[{theme}] dashboard shows {s_}", s_ in txt)
        pg.locator("#myservers").screenshot(path=os.path.join(d, "dashboard_servers.png"))
        pg.click("#acctRefresh"); pg.wait_for_timeout(300)
        ok(f"[{theme}] dashboard refresh button usable", "更新" in pg.inner_text("#acctAge") or "刷新" in pg.inner_text("#acctAge"))
        pg.goto(base + "/orders"); loaded(pg)
        ok(f"[{theme}] orders page: no server cards", pg.locator(".srv").count() == 0 and "账号服务器" not in pg.content())
        ok(f"[{theme}] 8 account orders", pg.locator('[data-slot="orders"] tbody tr').count() == 8)
        txt = pg.inner_text("#acct")
        for s_ in ("已交付", "未付款", "交付中", "已取消", "140.20 €", "已付款", "KS-LE-3 · BHS", "账单", "欠款支付"):
            ok(f"[{theme}] orders show {s_}", s_ in txt)
        ok(f"[{theme}] OVH order linked to 本站 task", pg.locator('#ovh-order-231890455 a.tag.accent').count() == 1)
        ok(f"[{theme}] 本站记录 row shows live OVH status", "已交付" in pg.inner_text('[data-oid="231890455"]'))
        ok(f"[{theme}] no JS errors", not errs, "; ".join(errs))
        pg.screenshot(path=os.path.join(d, "orders.png"), full_page=True)
        pg.click("#acctRefresh"); pg.wait_for_timeout(300)
        ok(f"[{theme}] refresh button usable (gap-limited)", "更新" in pg.inner_text("#acctAge") or "刷新" in pg.inner_text("#acctAge"))
        # 403 on servers: notice + re-authorize on the dashboard, orders page unaffected
        base2 = f"http://127.0.0.1:{PORTS['denied']}"
        login(pg, base2); pg.goto(base2 + "/"); loaded(pg)
        n = pg.locator("#myservers .acct-denied")
        ok(f"[{theme}] 403 notice shown on dashboard", n.count() == 1)
        ok(f"[{theme}] 403 notice: re-authorize form posts to /keys/ck-request", n.locator('form[action="/keys/ck-request"] input[name=endpoint][value=ovh-eu]').count() == 1)
        href = n.locator("a.btn").first.get_attribute("href") or ""
        ok(f"[{theme}] 403 notice: createToken link has new rules", "GET=/dedicated/server/*" in href, href)
        pg.locator("#myservers").screenshot(path=os.path.join(d, "dashboard_servers_403.png"))
        pg.goto(base2 + "/orders"); loaded(pg)
        ok(f"[{theme}] 403 (servers only): orders still listed, no notice on orders page",
           pg.locator('[data-slot="orders"] tbody tr').count() == 8 and pg.locator(".acct-denied").count() == 0)
        base3 = f"http://127.0.0.1:{PORTS['invalid']}"
        login(pg, base3); pg.goto(base3 + "/orders"); loaded(pg)
        ok(f"[{theme}] invalid key notice", pg.locator(".acct-notice .flash.err").count() == 1)
        c.close()
        # mobile
        m = ctx_for(b, theme, mobile=True); mp = m.new_page()
        login(mp, base); mp.goto(base + "/orders"); loaded(mp)
        over = mp.evaluate("document.documentElement.scrollWidth - innerWidth")
        ok(f"[{theme}] mobile: no page-level horizontal overflow", over <= 1, str(over))
        mp.screenshot(path=os.path.join(d, "mobile_orders.png"), full_page=True)
        mp.goto(base + "/"); loaded(mp)
        w = mp.evaluate("Math.max(...[...document.querySelectorAll('.srv')].map(e=>e.getBoundingClientRect().right))")
        ok(f"[{theme}] mobile: server cards inside viewport", w <= 390, str(w))
        mp.screenshot(path=os.path.join(d, "mobile_dashboard.png"), full_page=True)
        m.close()
    b.close()
print("ALL PASS" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
