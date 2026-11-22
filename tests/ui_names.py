"""v1.5b browser check: readable plan names/config, 我的服务器 on dashboard, clean 账号订单 summaries."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mockcred
from playwright.sync_api import sync_playwright
B = "http://127.0.0.1:8810"; MU, MP = mockcred.get()
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "screenshots", "v15")
bad = 0
def ok(n, c):
    global bad; print(("PASS " if c else "FAIL ") + n); bad += (not c)
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"))
    for theme in ("light", "dark"):
        os.makedirs(f"{OUT}/{theme}", exist_ok=True)
        ctx = b.new_context(viewport={"width": 1360, "height": 900}, color_scheme=theme)
        pg = ctx.new_page(); errs = []
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None); pg.on("pageerror", lambda e: errs.append("JS: " + str(e)))
        pg.goto(B + "/login"); pg.fill("input[name=username]", MU); pg.fill("input[name=password]", MP); pg.click("form button.primary"); pg.wait_for_load_state()
        pg.goto(B + "/tasks/5"); t = pg.inner_text("table.t")
        ok(f"[{theme}] task 5 plan table: KS-B + CPU + readable config",
           all(x in t for x in ("KS-B", "Intel Xeon E5-1620v2", "25skb012", "32 GB ECC 1333", "1×120 GB SSD（无 RAID）", "500 Mbps"))
           and "ram-32g-ecc-1333-25skb01" not in t)
        pg.locator(".card", has_text="监控型号（按优先级）").first.screenshot(path=f"{OUT}/{theme}/plans_table.png")
        pg.goto(B + "/"); pg.wait_for_selector("#myservers .srv:not(.sk-card)", timeout=20000)
        dash = pg.inner_text("main") if pg.locator("main").count() else pg.content()
        ok(f"[{theme}] dashboard has 我的服务器 with servers", "我的服务器" in dash and "ns3141592" in pg.inner_text("#myservers"))
        ok(f"[{theme}] dashboard task list shows model names", "KS-B" in pg.inner_text("table.t >> nth=0"))
        pg.locator("#myservers").screenshot(path=f"{OUT}/{theme}/my_servers.png")
        pg.goto(B + "/orders"); pg.wait_for_selector("tr#ovh-order-231960777", timeout=20000)
        ok(f"[{theme}] orders page has no server cards", pg.locator(".srvgrid").count() == 0 and "账号服务器" not in pg.content())
        row = pg.inner_text("tr#ovh-order-231960777 td.items")
        ok(f"[{theme}] KS-LE-3 order summarized", row.startswith("KS-LE-3 · BHS · Intel Xeon E-2274G") and "*001.001" not in row
           and "500 Mbps" in row and "1 个月" in row and row.count("ns1234567") == 1 and "原始明细 8 行" in row, )
        print("   ", row.replace("\n", " | "))
        ok(f"[{theme}] invoice/debt short labels", pg.inner_text("tr#ovh-order-231960100 td.items").startswith("账单")
           and pg.inner_text("tr#ovh-order-231950042 td.items").startswith("欠款支付"))
        pg.click("tr#ovh-order-231960777 details summary")
        ok(f"[{theme}] raw details expandable", "rental - datacenter bhs" in pg.inner_text("tr#ovh-order-231960777 details"))
        pg.locator(".acct .card").first.screenshot(path=f"{OUT}/{theme}/account_orders.png")
        pg.goto(B + "/orders/1")
        ok(f"[{theme}] order detail model name", "RISE-1" in pg.inner_text("dl.kv") and "24rise01" in pg.inner_text("dl.kv") and "Intel Xeon-E 2386G" in pg.inner_text("dl.kv"))
        ok(f"[{theme}] no console errors", not errs); [print("   ", e) for e in errs]
        ctx.close()
    b.close()
print("ALL PASS" if not bad else f"BAD {bad}")
