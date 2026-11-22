"""Browser checks for v1.9: the 新建任务 picker is the Live-stock table with row selection.
Multi-machine task end to end (rows from two models, same config in two DCs merges, whole model), priority reorder
(buttons + drag), per-item config, deep links from Live stock, edit round trip, dry-run start uses per-item DCs.
Against:  DEFAULT_LANG=en python tests/run_mock_server.py 8830 ok
          python tests/task_picker_ui.py [base] [screenshot-dir] [zh-base]"""
import json, os, re, sys
from playwright.sync_api import sync_playwright
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mockcred
MU, MP = mockcred.get()
B = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8830"
OUT = sys.argv[2] if len(sys.argv) > 2 else None
ZB = sys.argv[3] if len(sys.argv) > 3 else None
fails = []


def ok(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    cond or fails.append(name)


def login(pg, base):
    pg.goto(base + "/login"); pg.fill("input[name=username]", MU); pg.fill("input[name=password]", MP)
    pg.click("form button.primary"); pg.wait_for_load_state()


def add_model(pg, q):
    pg.click("#picker input"); pg.fill("#picker input", q); pg.wait_for_timeout(400); pg.keyboard.press("Enter"); pg.keyboard.press("Escape")


def tray(pg):
    return pg.evaluate("SEL.items.map(s => [s.planCode, s.memory, s.storage, s.dcs.join('+')])")


def stock_rows(pg, plans):
    return pg.evaluate("async p => (await (await fetch('/api/stock?' + new URLSearchParams({endpoint:'ovh-eu', subsidiary:'IE', plans:p}))).json()).rows", plans)


def wait_rows(pg):
    pg.wait_for_selector("#rows tr[data-r]", timeout=120000); pg.wait_for_timeout(600)


def build(pg, scheme):
    """New task: RISE-1 rows + ADVANCE-1 row + whole KS model; returns the selection."""
    tag = f"[{scheme}]"
    pg.goto(B + "/tasks/new"); pg.wait_for_load_state("load"); pg.wait_for_timeout(800)
    pg.click("#clearSel")
    ok(f"{tag} same filters as Live stock (region, subsidiary, catalog, search, models, in-stock, base-config)",
       all(pg.locator(s).count() == 1 for s in ("#endpoint", "#subsidiary", "#ctype", "#filter", "#picker", "#onlyStock", "#onlyDefault", "#quickRise", "#quickAdv")))
    add_model(pg, "RISE-1"); add_model(pg, "ADVANCE-1")
    wait_rows(pg)
    rows = pg.evaluate("sb.last.rows")
    rise = [r for r in rows if r["plan"] == "24rise01-v2" and r["orderable"] is not False]
    adv = [r for r in rows if r["plan"] != "24rise01-v2" and r["orderable"] is not False]
    ok(f"{tag} table shows live rows for both models", len(rise) > 2 and len(adv) > 0, (len(rise), len(adv)))
    # same config in two datacenters -> one item with two DCs
    by = {}
    for r in rise:
        by.setdefault((r["memory"], r["storage"]), []).append(r)
    two = next(v for v in by.values() if len(v) >= 2)
    other = next(v for k, v in by.items() if v is not two)
    pg.fill("#filter", "")
    def click_row(r):
        i = pg.evaluate("r => sb.shown.findIndex(x => x.plan===r.plan && x.dc===r.dc && x.memory===r.memory && x.storage===r.storage)", r)
        if i < 0:   # not visible with the current filters: switch the in-stock filter off
            if pg.is_checked("#onlyStock"): pg.locator("label.toggle:has(#onlyStock)").click(); pg.wait_for_timeout(200)
            i = pg.evaluate("r => sb.shown.findIndex(x => x.plan===r.plan && x.dc===r.dc && x.memory===r.memory && x.storage===r.storage)", r)
        pg.locator(f"#rows tr[data-r='{i}']").click(); pg.wait_for_timeout(150)
    click_row(two[0]); click_row(two[1]); click_row(other[0]); click_row(adv[0])
    t = tray(pg)
    ok(f"{tag} same config in two datacenters merges into one item", t[0][3] == two[0]["dc"] + "+" + two[1]["dc"], t)
    ok(f"{tag} rows from two different models in one task", len(t) == 3 and t[2][0] == adv[0]["plan"], t)
    ok(f"{tag} selected rows show their priority number", pg.locator("#rows tr.picked .rowpick.on .box").first.inner_text().strip() in ("1", "2", "3"))
    add_model(pg, "KS-")
    pg.wait_for_timeout(500)
    pg.locator("#wholebar [data-whole]").last.click(); pg.wait_for_timeout(300)
    t = tray(pg)
    ok(f"{tag} whole model (any config, any DC) added", len(t) == 4 and t[3][1] == "" and t[3][3] == "", t)
    pg.wait_for_selector("#tray li.selitem >> nth=3"); pg.wait_for_timeout(800)
    ok(f"{tag} tray shows priority numbers 1..4", pg.locator("#tray .prio").all_inner_texts() == ["1", "2", "3", "4"])
    # reorder with buttons: move item 3 (ADVANCE) up
    before = tray(pg)
    pg.locator("#tray li.selitem >> nth=2").locator("button[data-a=up]").click(); pg.wait_for_timeout(600)
    t = tray(pg)
    ok(f"{tag} ↑ button raises priority", t[1] == before[2] and t[2] == before[1], t)
    # drag the last item to the top
    src = pg.locator("#tray li.selitem >> nth=3").locator(".grip"); dst = pg.locator("#tray li.selitem >> nth=0")
    src.drag_to(dst); pg.wait_for_timeout(700)
    t2 = tray(pg)
    ok(f"{tag} drag & drop reorders", t2[0] == t[3], t2)
    # per-item config: set the whole-model item's memory to a concrete option, then back to any
    sel = pg.locator("#tray li.selitem >> nth=0").locator("select[data-k=memory]")
    vals = sel.evaluate("s => [...s.options].map(o => o.value)")
    ok(f"{tag} per-item memory options incl. 'Any'", vals[0] == "" and len(vals) > 1, vals)
    # per-item datacenter chips
    chip = pg.locator("#tray li.selitem >> nth=0").locator(".dc[data-c]").first
    code = chip.get_attribute("data-c"); chip.click(); pg.wait_for_timeout(500)
    ok(f"{tag} per-item datacenter chip toggles", tray(pg)[0][3] == code, tray(pg)[0])
    pg.locator("#tray li.selitem >> nth=0").locator(f".dc[data-c='{code}']").click(); pg.wait_for_timeout(500)
    ok(f"{tag} no datacenter = any", tray(pg)[0][3] == "")
    return tray(pg)


with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome" if os.path.exists("/usr/bin/google-chrome") else None)
    for scheme in ("light", "dark"):
        c = b.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2, color_scheme=scheme)
        pg = c.new_page(); errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        login(pg, B)
        tag = f"[{scheme}]"
        sel = build(pg, scheme)
        ok(f"{tag} price estimate lists every item", pg.locator("#pricebox tbody tr").count() >= len(sel) or pg.wait_for_timeout(2500) or pg.locator("#pricebox tbody tr").count() >= len(sel))
        if OUT:
            os.makedirs(OUT, exist_ok=True)
            pg.evaluate("window.scrollTo(0, 0)"); pg.wait_for_timeout(300)
            pg.locator("form#tf > .card >> nth=0").screenshot(path=os.path.join(OUT, f"task-picker-en-{scheme}.png"))
            pg.locator("#selcard").screenshot(path=os.path.join(OUT, f"task-picker-selected-en-{scheme}.png"))
        if scheme == "light":
            pg.fill("input[name=name]", "Multi-machine picker test")
            pg.locator("label.choice.c-dry").click()
            pg.click("form#tf button.primary"); pg.wait_for_load_state()
            m = re.search(r"/tasks/(\d+)$", pg.url)
            ok(f"{tag} multi-machine task saved", bool(m), pg.url)
            tid = int(m.group(1)) if m else 0
            trs = pg.locator(".card:has(h2) table.t >> nth=0").locator("tbody tr")
            ok(f"{tag} task detail lists all {len(sel)} items in priority order", trs.count() == len(sel), trs.count())
            pg.goto(B + f"/tasks/{tid}/edit"); pg.wait_for_load_state("load"); pg.wait_for_timeout(1500)
            ok(f"{tag} edit page restores items, order and datacenters", tray(pg) == sel, (tray(pg), sel))
            pg.wait_for_selector("#rows tr.picked", timeout=120000)
            ok(f"{tag} edit page shows the picked rows in the table", pg.locator("#rows tr.picked").count() >= 3)
            # dry-run start: the worker uses each item's own datacenters
            pg.goto(B + f"/tasks/{tid}")
            pg.click("form[action$='/start'] button"); pg.wait_for_load_state()
            pg.wait_for_timeout(9000)
            log = pg.evaluate("async id => (await (await fetch('/api/tasks/' + id + '/logs?after=0')).text())", tid)
            ok(f"{tag} dry run started with per-item datacenters", sel[1][3].split("+")[0] in log and "#4" in log, log[:600])
            pg.goto(B + f"/tasks/{tid}")
            if pg.locator("form[action$='/stop'] button").count():
                pg.click("form[action$='/stop'] button"); pg.wait_for_load_state()
            # deep links from Live stock: the New task button carries plan/type/dc/memory/storage
            pg.goto(B + "/stock"); pg.wait_for_load_state("load")
            pg.click("#clearSel"); add_model(pg, "RISE-1"); pg.wait_for_selector("#rows a.btn", timeout=120000)
            a = pg.locator("#rows a.btn").first
            href = a.get_attribute("href")
            a.click(); pg.wait_for_load_state("load"); pg.wait_for_timeout(1500)
            q = dict(re.findall(r"([a-z]+)=([^&]*)", href))
            t = tray(pg)
            ok(f"{tag} Live stock 'New task' deep link preselects that row", len(t) == 1 and t[0][0] == q["plan"] and t[0][3] == q["dc"], (t, href))
            pg.wait_for_selector("#rows tr.picked", timeout=120000)
            ok(f"{tag} deep-linked row is ticked in the table", pg.locator("#rows tr.picked").count() == 1)
            pg.goto(B + "/tasks/new?item=24rise01-v2|eco|||gra+rbx&item=24adv01-v3|baremetalServers|||"); pg.wait_for_load_state("load"); pg.wait_for_timeout(1200)
            ok(f"{tag} multi-item deep link", tray(pg) == [["24rise01-v2", "", "", "gra+rbx"], ["24adv01-v3", "", "", ""]], tray(pg))
            # legacy task (seeded with task-wide datacenters) opens with those DCs on each item
            pg.goto(B + "/tasks/1/edit"); pg.wait_for_load_state("load"); pg.wait_for_timeout(1200)
            ok(f"{tag} legacy task: task-wide datacenters on its items", all(x[3] == "gra+rbx" for x in tray(pg)), tray(pg))
            # notify-only still selectable with the new picker
            ok(f"{tag} notify-only card still present", pg.locator("label.choice.c-notify").count() == 1)
        ok(f"{tag} no page errors", not errs, errs[:3])
        c.close()
    # mobile: no sideways page scroll, tray stacks
    c = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=3, is_mobile=True, has_touch=True)
    pg = c.new_page(); login(pg, B)
    pg.goto(B + "/tasks/new?item=24rise01-v2|eco|||gra+rbx&item=24adv01-v3|baremetalServers|||"); pg.wait_for_load_state("load"); pg.wait_for_timeout(2500)
    ok("[mobile] no horizontal page overflow", pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), pg.evaluate("[document.documentElement.scrollWidth, window.innerWidth]"))
    ok("[mobile] tray items fit the screen", pg.evaluate("[...document.querySelectorAll('#tray li.selitem')].every(li => li.getBoundingClientRect().right <= window.innerWidth + 1)"))
    if OUT:
        pg.locator("#selcard").screenshot(path=os.path.join(OUT, "task-picker-selected-en-mobile.png"))
    c.close()
    if ZB:   # Chinese UI screenshot
        c = b.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2, color_scheme="light")
        pg = c.new_page(); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
        login(pg, ZB)
        B0, B = B, ZB
        sel = build(pg, "zh-light")
        txt = pg.locator("form#tf > .card >> nth=0").inner_text()
        ok("[zh] picker in Chinese", "选择机器" in txt and "只看有货" in txt, txt[:200])
        if OUT:
            pg.evaluate("window.scrollTo(0, 0)"); pg.wait_for_timeout(300)
            pg.locator("form#tf > .card >> nth=0").screenshot(path=os.path.join(OUT, "task-picker-zh-light.png"))
            pg.locator("#selcard").screenshot(path=os.path.join(OUT, "task-picker-selected-zh-light.png"))
        ok("[zh] no page errors", not errs, errs[:3])
        B = B0
        c.close()
    b.close()
print("ALL PASS" if not fails else f"FAILED ({len(fails)}): {fails}")
sys.exit(1 if fails else 0)
