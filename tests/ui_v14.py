"""v1.4 browser checks + screenshots (light/dark) against tests/run_mock_server.py 8810 ok.
python tests/ui_v14.py [base]"""
import os, sys
from playwright.sync_api import sync_playwright
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mockcred
MU, MP = mockcred.get()

B = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8810"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "screenshots", "v14")
fails = []
def ok(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    cond or fails.append(name)

def login(pg):
    pg.goto(B + "/login")
    if pg.locator("input[name=username]").count():
        pg.fill("input[name=username]", MU); pg.fill("input[name=password]", MP)
        pg.click("form button.primary"); pg.wait_for_load_state()

def state(pg):
    return pg.evaluate("[document.querySelector('#picker').classList.contains('open'), document.activeElement===document.querySelector('#picker input')]")

def picker_test(pg, path, theme):
    pg.goto(B + path); pg.evaluate("localStorage.removeItem('ovhsn.stock.plans')"); pg.goto(B + path)
    pg.click("#picker input"); pg.wait_for_selector("#picker .opt", timeout=90000)
    ok(f"[{theme}] {path} picker opens on click", state(pg)[0])
    n0 = pg.locator("#picker .chip").count()
    pg.locator("#picker .opt:not(.sel)").first.click(); pg.wait_for_timeout(150)
    ok(f"[{theme}] {path} stays open after adding", state(pg) == [True, True] and pg.locator("#picker .chip").count() == n0 + 1, state(pg))
    pg.mouse.click(1340, 880); pg.wait_for_timeout(250)
    ok(f"[{theme}] {path} closes on blank click", state(pg)[0] is False)
    pg.click("#picker input"); pg.wait_for_timeout(200)
    ok(f"[{theme}] {path} REOPENS on click after add+blank (bug)", state(pg)[0] is True, state(pg))
    pg.mouse.click(1340, 880); pg.wait_for_timeout(200)
    bb = pg.locator("#picker .box").bounding_box()
    pg.mouse.click(bb["x"] + bb["width"] - 6, bb["y"] + bb["height"] / 2); pg.wait_for_timeout(200)
    ok(f"[{theme}] {path} reopens on click inside the box", state(pg)[0] is True)
    pg.keyboard.press("Escape"); pg.wait_for_timeout(100)
    pg.keyboard.type("adv"); pg.wait_for_timeout(200)
    ok(f"[{theme}] {path} reopens on typing", state(pg)[0] is True)
    pg.locator("#picker .opt:not(.sel)").first.click(); pg.wait_for_timeout(150)
    ok(f"[{theme}] {path} second add keeps open", state(pg)[0] is True)
    pg.keyboard.press("Escape"); pg.mouse.click(1340, 880)

def run(b, theme):
    d = os.path.join(OUT, theme); os.makedirs(d, exist_ok=True)
    c = b.new_context(viewport={"width": 1360, "height": 900}, color_scheme="light")
    c.add_init_script(f"try{{localStorage.setItem('ovhsn.theme','{theme}')}}catch(e){{}}")
    pg = c.new_page()
    csp = []
    pg.on("console", lambda m: csp.append(m.text) if "Content Security Policy" in m.text or "Refused to" in m.text else None)
    pg.on("pageerror", lambda e: csp.append("JS: " + str(e)))
    login(pg)
    shot = lambda name: pg.screenshot(path=os.path.join(d, name + ".png"), full_page=True)

    # --- stock: picker + toggles
    picker_test(pg, "/stock", theme)
    tog = pg.locator("label.toggle:has(#onlyStock)")
    bg = lambda: pg.evaluate("getComputedStyle(document.querySelector('#onlyStock + .tr')).backgroundColor")
    before, chk = bg(), pg.is_checked("#onlyStock")
    tog.click(); pg.wait_for_timeout(250)
    ok(f"[{theme}] stock toggle flips state and look", pg.is_checked("#onlyStock") != chk and bg() != before, (before, bg()))
    tog.click(); pg.wait_for_timeout(2500)
    ok(f"[{theme}] stock FX line shows rate", "¥" in pg.inner_text("#fxline") or "CNY" in pg.inner_text("#fxline"), pg.inner_text("#fxline"))
    shot("stock")

    # --- task form: picker, pay mode, price
    picker_test(pg, "/tasks/new", theme)
    pg.click("label.choice.c-order"); pg.wait_for_timeout(150)
    ok(f"[{theme}] pay mode 'order' selected via card", pg.is_checked("input[name=pay_mode][value=order]"))
    ok(f"[{theme}] order-only warning text", "手动" in pg.inner_text("#realwarn_t") or "付款" in pg.inner_text("#realwarn_t"), pg.inner_text("#realwarn_t"))
    pg.wait_for_function("document.querySelector('#pricebox').innerText.includes('首期合计')", timeout=60000)
    txt = pg.inner_text("#pricebox")
    ok(f"[{theme}] price breakdown monthly/setup/tax/total + CNY", all(k in txt for k in ("月付", "安装", "税", "首期合计", "¥")), txt[:300])
    ok(f"[{theme}] FX rate + time shown", "1 " in pg.inner_text("#fxline") or "汇率" in pg.inner_text("#fxline"), pg.inner_text("#fxline"))
    pg.locator("#pricebox").scroll_into_view_if_needed()
    shot("task_form")
    pg.locator(".choices").screenshot(path=os.path.join(d, "pay_mode.png"))
    pg.locator("#pricebox").locator("xpath=..").screenshot(path=os.path.join(d, "price_box.png"))

    # --- notify
    pg.goto(B + "/notify")
    ok(f"[{theme}] notify: 5 category switches", pg.locator("input[name^=cat_]").count() == 5)
    ok(f"[{theme}] notify: 5 test buttons", pg.locator("button[formaction='/notify/test']").count() == 5)
    shot("notify")
    pg.fill("input[name=tg_chat]", "123456789"); pg.press("input[name=tg_chat]", "Enter"); pg.wait_for_load_state()
    ok(f"[{theme}] notify: Enter saves (not test)", "/notify" in pg.url and ("已保存" in pg.content() or "保存" in pg.content()) and "测试" not in (pg.locator(".flash").first.inner_text() if pg.locator(".flash").count() else ""),
       pg.locator(".flash").first.inner_text() if pg.locator(".flash").count() else "")

    # --- orders + detail (lottery)
    pg.goto(B + "/orders"); pg.wait_for_timeout(1500)
    ok(f"[{theme}] orders: lottery tag + detail link", pg.locator("text=中奖").count() >= 1 and pg.locator("a[href='/orders/1']").count() >= 1)
    pg.goto(B + "/orders/1")
    ok(f"[{theme}] order detail: lottery banner", pg.locator(".lottery").count() == 1 and "64 GB" in pg.inner_text(".lottery"))
    ok(f"[{theme}] order detail: official vs actual", "官方配置" in pg.content() and "实际交付" in pg.content() and "升级" in pg.inner_text("table.cmp"))
    ok(f"[{theme}] order detail: timeline 3 steps done", pg.locator(".timeline li.done").count() == 3)
    shot("order_detail")

    # --- account, dashboard, admin
    pg.goto(B + "/account"); ok(f"[{theme}] account page", pg.locator("text=修改密码").count() >= 1); shot("account")
    pg.goto(B + "/"); pg.wait_for_timeout(800); shot("dashboard")
    ok(f"[{theme}] dashboard: mode tags", pg.locator("td .tag:text-matches('只预览|只下单|下单并付款')").count() >= 1)
    pg.goto(B + "/admin"); shot("admin")
    ok(f"[{theme}] times localized (time[data-lt] rewritten)", pg.evaluate("[...document.querySelectorAll('time[data-lt]')].every(t => t.title)"))
    # data-confirm works (dismiss -> no navigation)
    pg.goto(B + "/tasks/4")
    pg.once("dialog", lambda dlg: dlg.dismiss())
    btn = pg.locator("button[data-confirm]").first
    if btn.count():
        u0 = pg.url; btn.click(); pg.wait_for_timeout(500)
        ok(f"[{theme}] data-confirm cancel keeps page", pg.url == u0)
    shot("task_detail")
    ok(f"[{theme}] no CSP violations / JS errors", not csp, csp[:5])
    # mobile task form
    m = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True, color_scheme="light")
    m.add_init_script(f"try{{localStorage.setItem('ovhsn.theme','{theme}')}}catch(e){{}}")
    mp = m.new_page(); login(mp)
    mp.goto(B + "/orders/1"); mp.wait_for_timeout(600); mp.screenshot(path=os.path.join(d, "mobile_order_detail.png"), full_page=True)
    mp.goto(B + "/notify"); mp.screenshot(path=os.path.join(d, "mobile_notify.png"), full_page=True)
    m.close(); c.close()

with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    for t in ("light", "dark"):
        run(b, t)
    b.close()
print("\nALL PASS" if not fails else f"\n{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
