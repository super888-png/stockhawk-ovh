"""仅提醒 / notify-only mode, offline: Telegram readiness check, alert de-dup + cooldown state machine, bilingual texts,
and that notify-only tasks never load OVH keys.   python tests/notify_only_test.py"""
import os, re, sys, tempfile, time, types
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("SECRET_KEY", "unit-only-" + "k" * 40)
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="ovhsn-notify-")
os.environ.pop("DEFAULT_LANG", None)
CJK = re.compile(r"[\u4e00-\u9fff]")
fails = []


def ok(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    cond or fails.append(name)


from app.db import SessionLocal, auto_migrate  # noqa: E402
from app.models import NotifySetting, Task, User  # noqa: E402
from app.security import enc  # noqa: E402
from app.i18n import relang  # noqa: E402
from app import main, worker  # noqa: E402
auto_migrate()
with SessionLocal() as db:
    db.add(User(id=1, username="u", password_hash="x", is_admin=True, lang=""))
    db.commit()
    ok("tg_status: nothing configured -> blocked", main.tg_status(db, 1)[0] is False)
    db.add(NotifySetting(user_id=1, telegram_token_enc=enc("123456:" + "A" * 30), telegram_chat_id="", lang=""))
    db.commit()
    ok("tg_status: token without chat id -> blocked", main.tg_status(db, 1)[0] is False)
    ns = db.get(NotifySetting, 1); ns.telegram_chat_id = "42"; ns.notify_stock = False; db.commit()
    r = main.tg_status(db, 1)
    ok("tg_status: stock alerts switched off -> blocked with reason", r[0] is False and "off" in str(r[1]), str(r[1]))
    ns.notify_stock = True; db.commit()
    ok("tg_status: token + chat id + stock alerts -> ok", main.tg_status(db, 1) == (True, ""))

t = Task(user_id=1, plan_code="24ska01", notify_only=True, dry_run=True, auto_pay=False, alert_cooldown=10)
ok("pay_mode property", t.pay_mode == "notify" and Task(dry_run=True).pay_mode == "dry" and Task(dry_run=False, auto_pay=True).pay_mode == "pay")
ok("PAY_MODE_CN has notify (English)", main.PAY_MODE_CN["notify"] == "Notify only")

# ---- alert state machine
sent, logs = [], []
r = worker.TaskRunner(99)
r.t = t
r._send_card = lambda p, pick, mode, dedupe, extra=None: sent.append(("STOCK_FOUND", mode, p["label"], dedupe))
r.L = lambda marker, msg="", **kw: logs.append((marker, msg))
feed = types.SimpleNamespace(data={"x": 1})
plan = {"planCode": "24ska01", "label": "KS-A", "cpu": "Intel i7-6700k", "feed": feed, "dcs": ["gra", "rbx", "sbg"]}
pick = {"dc": "gra", "availability": "1H-low", "memory": "ram-32g-noecc-2133-24ska01", "storage": "softraid-1x480ssd-24ska01",
        "datacenters": [("gra", "1H-low"), ("rbx", "72H"), ("bhs", "1H-low")]}
now = 1_000_000.0
r._notify_only([plan], [(plan, pick)], now)
ok("alert when stock appears", len(sent) == 1 and sent[0][0] == "STOCK_FOUND", sent)
r._notify_only([plan], [(plan, pick)], now + 30)
ok("no repeat while still in stock", len(sent) == 1)
r._notify_only([plan], [], now + 60)
ok("sold out -> log, no alert", len(sent) == 1 and any("no longer" in str(m) for _, m in logs))
r._notify_only([plan], [(plan, pick)], now + 120)
ok("back within cooldown -> suppressed + logged once", len(sent) == 1 and any("cooldown" in str(m) for _, m in logs))
r._notify_only([plan], [(plan, pick)], now + 180)
ok("cooldown log not repeated", sum("cooldown" in str(m) for _, m in logs) == 1)
r._notify_only([plan], [(plan, pick)], now + 601)
ok("still in stock after cooldown -> alert again", len(sent) == 2)
ok("unique dedupe keys (task cooldown replaces global debounce)", sent[0][3] != sent[1][3])
t.alert_cooldown = 0
r._notify_only([plan], [], now + 700); r._notify_only([plan], [(plan, pick)], now + 701)
ok("cooldown 0 -> alert on every restock", len(sent) == 3)
waiting = {**plan, "planCode": "X", "feed": types.SimpleNamespace(data=None)}
r._notify_only([waiting], [], now + 800)
ok("plan without data yet is ignored", len(sent) == 3)

mode = sent[0][1]
ok("alert = restock card with the notify-only mode line, bilingual", str(relang(mode, "en")) == "Notify only" and str(relang(mode, "zh")) == "仅提醒", mode)
ok("card is for the matching plan", sent[0][2] == "KS-A")

# ---- notify-only never loads OVH keys
src = open(os.path.join(ROOT, "app", "worker.py"), encoding="utf-8").read()
ok("worker skips load_creds for notify-only", "creds = None if t.notify_only else load_creds(" in src)
ok("worker returns before any cart/order code for notify-only",
   src.index("if t.notify_only:\n                    self._notify_only(") < src.index("p, pick = picks[0]"))
print("ALL PASS" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
