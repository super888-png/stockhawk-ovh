"""English panel screenshots for the project website (demo data, never real accounts).
Against an English mock server:  DEFAULT_LANG=en python tests/run_mock_server.py 8830 ok
                                 python tests/site_shots.py [base] [outdir]
Every viewport shot is exactly 1440×900 CSS px at 2× DPR (2880×1800, 16:10) so the site can show it without
stretching; card shots are element captures (natural aspect ratio kept)."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mockcred
from playwright.sync_api import sync_playwright
B = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8830"
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "screenshots", "en")
MU, MP = mockcred.get()
VW, VH = 1440, 900


def login(pg):
    pg.goto(B + "/login")
    if pg.locator("input[name=username]").count():
        pg.fill("input[name=username]", MU); pg.fill("input[name=password]", MP); pg.click("form button.primary"); pg.wait_for_load_state()


def to_top(pg, sel, pad=16):
    pg.evaluate("([s, p]) => { const e = document.querySelector(s); window.scrollTo(0, e.getBoundingClientRect().top + scrollY - p); }", [sel, pad])
    time.sleep(0.6)


def shoot(b, theme):
    d = os.path.join(OUT, theme); os.makedirs(d, exist_ok=True)
    c = b.new_context(viewport={"width": VW, "height": VH}, device_scale_factor=2, color_scheme=theme, locale="en-US")
    c.add_init_script(f"try{{localStorage.setItem('ovhsn.theme','{theme}')}}catch(e){{}}")
    pg = c.new_page(); login(pg)
    # live task (dry run, preview only) so the log + strip have content
    pg.goto(B + "/tasks/4")
    if pg.locator("form[action='/tasks/4/start'] button").count():
        pg.click("form[action='/tasks/4/start'] button"); pg.wait_for_load_state()
    pg.wait_for_function("() => (document.querySelector('#log')?.textContent || '').includes('PREVIEW_OK')", timeout=120000)
    time.sleep(3)
    pg.locator("#log").evaluate("e => e.scrollTop = e.scrollHeight")
    to_top(pg, "#log", 260)
    pg.screenshot(path=f"{d}/live.png")
    pg.goto(B + "/")
    pg.wait_for_selector("#myservers .srv:not(.sk-card)", timeout=30000)
    pg.wait_for_function("() => /Updated/.test((document.getElementById('mtime')||{}).textContent||'')", timeout=90000)
    time.sleep(1.5)
    pg.screenshot(path=f"{d}/dashboard.png")
    to_top(pg, "#dcm-world", 90)
    pg.screenshot(path=f"{d}/dcmap.png")
    card = pg.locator(".card", has=pg.locator("#dcm-world")).first
    card.screenshot(path=f"{d}/dcmap-card.png")
    to_top(pg, "#myservers", 70)
    pg.screenshot(path=f"{d}/servers.png")
    pg.goto(B + "/orders/1"); time.sleep(0.8)
    pg.screenshot(path=f"{d}/orders.png")
    pg.goto(B + "/tasks/5/edit"); pg.wait_for_selector("#pricebox table tbody tr td.total", timeout=60000); time.sleep(1)
    pg.locator("#pricebox").screenshot(path=f"{d}/price-card.png")
    pg.goto(B + "/tasks/4")
    if pg.locator("form[action='/tasks/4/stop'] button").count():
        pg.click("form[action='/tasks/4/stop'] button"); pg.wait_for_load_state()
    c.close()


with sync_playwright() as p:
    b = p.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"))
    for theme in ("light", "dark"):
        shoot(b, theme)
    b.close()
print("screenshots written to", os.path.abspath(OUT))
