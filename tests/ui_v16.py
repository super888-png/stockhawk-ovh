"""v1.6 browser checks: datacenter stock map on the dashboard (light / dark, desktop / mobile, reduced motion).
Against tests/run_mock_server.py 8810 ok.   python tests/ui_v16.py [base]
Screenshots -> docs/screenshots/v16/{light,dark}/"""
import os, sys
from playwright.sync_api import sync_playwright
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mockcred
MU, MP = mockcred.get()

B = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8810"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "screenshots", "v16")
fails = []
def ok(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    cond or fails.append(name)

def login(pg):
    pg.goto(B + "/login")
    if pg.locator("input[name=username]").count():
        pg.fill("input[name=username]", MU); pg.fill("input[name=password]", MP); pg.click("form button.primary"); pg.wait_for_load_state()

def ready(pg):
    pg.goto(B + "/")
    pg.wait_for_selector("#dcm-world .dcm-m", timeout=30000)
    # wait until the shared poller has data (first OVH fetch) so markers carry real levels
    pg.wait_for_function("() => /更新于/.test(document.getElementById('mtime').textContent)", timeout=90000)
    pg.wait_for_timeout(600)

def run(b, theme, mobile=False):
    d = os.path.join(OUT, theme); os.makedirs(d, exist_ok=True)
    vp = {"width": 390, "height": 844} if mobile else {"width": 1360, "height": 900}
    c = b.new_context(viewport=vp, device_scale_factor=2 if mobile else 1, color_scheme="light", is_mobile=mobile, has_touch=mobile)
    c.add_init_script(f"try{{localStorage.setItem('ovhsn.theme','{theme}')}}catch(e){{}}")
    pg = c.new_page(); errs = []
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errs.append("JS: " + str(e)))
    ext = []
    pg.on("request", lambda r: ext.append(r.url) if not r.url.startswith(B) and not r.url.startswith("data:") else None)
    login(pg); ready(pg)
    tag = f"[{theme}{' mobile' if mobile else ''}]"
    n_world = pg.locator("#dcm-world .dcm-m:not(.mini)").count(); n_eu = pg.locator("#dcm-eu .dcm-m").count()
    ok(f"{tag} world + Europe markers drawn", n_world >= 7 and n_eu >= 7, (n_world, n_eu))
    codes = pg.evaluate("[...document.querySelectorAll('.dcm-m:not(.mini)')].map(g=>g.dataset.code)")
    ok(f"{tag} core datacenters present", all(c in codes for c in ("gra", "rbx", "sbg", "bhs", "sgp", "syd", "waw", "lon", "fra")), codes)
    lv = pg.evaluate("[...document.querySelectorAll('.dcm-m:not(.mini)')].map(g=>(g.getAttribute('class').match(/lv-(\\w+)/)||[])[1])")
    ok(f"{tag} markers carry a stock level", all(lv) and any(x != "unknown" for x in lv), lv)
    ok(f"{tag} legend has 5 levels", pg.locator("#dcm-legend span").count() == 5)
    ok(f"{tag} land drawn from self-hosted svg", pg.evaluate("document.querySelector('#dcm-world .dcm-land').getAttribute('d').length") > 5000)
    ok(f"{tag} datacenter list", pg.locator("#dcm-list .dcm-chip").count() == len(codes))
    # tooltip: tap/click a monitored in-Europe marker
    code = pg.evaluate("(document.querySelector('#dcm-eu .dcm-m:not(.unmon)')||document.querySelector('#dcm-eu .dcm-m')).dataset.code")
    if mobile:
        pg.locator(f"#dcm-list .dcm-chip[data-code='{code}']").click()
    else:
        pg.locator(f"#dcm-eu .dcm-m[data-code='{code}'] .hit").hover()
    pg.wait_for_selector("#dcm-tip:not([hidden])", timeout=5000)
    tip = pg.inner_text("#dcm-tip")
    ok(f"{tag} tooltip shows DC + plans", code.upper() in tip and ("型号" in tip or "没有" in tip), tip[:120])
    ok(f"{tag} no external requests (CSP/self-hosted)", not ext, ext[:3])
    if mobile:
        pg.add_style_tag(content=".side{position:static!important}")   # keep the sticky top bar out of the element shot
        pg.locator("#dcmap").screenshot(path=os.path.join(d, "dcmap_mobile.png"))
        pg.evaluate("window.scrollTo(0,0)"); pg.screenshot(path=os.path.join(d, "dashboard_mobile.png"), full_page=True)
    else:
        pg.locator("#dcmap").screenshot(path=os.path.join(d, "dcmap_tooltip.png"))
        pg.keyboard.press("Escape"); pg.mouse.move(5, 5); pg.wait_for_timeout(300)
        ok(f"{tag} Escape closes tooltip", pg.locator("#dcm-tip[hidden]").count() == 1)
        pg.locator("#dcmap").screenshot(path=os.path.join(d, "dcmap.png"))
        pg.screenshot(path=os.path.join(d, "dashboard.png"), full_page=True)
        ok(f"{tag} pulse animation on in-stock markers",
           pg.evaluate("(()=>{const r=document.querySelector('.dcm-m.lv-high .ring,.dcm-m.lv-low .ring');return !r||getComputedStyle(r).animationName==='dcm-pulse'})()"))
    ok(f"{tag} no console/CSP errors", not errs, errs[:5])
    c.close()

def reduced(b):
    c = b.new_context(viewport={"width": 1360, "height": 900}, reduced_motion="reduce")
    pg = c.new_page(); login(pg); ready(pg)
    anim = pg.evaluate("[...document.querySelectorAll('.dcm-m .ring,.dcm-m .halo')].map(n=>getComputedStyle(n).animationName).filter(a=>a!=='none')")
    ok("[reduced-motion] no map animations", not anim, anim[:3])
    c.close()

with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    for t in ("light", "dark"):
        run(b, t)
        run(b, t, mobile=True)
    reduced(b)
    b.close()
print("\nALL PASS" if not fails else f"\n{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
