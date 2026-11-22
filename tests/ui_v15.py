"""v1.5 browser checks: live log over SSE, 进行中的任务 strip, CNY in the price table, single-owner login.
Against tests/run_mock_server.py 8810 ok.   python tests/ui_v15.py [base]"""
import os, sys, time
from playwright.sync_api import sync_playwright
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mockcred
MU, MP = mockcred.get()

B = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8810"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "screenshots", "v15")
fails = []
def ok(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    cond or fails.append(name)

def run(b, theme):
    d = os.path.join(OUT, theme); os.makedirs(d, exist_ok=True)
    c = b.new_context(viewport={"width": 1360, "height": 900}, color_scheme="light")
    c.add_init_script(f"try{{localStorage.setItem('ovhsn.theme','{theme}')}}catch(e){{}}")
    pg = c.new_page(); errs = []
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errs.append("JS: " + str(e)))
    pg.goto(B + "/login")
    if pg.locator("input[name=username]").count():
        pg.fill("input[name=username]", MU); pg.fill("input[name=password]", MP); pg.click("form button.primary"); pg.wait_for_load_state()
    ok(f"[{theme}] login page has no register link", pg.goto(B + "/login") and "/register" not in pg.content())
    r = pg.request.get(B + "/register"); ok(f"[{theme}] /register is gone (404)", r.status == 404, r.status)

    # start task 4 from its page, then watch the live log
    pg.goto(B + "/tasks/4")
    if pg.locator("form[action='/tasks/4/start'] button").count():
        pg.once("dialog", lambda dlg: dlg.accept())
        pg.click("form[action='/tasks/4/start'] button"); pg.wait_for_load_state()
    pg.wait_for_function("() => !!document.querySelector('#livepoll') && document.querySelector('#livepoll').textContent.includes('第')", timeout=60000)
    ok(f"[{theme}] live log uses SSE push", "实时推送" in pg.inner_text("#livepoll"), pg.inner_text("#livepoll"))
    n0 = pg.evaluate("document.querySelectorAll('#log .m').length")
    t0 = time.time()
    pg.wait_for_function(f"() => document.querySelectorAll('#log .m').length > {n0}", timeout=90000)
    ok(f"[{theme}] new log lines appear live (no reload)", True, f"{time.time() - t0:.1f}s")
    ok(f"[{theme}] auto-scroll keeps the log at the bottom",
       pg.evaluate("(()=>{const l=document.getElementById('log');return l.scrollHeight-l.scrollTop-l.clientHeight<60})()"))
    pg.wait_for_selector("#rs-list .rs-item", timeout=15000)
    ok(f"[{theme}] running strip shows the task", pg.locator("#rs-list .rs-item").count() >= 1 and "#4" in pg.inner_text("#rs-list"))
    txt = pg.inner_text("#rs-list")
    ok(f"[{theme}] strip shows plans/mode/polls/heartbeat/stop", all(k in txt for k in ("轮询", "心跳", "停止")) and any(k in txt for k in ("只预览", "只下单", "下单并")), txt[:200])
    p1 = pg.evaluate("document.querySelector('#rs-list .rs-item .rs-k b').textContent")
    pg.wait_for_timeout(int(os.environ.get("POLLWAIT", "7000")))
    p2 = pg.evaluate("document.querySelector('#rs-list .rs-item .rs-k b').textContent")
    ok(f"[{theme}] strip poll counter updates live", int(p2) > int(p1), (p1, p2))
    pg.screenshot(path=os.path.join(d, "task_detail_live.png"), full_page=True)
    pg.goto(B + "/tasks/new"); pg.wait_for_selector("#rs-list .rs-item", timeout=15000)
    ok(f"[{theme}] strip on 抢购任务 (new task) page", pg.locator("#rs-list .rs-item").count() >= 1)
    # CNY on every price cell incl. 首月租金
    pg.click("#picker input"); pg.wait_for_selector("#picker .opt", timeout=90000)
    pg.locator("#picker .opt:not(.sel)").first.click(); pg.keyboard.press("Escape"); pg.mouse.click(1340, 880)
    pg.wait_for_selector('#pricebox table tbody tr td.total', timeout=60000)
    cells = pg.evaluate("[...document.querySelectorAll('#pricebox tbody tr:first-child td.num')].map(td => td.innerText)")
    print("   cells:", cells); ok(f"[{theme}] CNY line in every price cell (incl. 首月租金)", len(cells) == 5 and all("¥" in x for x in cells), cells)
    pg.fill("input[name=price_cap]", "200"); pg.wait_for_timeout(900)
    ok(f"[{theme}] CNY hint next to price cap", "¥" in pg.inner_text("#capcny"), pg.inner_text("#capcny"))
    pg.locator("#pricebox").scroll_into_view_if_needed(); pg.locator("#pricebox").locator("xpath=..").screenshot(path=os.path.join(d, "price_box.png"))
    pg.locator("#runstrip").screenshot(path=os.path.join(d, "running_strip.png"))
    # orders: CNY under amounts
    pg.goto(B + "/orders"); pg.wait_for_timeout(1500)
    ok(f"[{theme}] order history amounts have CNY", pg.locator("td.price .cny").count() >= 1)
    pg.goto(B + "/orders/1"); ok(f"[{theme}] order detail amount has CNY", "≈ ¥" in pg.inner_text("dl.kv"))
    # quick stop from the strip
    pg.goto(B + "/"); pg.wait_for_selector("#rs-list [data-stop='4']", timeout=15000)
    pg.screenshot(path=os.path.join(d, "dashboard.png"), full_page=True)
    pg.click("#rs-list [data-stop='4']")
    pg.wait_for_function("() => !document.querySelector(\"#rs-list [data-stop='4']\")", timeout=15000)
    ok(f"[{theme}] quick stop from the strip", "没有运行中" in pg.inner_text("#rs-count"), pg.inner_text("#rs-count"))
    pg.goto(B + "/admin"); ok(f"[{theme}] system page has no user mgmt / registration", "开放注册" not in pg.content() and "/toggle" not in pg.content())
    pg.screenshot(path=os.path.join(d, "system.png"), full_page=True)
    ok(f"[{theme}] no console/CSP errors", not errs, errs[:5])
    c.close()

with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    for t in ("light", "dark"):
        run(b, t)
    b.close()
print("\nALL PASS" if not fails else f"\n{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
