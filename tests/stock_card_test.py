"""Telegram restock card, offline with a mocked Bot API: layout (no emoji, HTML-escaped), order buttons, bilingual,
edit to strikethrough + "Sold out at" when the stock goes, new message on restock, debounce.   python tests/stock_card_test.py"""
import json, os, re, sys, tempfile, threading, time, types
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("SECRET_KEY", "unit-only-" + "k" * 40)
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="ovhsn-card-")
os.environ["APP_TZ"] = "Asia/Shanghai"
fails = []


def ok(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    cond or fails.append(name)


from app.db import SessionLocal, auto_migrate  # noqa: E402
from app.models import NotifySetting, User  # noqa: E402
from app.security import enc  # noqa: E402
from app import notify, stockcard, worker  # noqa: E402
from app.i18n import T  # noqa: E402
auto_migrate()
TOKEN = "123456:" + "A" * 35
with SessionLocal() as db:
    db.add(User(id=1, username="u", password_hash="x", is_admin=True, lang=""))
    db.add(User(id=2, username="z", password_hash="x", is_admin=False, lang=""))
    db.flush()
    db.add(NotifySetting(user_id=1, telegram_token_enc=enc(TOKEN), telegram_chat_id="42", lang="en", stock_debounce=600))
    db.add(NotifySetting(user_id=2, telegram_token_enc=enc(TOKEN), telegram_chat_id="43", lang="zh", stock_debounce=600))
    db.commit()

calls = []
mid = iter(range(100, 1000))


class R:
    def __init__(self, body, code=200):
        self.status_code, self._b = code, body

    def json(self):
        return self._b


def fake_post(url, json=None, timeout=None):
    method = url.rsplit("/", 1)[1]
    calls.append((url, method, json))
    if method == "sendMessage":
        return R({"ok": True, "result": {"message_id": next(mid), "chat": {"id": json["chat_id"]}}})
    return R({"ok": True, "result": True})


notify.httpx.post = fake_post
EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")
info = {"name": "KS-LE-B <beta> & co", "cpu": "Intel Xeon-E 2274G", "memory_code": "ram-32g-ecc-2666", "storage_code": "softraid-2x4000sa",
        "dc": "rbx", "availability": "1H-low", "sym": "€", "price": 25.99, "setup": 0.0, "cny_price": "≈ ¥190.82", "mode": T("仅提醒"),
        "task": "Task #3", "ts": 1795273445.0, "others_codes": ["gra"],
        "order": {"endpoint": "ovh-eu", "subsidiary": "IE", "plan_code": "24sklb01", "catalog_type": "eco", "dc": "rbx", "region": "europe"}}
errs = notify.send_card(1, info, ref="t:1", dedupe_key="d1")
ok("send: no errors", errs == [], errs)
ok("send: one sendMessage call", [c[1] for c in calls] == ["sendMessage"], calls)
p = calls[0][2]
txt = p["text"]
print(txt)
ok("HTML parse mode, chat id from settings", p["parse_mode"] == "HTML" and p["chat_id"] == "42")
lines = txt.split("\n")
ok("bold title line 'OVH Restock'", lines[0] == "<b>OVH Restock</b>", lines[0])
ok("headline then blank line", "back in stock" in lines[1] and lines[2] == "", lines[:3])
ok("no emoji icons", not EMOJI.search(txt))
ok("text is HTML-escaped", "&lt;beta&gt; &amp; co" in txt and "<beta>" not in txt)
for lab in ("Product", "CPU", "RAM", "Storage", "Datacenter", "Stock", "Price", "Setup fee"):
    ok(f"field line: {lab}", any(l.startswith(f"<b>{lab}</b>: ") for l in lines), lab)
ok("product line = name | CPU", "<b>Product</b>: KS-LE-B &lt;beta&gt; &amp; co | Intel Xeon-E 2274G" in lines)
ok("datacenter = code (city)", "<b>Datacenter</b>: rbx (Roubaix)" in lines, [l for l in lines if "Datacenter" in l])
ok("availability human readable", "<b>Stock</b>: Within 1 hour - low stock" in lines)
ok("price per month + CNY, setup free", "<b>Price</b>: €25.99/month (≈ ¥190.82)" in lines and "<b>Setup fee</b>: Free" in lines)
ok("detected time in APP_TZ after a blank line", lines[-1] == "Detected at: 2026-11-21 23:04:05" and "" in lines[-4:], lines[-3:])
ok("English everywhere (no CJK)", not re.search(r"[\u4e00-\u9fff]", re.sub(r"≈ ¥", "", txt)), txt)
kb = p["reply_markup"]["inline_keyboard"]
ok("inline keyboard: order + product page buttons", len(kb) == 2 and kb[0][0]["text"] == "Order now (IE)" and kb[1][0]["text"] == "OVH product page", kb)
u = kb[0][0]["url"]
ok("order link = OVH express order, IE locale, plan + datacenter + region preselected",
   u.startswith("https://order.eu.ovhcloud.com/en-ie/express/#/express/review?products=~(~(planCode~'24sklb01~productId~'eco")
   and "label~'dedicated_datacenter~value~'rbx" in u and "label~'region~value~'europe" in u, u)
ok("product page link", kb[1][0]["url"] == "https://eco.ovhcloud.com/en-ie/")

# sold out -> edit the same message
calls.clear()
errs = notify.mark_sold("t:1", ts=1795273800.0)
ok("sold out: one editMessageText call", errs == [] and [c[1] for c in calls] == ["editMessageText"], calls)
e = calls[0][2]
ok("edit targets the original chat + message id", e["chat_id"] == "42" and e["message_id"] == p and False or (e["chat_id"] == "42" and e["message_id"] == 100), e)
ok("edited body struck through, title kept bold", e["text"].startswith("<b>OVH Restock</b>\n<s>") and "</s>" in e["text"], e["text"][:80])
ok("sold-out line appended with time", e["text"].endswith("<b>Sold out at 2026-11-21 23:10:00</b>"), e["text"][-60:])
ok("buttons removed on sold out", "reply_markup" not in e)
calls.clear()
ok("second mark_sold is a no-op", notify.mark_sold("t:1") == [] and calls == [])

# restock => brand-new message (new ref); global debounce still applies to the same dedupe key
calls.clear()
notify.send_card(1, info, ref="t:2", dedupe_key="d1")
ok("same dedupe key within the stock debounce -> not sent", calls == [])
ok("mark_sold without a sent card -> nothing", notify.mark_sold("t:2", wait=0.1) == [] and calls == [])
notify.send_card(1, info, ref="t:3", dedupe_key="d2")
ok("restock with a new key -> new message", [c[1] for c in calls] == ["sendMessage"] and calls[0][2]["text"] == txt)

# Chinese notification language
calls.clear()
notify.send_card(2, {**info, "name": "KS-LE-B"}, ref="z:1", dedupe_key="z1")
zt = calls[0][2]["text"]; print(zt)
ok("zh: title / fields in Chinese", zt.startswith("<b>OVH 补货通知</b>\nKS-LE-B 补货了（仅提醒）") and "<b>机房</b>：rbx（鲁贝 Roubaix）" in zt
   and "<b>库存</b>：1小时内有货 - 低库存" in zt and "<b>价格</b>：€25.99/月（≈ ¥190.82）" in zt, zt)
ok("zh: order button label", calls[0][2]["reply_markup"]["inline_keyboard"][0][0]["text"] == "立即下单（IE）")
calls.clear(); notify.mark_sold("z:1", ts=1795273800.0)
ok("zh: sold-out line", calls[0][2]["text"].endswith("<b>已被抢完：2026-11-21 23:10:00</b>"))

# Telegram error never leaks the token
notify.httpx.post = lambda url, json=None, timeout=None: R({"ok": False, "description": f"bad {TOKEN}"}, 400)
errs = notify.send_card(1, info, ref="t:9", dedupe_key="d9")
ok("Telegram error reported without the token", errs and TOKEN not in errs[0], errs)
notify.httpx.post = fake_post

# ---- worker: card sent on match, edited when the plan's stock goes, new card on restock
sent, sold = [], []
worker.notify_card = lambda uid, info, ref, dedupe_key: sent.append((ref, info))
worker.notify_sold = lambda ref, ts: sold.append(ref)
t = types.SimpleNamespace(user_id=1, endpoint="ovh-eu", subsidiary="IE", catalog_type="eco", duration="P1M", pricing_mode="default",
                          os_code="none_64.en", alert_cooldown=0, name="", notify_only=True)
r = worker.TaskRunner(7); r.t = t; r.sym, r.cur = "€", "EUR"
r.L = lambda *a, **k: None
feed = types.SimpleNamespace(data={"x": 1})
plan = {"planCode": "24sklb01", "key": "24sklb01", "label": "KS-LE-B", "cpu": "Xeon", "feed": feed, "dcs": ["rbx", "gra"],
        "obj": {"invoiceName": "KS-LE-B", "pricings": [], "configurations": []}, "addons": {}, "ctype": "eco", "card": None}
pick = {"dc": "rbx", "availability": "1H-low", "memory": "ram-32g-ecc-2666", "storage": "softraid-2x4000sa",
        "datacenters": [("rbx", "1H-low"), ("gra", "72H"), ("bhs", "1H-low")]}


def tick(picks, now):
    r._cards_sold([plan], picks, now)
    r._notify_only([plan], picks, now)
    time.sleep(0.15)


tick([(plan, pick)], 1000.0)
ok("worker: card sent when stock appears", len(sent) == 1 and plan["card"] == sent[0][0], sent)
i0 = sent[0][1]
ok("worker: card fields (codes, DCs within the task, order spec)", i0["dc"] == "rbx" and i0["others_codes"] == ["gra"] and i0["order"]["plan_code"] == "24sklb01"
   and i0["order"]["subsidiary"] == "IE" and str(i0["mode"]) == "Notify only", i0)
tick([(plan, pick)], 1010.0)
ok("worker: no second card while still in stock", len(sent) == 1 and not sold)
tick([], 1020.0)
ok("worker: sold out -> edit that card", sold == [sent[0][0]] and plan["card"] is None, sold)
tick([(plan, pick)], 1030.0)
ok("worker: restock -> a new card (new ref)", len(sent) == 2 and sent[1][0] != sent[0][0])
print("ALL PASS" if not fails else f"FAILED ({len(fails)}): {fails}")
sys.exit(1 if fails else 0)
