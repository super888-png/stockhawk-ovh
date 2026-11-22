"""README screenshots from the demo server (tests/run_mock_server.py 8810 ok). Writes docs/screenshots/*.png."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mockcred
from playwright.sync_api import sync_playwright
B = "http://127.0.0.1:8810"; MU, MP = mockcred.get()
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "screenshots")
os.makedirs(OUT, exist_ok=True)


def login(pg):
    pg.goto(B + "/login")
    if pg.locator("input[name=username]").count():
        pg.fill("input[name=username]", MU); pg.fill("input[name=password]", MP); pg.click("form button.primary"); pg.wait_for_load_state()


with sync_playwright() as p:
    b = p.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"))
    c = b.new_context(viewport={"width": 1360, "height": 900}, device_scale_factor=1, color_scheme="light")
    pg = c.new_page(); login(pg)
    pg.goto(B + "/login"); pg.wait_for_load_state()
    # start the dry-run multi-plan task so the strip and the live log have content
    pg.goto(B + "/tasks/4")
    if pg.locator("form[action='/tasks/4/start'] button").count():
        pg.click("form[action='/tasks/4/start'] button"); pg.wait_for_load_state()
    pg.wait_for_function("() => (document.querySelector('#log')?.textContent || '').includes('PREVIEW_OK')", timeout=90000)
    time.sleep(3)
    pg.locator("#log").evaluate("e => e.scrollTop = e.scrollHeight")
    pg.locator(".card", has_text="实时日志").first.screenshot(path=f"{OUT}/task_live.png")
    pg.goto(B + "/"); pg.wait_for_selector("#myservers .srv:not(.sk-card)", timeout=30000); time.sleep(1.5)
    pg.screenshot(path=f"{OUT}/dashboard.png")
    pg.locator("#myservers").screenshot(path=f"{OUT}/my_servers.png")
    pg.goto(B + "/tasks/5/edit"); pg.wait_for_selector("#pricebox table tbody tr td.total", timeout=60000); time.sleep(1)
    pg.locator("#pricebox").screenshot(path=f"{OUT}/task_form_price.png")
    pg.goto(B + "/orders"); pg.wait_for_selector("tr#ovh-order-231960777", timeout=30000); time.sleep(0.5)
    pg.locator(".acct .card").first.screenshot(path=f"{OUT}/account_orders.png")
    pg.goto(B + "/orders/1"); time.sleep(0.5)
    pg.screenshot(path=f"{OUT}/order_detail.png")
    pg.goto(B + "/logout") if False else None
    d = b.new_context(viewport={"width": 1360, "height": 900}, color_scheme="dark")
    dp = d.new_page(); login(dp)
    dp.goto(B + "/"); dp.wait_for_selector("#myservers .srv:not(.sk-card)", timeout=30000); time.sleep(1.5)
    dp.screenshot(path=f"{OUT}/dark_dashboard.png")
    lp = b.new_context(viewport={"width": 1360, "height": 700}, color_scheme="light").new_page()
    lp.goto(B + "/login"); lp.screenshot(path=f"{OUT}/login.png")
    # stop the demo task again
    pg.goto(B + "/tasks/4")
    if pg.locator("form[action='/tasks/4/stop'] button").count():
        pg.click("form[action='/tasks/4/stop'] button")
    b.close()
print("screenshots written to", os.path.abspath(OUT))
