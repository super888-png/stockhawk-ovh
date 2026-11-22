"""Render the Telegram restock card (normal + sold-out edit, EN + ZH) into a Telegram-like mock and screenshot it.
python tests/tg_card_preview.py OUT_DIR"""
import html, os, sys, tempfile
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("SECRET_KEY", "preview-" + "k" * 40)
os.environ["DATA_DIR"] = tempfile.mkdtemp()
from app import stockcard, notify, labels  # noqa: E402
from app.i18n import T, lang_scope, relang  # noqa: E402
from app.dcmap import SITES  # noqa: E402
from app.ovh_engine import dc_label  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp"


def card(lang, sold):
    with lang_scope(lang):
        i = relang({**notify.SAMPLE_CARD, "mode": T("仅提醒"), "task": "Task #3" if lang == "en" else "任务 #3"}, lang)
        i["memory"] = labels.ram_label(i["memory_code"]); i["storage"] = labels.disk_label(i["storage_code"])
        i["city"] = SITES["rbx"][0]; i["others"] = dc_label("gra"); i["detected"] = "2026-11-21 23:04:05"
        i["links"] = stockcard.order_links(**i["order"])
        h, _, kb = stockcard.render(i, sold_at="2026-11-21 23:10:12" if sold else None)
        return str(h), kb


def bubble(h, kb):
    keys = "".join(f'<div class="k">{html.escape(str(b["text"]))} <span>↗</span></div>' for row in kb for b in row)
    return f'<div class="msg"><div class="b">{h.replace(chr(10), "<br>")}<div class="t">23:04</div></div><div class="kb">{keys}</div></div>'


CSS = """body{margin:0;font:15px/1.38 -apple-system,'Segoe UI',Roboto,'Noto Sans CJK SC',sans-serif;background:#0e1621;color:#f5f5f5}
.wrap{display:flex;gap:28px;padding:26px;background:linear-gradient(#0e1621,#17212b)}.col{width:430px}
h3{font:600 13px sans-serif;color:#7f91a4;margin:0 0 10px;text-transform:uppercase;letter-spacing:.06em}
.hdr{display:flex;align-items:center;gap:10px;margin-bottom:14px}.av{width:38px;height:38px;border-radius:50%;background:#0b1630;padding:5px;box-sizing:border-box}
.hdr b{display:block}.hdr small{color:#7f91a4}
.msg{max-width:420px}.b{background:#182533;border-radius:14px 14px 14px 4px;padding:9px 12px 20px;position:relative;word-wrap:break-word}
.b s{color:#9aa7b4}.t{position:absolute;right:10px;bottom:4px;font-size:12px;color:#6d7f8f}
.kb{display:grid;gap:4px;margin-top:4px}.k{background:rgba(24,37,51,.75);border-radius:8px;text-align:center;padding:9px;font-weight:500;font-size:14px}
.k span{float:right;font-size:11px;color:#8ea4b8}"""


import base64
AV = base64.b64encode(open(os.path.join(ROOT, "app/static/brand/stockhawk-icon.svg"), "rb").read()).decode()


def page(lang):
    a, b = card(lang, False), card(lang, True)
    lab = ("New restock alert", "Edited when sold out") if lang == "en" else ("补货提醒", "售罄后自动编辑原消息")
    cols = "".join(f'<div class="col"><h3>{l}</h3><div class="hdr"><img class="av" src="data:image/svg+xml;base64,{AV}"><div><b>StockHawk</b><small>bot</small></div></div>{bubble(*c)}</div>'.replace('{AV}', AV)
                   for l, c in ((lab[0], a), (lab[1], b)))
    return f"<!doctype html><meta charset=utf-8><style>{CSS}</style><div class=wrap>{cols}</div>"


with sync_playwright() as p:
    br = p.chromium.launch(executable_path="/usr/bin/google-chrome")
    pg = br.new_page(viewport={"width": 960, "height": 700}, device_scale_factor=2)
    for lang in ("en", "zh"):
        pg.set_content(page(lang)); pg.locator(".wrap").screenshot(path=os.path.join(OUT, f"telegram-card-{lang}.png"))
        print("wrote", f"telegram-card-{lang}.png")
    br.close()
