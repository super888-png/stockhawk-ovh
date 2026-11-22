"""Browser check + screenshots for both themes (Playwright + Chrome).
# bypass_csp: these layout checks use string wait_for_function predicates (eval); CSP itself is covered by ui_v14.py
Asserts: the plan dropdown is the topmost element where it overlaps the next card (task form and stock page),
the theme is applied before DOMContentLoaded (no flash), system theme follows prefers-color-scheme,
and there are no console errors. Usage: python tests/ui_check.py [base_url] [user] [password]"""
import os, sys
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8810"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mockcred  # noqa: E402
USER, PW = (sys.argv[2], sys.argv[3]) if len(sys.argv) > 3 else mockcred.get()
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "screenshots")
TASK = os.environ.get("UI_TASK", "4")
errors, results = [], []

def ok(name, cond, extra=""):
    results.append((name, bool(cond), extra)); print(("PASS " if cond else "FAIL ") + name, extra)

TOPMOST = """(sel) => { const dd = document.querySelector(sel + ' .dd'); const r = dd.getBoundingClientRect();
  const card = dd.closest('.card'); const cr = card.getBoundingClientRect();
  const y = Math.min(r.bottom, innerHeight) - 12;
  const pts = [[r.left + 40, y], [r.left + r.width / 2, y], [r.right - 30, y]];
  const hits = pts.map(([x, y]) => { const el = document.elementFromPoint(x, y); return !!(el && el.closest(sel + ' .dd')); });
  return {visible: getComputedStyle(dd).display !== 'none' && r.height > 40, extendsPastCard: r.bottom > cr.bottom + 20,
          hits, inViewport: r.bottom <= innerHeight && r.top >= 0, ddBottom: Math.round(r.bottom), cardBottom: Math.round(cr.bottom)}; }"""

def login(pg):
    pg.goto(BASE + "/login"); pg.fill("input[name=username]", USER); pg.fill("input[name=password]", PW)
    pg.click("button.primary"); pg.wait_for_load_state("networkidle")

def shoot(pg, d, name, full=True):
    pg.screenshot(path=os.path.join(d, name + ".png"), full_page=full)

def theme_run(b, theme):
    d = os.path.join(OUT, theme); os.makedirs(d, exist_ok=True)
    ctx = b.new_context(bypass_csp=True, viewport={"width": 1360, "height": 900}, device_scale_factor=1, color_scheme="light")
    ctx.add_init_script(f"""try{{localStorage.setItem('ovhsn.theme','{theme}')}}catch(e){{}}
      document.addEventListener('DOMContentLoaded',()=>{{window.__dcl=document.documentElement.dataset.theme}});""")
    pg = ctx.new_page(); pg.on("console", lambda m: m.type == "error" and errors.append(f"{theme} {pg.url}: {m.text}"))
    pg.on("pageerror", lambda e: errors.append(f"{theme} {pg.url}: {e}"))
    pg.goto(BASE + "/login"); ok(f"[{theme}] theme set before DOMContentLoaded", pg.evaluate("window.__dcl") == theme, pg.evaluate("window.__dcl"))
    shoot(pg, d, "login", False)
    login(pg)
    # task form: open the plan dropdown and make sure it covers card 2
    pg.goto(BASE + "/tasks/new"); pg.wait_for_function("document.querySelector('#catinfo').textContent.includes('共')", timeout=90000)
    pg.click("#picker input"); pg.keyboard.type("RISE"); pg.wait_for_timeout(300)
    r = pg.evaluate(TOPMOST, "#picker")
    ok(f"[{theme}] task form: dropdown above card 2", r["visible"] and r["extendsPastCard"] and all(r["hits"]), str(r))
    shoot(pg, d, "task_new_dropdown", False)
    for _ in range(2): pg.keyboard.press("Enter")          # RISE-1, then next match
    pg.keyboard.press("Escape"); pg.fill("#picker input", ""); pg.click("#picker input"); pg.keyboard.type("ADVANCE-1"); pg.keyboard.press("Enter"); pg.keyboard.press("Escape")
    pg.click("h1"); pg.wait_for_timeout(1500)
    ok(f"[{theme}] dropdown closes on outside click", not pg.evaluate("document.querySelector('#picker').classList.contains('open')"))
    for dc in ("gra", "rbx", "fra"):
        el = pg.query_selector(f'#dcs .dc[data-c="{dc}"]')
        if el: el.click()
    pg.wait_for_timeout(300); shoot(pg, d, "task_new")
    # stock page dropdown above the table
    pg.goto(BASE + "/stock"); pg.wait_for_timeout(500)
    pg.evaluate("localStorage.setItem('ovhsn.stock.v1', JSON.stringify({endpoint:'ovh-eu',subsidiary:'IE',ctype:'all',onlyStock:true,onlyDefault:false,plans:[{planCode:'24rise01-v2',type:'eco',label:'RISE-1'},{planCode:'24rise02-v2',type:'eco',label:'RISE-2'},{planCode:'24adv01-v3',type:'baremetalServers',label:'ADVANCE-1'}]}))")
    pg.reload(); pg.wait_for_function("document.querySelectorAll('#rows tr').length > 3", timeout=90000); pg.wait_for_timeout(800)
    shoot(pg, d, "stock", False)
    pg.click("#picker input"); pg.keyboard.type("ADV"); pg.wait_for_timeout(300)
    r = pg.evaluate(TOPMOST, "#picker")
    ok(f"[{theme}] stock page: dropdown above the table", r["visible"] and r["extendsPastCard"] and all(r["hits"]), str(r))
    shoot(pg, d, "stock_dropdown", False)
    pg.keyboard.press("Escape")
    for name, path, wait in [("dashboard", "/", 2500), ("task_detail", f"/tasks/{TASK}", 4000), ("keys", "/keys", 300),
                             ("orders", "/orders", 300), ("notify", "/notify", 300), ("admin", "/admin", 300)]:
        pg.goto(BASE + path); pg.wait_for_timeout(wait); shoot(pg, d, name)
    # theme switch buttons
    pg.click('[data-theme-set="system"]'); pg.wait_for_timeout(100)
    ok(f"[{theme}] switch to system => follows light OS", pg.evaluate("document.documentElement.dataset.theme") == "light" and pg.evaluate("localStorage.getItem('ovhsn.theme')") is None)
    pg.click(f'[data-theme-set="{theme}"]'); pg.wait_for_timeout(100)
    ok(f"[{theme}] switch back", pg.evaluate("document.documentElement.dataset.theme") == theme)
    # mobile
    m = b.new_context(bypass_csp=True, viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True, color_scheme="light")
    m.add_init_script(f"try{{localStorage.setItem('ovhsn.theme','{theme}')}}catch(e){{}}")
    mp = m.new_page(); mp.on("pageerror", lambda e: errors.append(f"mobile {theme}: {e}")); login(mp)
    mp.goto(BASE + "/"); mp.wait_for_timeout(2500); shoot(mp, d, "mobile_dashboard")
    mp.goto(BASE + "/tasks/new"); mp.wait_for_function("document.querySelector('#catinfo').textContent.includes('共')", timeout=90000)
    mp.tap("#picker input"); mp.keyboard.type("RISE"); mp.wait_for_timeout(300)
    r = mp.evaluate(TOPMOST, "#picker")
    ok(f"[{theme}] mobile: dropdown above card 2 and inside the viewport", r["visible"] and all(r["hits"]) and r["inViewport"], str(r))
    shoot(mp, d, "mobile_task_new_dropdown", False)
    mp.goto(BASE + "/stock"); mp.wait_for_timeout(3000); shoot(mp, d, "mobile_stock")
    hscroll = mp.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
    ok(f"[{theme}] mobile: no page-level horizontal scroll", not hscroll)
    m.close(); ctx.close()

with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    for th in ("light", "dark"):
        theme_run(b, th)
    # system preference with nothing stored
    for scheme in ("dark", "light"):
        c = b.new_context(bypass_csp=True, color_scheme=scheme); pg = c.new_page(); pg.goto(BASE + "/login")
        ok(f"[system] prefers-color-scheme:{scheme} => {scheme}", pg.evaluate("document.documentElement.dataset.theme") == scheme)
        c.close()
    b.close()
print("console/page errors:", errors or "none")
print("SUMMARY", sum(1 for r in results if r[1]), "/", len(results), "passed")
sys.exit(0 if all(r[1] for r in results) and not errors else 1)
