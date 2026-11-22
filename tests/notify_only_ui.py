"""Browser checks for v1.8: 仅提醒 / notify-only card (warning + link, blocked save without Telegram, server-side
guard, cooldown field, fast lane hidden), Telegram contacts as t.me links.
Against:  DEFAULT_LANG=en MOCK_NOTIFY_TASK=1 python tests/run_mock_server.py 8830 ok
          python tests/notify_only_ui.py [base] [screenshot-dir]"""
import os, re, sys
from playwright.sync_api import sync_playwright
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mockcred
MU, MP = mockcred.get()
B = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8830"
OUT = sys.argv[2] if len(sys.argv) > 2 else None
fails = []


def ok(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    cond or fails.append(name)


def shot(pg, name, el=None):
    if OUT:
        os.makedirs(OUT, exist_ok=True)
        (pg.locator(el) if el else pg).screenshot(path=os.path.join(OUT, name))


with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome" if os.path.exists("/usr/bin/google-chrome") else None)
    for scheme in ("light", "dark"):
        c = b.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2, color_scheme=scheme)
        pg = c.new_page(); errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(B + "/login"); pg.fill("input[name=username]", MU); pg.fill("input[name=password]", MP)
        pg.click("form button.primary"); pg.wait_for_load_state()
        tag = f"[{scheme}]"
        # make sure Telegram is NOT configured for the first part
        pg.goto(B + "/notify")
        tg = pg.locator("a.tglink")
        hrefs = sorted(tg.evaluate_all("a => a.map(x => [x.href, x.target, x.rel])"))
        ok(f"{tag} Telegram help: @BotFather + @userinfobot are t.me links in a new tab",
           [h[0] for h in hrefs] == ["https://t.me/BotFather", "https://t.me/userinfobot"] and all(h[1] == "_blank" and "noopener" in h[2] for h in hrefs), hrefs)
        if pg.locator("input[name=clear_tg]").count():   # start each pass without Telegram
            pg.check("input[name=clear_tg]"); pg.fill("input[name=tg_chat]", "")
            pg.click("form[action='/notify/save'] button.primary"); pg.wait_for_load_state(); pg.goto(B + "/notify")
        shot(pg, f"tg-settings-{scheme}.png", ".card >> nth=0")

        pg.goto(B + "/tasks/new"); pg.wait_for_load_state("load"); pg.wait_for_timeout(1500)
        card = pg.locator("label.choice.c-notify")
        ok(f"{tag} notify-only card exists and is first", card.count() == 1 and pg.locator(".choices .choice").first.get_attribute("class").endswith("c-notify"))
        ok(f"{tag} card text English", "Notify only" in card.inner_text() and "No API keys" in card.inner_text())
        tgok = pg.evaluate("TG_OK")
        card.click(); pg.wait_for_timeout(200)
        ok(f"{tag} fast lane hidden for notify-only", not pg.locator("#prebuildcard").is_visible())
        ok(f"{tag} cooldown field shown for notify-only", pg.locator("#cooldowncard").is_visible())
        ok(f"{tag} real-order warning hidden", not pg.locator("#realwarn").is_visible())
        if not tgok:
            w = pg.locator("#tgwarn")
            ok(f"{tag} inline warning on the card when Telegram is missing", w.is_visible() and "Telegram" in w.inner_text())
            ok(f"{tag} warning links to the notification settings", w.locator("a[href='/notify']").count() == 1)
            shot(pg, f"notify-only-card-warning-{scheme}.png", ".card:has(.choices)")
            # client-side block (the picker needs a plan first, so seed one and submit)
            pg.evaluate("SEL.items.push({planCode:'24sk202', type:'eco', label:'KS-2', name:'', memory:'', storage:'', bandwidth:'', dcs:[], memL:'', stoL:''}); SEL.onChange()")
            url = pg.url
            pg.click("form#tf button.primary"); pg.wait_for_timeout(800)
            ok(f"{tag} save blocked in the browser without Telegram", pg.url == url)
            # server-side guard: post the form directly
            r = pg.evaluate("""async () => { const f = new FormData(document.getElementById('tf'));
                 f.set('pay_mode', 'notify'); f.set('plans_json', JSON.stringify([{planCode:'24sk202', type:'eco'}]));
                 const r = await fetch('/tasks/save', {method:'POST', body:f, redirect:'manual'}); return r.type + ' ' + r.status; }""")
            pg.goto(B + "/"); body = pg.inner_text("body")
            ok(f"{tag} server rejects notify-only save without Telegram", "Set up Telegram" in body or "Telegram isn't set up" in body, body[:200])
            n_before = pg.locator("table a[href^='/tasks/']").count()
            pg.goto(B + "/tasks/6")
            ok(f"{tag} notify task detail: start disabled + warning", pg.locator("form[action='/tasks/6/start'] button[disabled]").count() == 1 and pg.locator(".warn a[href='/notify']").count() == 1)
            r = pg.evaluate("""async () => { const f = new FormData(); f.set('csrf', document.querySelector('input[name=csrf]').value);
                 await fetch('/tasks/6/start', {method:'POST', body:f}); return 1; }""")
            pg.goto(B + "/tasks/6")
            ok(f"{tag} server refuses to start notify-only without Telegram", "Stopped" in pg.inner_text("#st") or pg.locator("form[action='/tasks/6/start']").count() == 1)
        # configure Telegram, then it saves and starts
        pg.goto(B + "/notify")
        pg.fill("input[name=tg_token]", "123456:" + "A" * 35); pg.fill("input[name=tg_chat]", "42")
        pg.click("form[action='/notify/save'] button.primary"); pg.wait_for_load_state()
        pg.goto(B + "/tasks/6/edit"); pg.wait_for_load_state("load"); pg.wait_for_timeout(1500)
        ok(f"{tag} with Telegram: no warning on the card", pg.locator("#tgwarn").count() == 0)
        ok(f"{tag} edit form keeps notify-only selected", pg.locator("input[name=pay_mode][value=notify]").is_checked())
        shot(pg, f"notify-only-card-{scheme}.png", ".card:has(.choices)")
        pg.click("form#tf button.primary"); pg.wait_for_load_state()
        ok(f"{tag} notify-only task saves", "/tasks/6" in pg.url and "Telegram" in pg.inner_text("body"), pg.url)
        if scheme == "light":
            pg.click("form[action='/tasks/6/start'] button"); pg.wait_for_load_state()
            pg.wait_for_function("() => (document.querySelector('#log')?.textContent || '').includes('HEARTBEAT')", timeout=120000)
            log = pg.inner_text("#log")
            ok("notify-only run: START says Notify only, no OVH login (keys never used)", "Notify only" in log and "AUTH_OK" not in log, log[-600:])
            shot(pg, "notify-only-task-running-light.png")
            pg.click("form[action='/tasks/6/stop'] button"); pg.wait_for_load_state()
        pg.goto(B + "/"); pg.wait_for_timeout(500)
        ok(f"{tag} dashboard shows the Notify only tag", "Notify only" in pg.inner_text("body"))
        ok(f"{tag} no JS errors", not errs, errs[:3])
        c.close()
    b.close()
print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAIL: {fails}"))
sys.exit(1 if fails else 0)
