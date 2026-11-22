"""i18n checks (v1.7): English is the default, no CJK anywhere in the English UI, Chinese on request.
Against an English mock server:  DEFAULT_LANG=en python tests/run_mock_server.py 8830 ok
                                 python tests/i18n_test.py [base]
* every page, logged out and in, rendered by a real browser (server HTML + JS-rendered text + title/placeholder/aria)
* Accept-Language: zh-CN must NOT switch the language; the EN/中 switch must (and persist per user)
* static checks: every message id has an English translation, placeholders match"""
import json, os, re, subprocess, sys
from playwright.sync_api import sync_playwright
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mockcred
MU, MP = mockcred.get()
B = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8830"
CJK = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")
ALLOW = {"中", "中文", "简体中文", "语言"}     # the language switch itself is always shown in its own language
fails = []


def ok(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    cond or fails.append(name)


def cjk_bits(text):
    """Words that still contain CJK after removing the always-allowed language-switch labels."""
    bits = set()
    for w in re.findall(r"\S*[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]\S*", text):
        rest = w
        for a in sorted(ALLOW, key=len, reverse=True):
            rest = rest.replace(a, "")
        if CJK.search(rest):
            bits.add(w)
    return bits


SCAN_JS = """() => {
  const out = [document.title];
  out.push(document.body.innerText);
  for (const el of document.querySelectorAll('[title],[placeholder],[aria-label],option,input[type=submit],button[value]')) {
    for (const a of ['title', 'placeholder', 'aria-label']) if (el.getAttribute(a)) out.push(el.getAttribute(a));
    if (el.tagName === 'OPTION') out.push(el.textContent);
  }
  return out.join('\\n');
}"""


def static_checks():
    r = subprocess.run([sys.executable, os.path.join(HERE, "..", "tools_i18n_extract.py")], capture_output=True, text=True)
    ok("catalog: every message id translated", r.returncode == 0, r.stdout.strip())
    cat = json.load(open(os.path.join(HERE, "..", "app", "i18n_en.json"), encoding="utf-8"))
    bad = [k for k, v in cat.items() if sorted(re.findall(r"\{\d+[^}]*\}", k)) != sorted(re.findall(r"\{\d+[^}]*\}", v))]
    ok("catalog: placeholders match", not bad, bad[:3])
    left = [k for k, v in cat.items() if CJK.search(v) and v not in ALLOW]
    ok("catalog: English values contain no CJK", not left, left[:3])


def pages(pg):
    pg.goto(B + "/")
    links = ["/", "/stock", "/tasks/new", "/tasks/1", "/tasks/2/edit", "/tasks/4", "/orders", "/orders/1", "/orders/2",
             "/keys", "/notify", "/account", "/admin"]
    if pg.locator("a[href='/tasks/6']").count():      # MOCK_NOTIFY_TASK=1: the notify-only demo task
        links += ["/tasks/6", "/tasks/6/edit"]
    return links


def scan(pg, path, tag, wait=2500):
    pg.goto(B + path)
    pg.wait_for_load_state("networkidle", timeout=60000) if path != "/tasks/1" else None
    pg.wait_for_timeout(wait)
    bits = cjk_bits(pg.evaluate(SCAN_JS))
    ok(f"{tag} {path}: no CJK in English UI", not bits, sorted(bits)[:8])


def main():
    static_checks()
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path="/usr/bin/google-chrome" if os.path.exists("/usr/bin/google-chrome") else None)
        # Chinese browser locale + Accept-Language: must still be English
        c = b.new_context(viewport={"width": 1360, "height": 900}, locale="zh-CN",
                          extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9"})
        pg = c.new_page(); errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(B + "/login")
        ok("login page: <html lang=en> despite Accept-Language zh-CN", pg.evaluate("document.documentElement.lang") == "en")
        scan(pg, "/login", "[out]", 300)
        pg.fill("input[name=username]", MU); pg.fill("input[name=password]", MP); pg.click("form button.primary")
        pg.wait_for_load_state()
        for path in pages(pg):
            scan(pg, path, "[en]")
        # a running task writes its live log in the panel language
        pg.goto(B + "/tasks/4")
        if pg.locator("form[action='/tasks/4/start'] button").count():
            pg.click("form[action='/tasks/4/start'] button"); pg.wait_for_load_state()
        pg.wait_for_function("() => (document.querySelector('#log')?.textContent || '').includes('PREVIEW_OK')", timeout=120000)
        pg.wait_for_timeout(2500)
        log = pg.inner_text("#log")
        bits = cjk_bits(log.split("STOPPED")[-1] if "STOPPED" in log else log)
        ok("[en] running task: live log + strip in English", not bits, sorted(bits)[:8])
        scan(pg, "/", "[en running]")
        pg.goto(B + "/tasks/4")
        if pg.locator("form[action='/tasks/4/stop'] button").count():
            pg.click("form[action='/tasks/4/stop'] button"); pg.wait_for_load_state()
        ok("no JS errors on English pages", not errs, errs[:3])
        # mock account data card on dashboard actually rendered (JS i18n path exercised)
        pg.goto(B + "/"); pg.wait_for_timeout(3000)
        ok("dashboard account card rendered in English", "My servers" in pg.inner_text("body"))
        # switch to Chinese with the EN/中 switch
        pg.click(".side .langsw button[value=zh]"); pg.wait_for_load_state()
        body = pg.inner_text("body")
        ok("switch to 中文 works", pg.evaluate("document.documentElement.lang") == "zh-CN" and "控制台" in body)
        ok("scanner sanity: Chinese UI is detected as CJK", len(cjk_bits(pg.evaluate(SCAN_JS))) > 20)
        # persisted per user: a fresh browser (no cookie) logging in gets Chinese
        c2 = b.new_context(viewport={"width": 1200, "height": 800}); p2 = c2.new_page()
        p2.goto(B + "/login")
        ok("fresh browser login page is English", p2.evaluate("document.documentElement.lang") == "en")
        p2.fill("input[name=username]", MU); p2.fill("input[name=password]", MP); p2.click("form button.primary"); p2.wait_for_load_state()
        ok("language persisted on the account (zh after login elsewhere)", p2.evaluate("document.documentElement.lang") == "zh-CN")
        p2.click(".side .langsw button[value=en]"); p2.wait_for_load_state()
        ok("switch back to English", p2.evaluate("document.documentElement.lang") == "en" and "Dashboard" in p2.inner_text("body"))
        c2.close(); c.close(); b.close()
    print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAIL: {fails}"))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
