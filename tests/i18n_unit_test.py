"""i18n unit checks (no server): Telegram/webhook texts follow the notification language, worker texts can be
re-rendered, CLI is English by default.   python tests/i18n_unit_test.py"""
import os, re, subprocess, sys, tempfile
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("SECRET_KEY", "unit-only-" + "k" * 40)
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="ovhsn-i18n-")
os.environ.pop("DEFAULT_LANG", None)
CJK = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")
fails = []


def ok(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    cond or fails.append(name)


from app import i18n  # noqa: E402
from app.i18n import T, lang_scope, ljoin, relang  # noqa: E402
ok("default language is English", i18n.get_lang() == "en")
ok("T() translates in English", T("控制台") == "Dashboard")
with lang_scope("zh"):
    ok("T() keeps Chinese in zh", T("控制台") == "控制台")
    msg = T("已轮询 {0} 次 · {1} · ", 3, T("有匹配库存")) + T("任务 #{0}", 7)
    joined = ljoin("\n", [T("付款方式：自动扣款中…"), "RISE-1", T("首期含税 {0}{1}", "€140.20", "")])
ok("re-render a zh worker message in English", relang(msg, "en") == "Polls: 3 · matching stock · Task #7", relang(msg, "en"))
ok("re-render joined lines in English", not CJK.search(relang(joined, "en")), relang(joined, "en"))

# notifications: capture what would be POSTed to Telegram / webhook
from app.db import SessionLocal, auto_migrate  # noqa: E402
from app.models import NotifySetting, User  # noqa: E402
from app.security import enc  # noqa: E402
from app import notify  # noqa: E402
auto_migrate()
with SessionLocal() as db:
    db.add(User(id=1, username="u", password_hash="x", is_admin=True, lang=""))
    db.commit()
    db.add(NotifySetting(user_id=1, telegram_token_enc=enc("123456:" + "A" * 30), telegram_chat_id="42", lang=""))
    db.commit()
sent = []


class R:
    status_code = 200
    def json(self): return {}


notify.httpx.post = lambda url, json=None, timeout=None: sent.append(json["text"]) or R()


def set_langs(user_lang, notify_lang):
    with SessionLocal() as db:
        db.get(User, 1).lang = user_lang
        db.get(NotifySetting, 1).lang = notify_lang
        db.commit()


for cat in notify.CATEGORIES:
    sent.clear(); set_langs("", ""); notify.send_test(1, cat)
    ok(f"telegram '{cat}' sample is English by default", sent and not CJK.search(sent[0]), sent[:1])
sent.clear(); set_langs("en", "zh"); notify.send_test(1, "order")
ok("notification language zh overrides an English panel", sent and "订单" in sent[0], sent[:1])
sent.clear(); set_langs("zh", ""); notify.send_test(1, "paid")
ok("notifications follow the panel language when not set", sent and "已付款" in sent[0], sent[:1])
with lang_scope("zh"):   # a worker running in Chinese, telegram set to English
    m = T("{0}\n首期含税 {1}{2}\n自动扣款成功，等待 OVH 交付（交付后会再通知你）", T("任务 #{0}", 3), "€1.00", "")
sent.clear(); set_langs("zh", "en"); notify.send(1, "ORDER_PAID", m, title=T("订单已付款"), category="paid", force=True)
ok("worker text built in zh is sent in English", sent and not CJK.search(sent[0]), sent[:1])

# CLI help in English by default
r = subprocess.run([sys.executable, "-m", "app.cli", "--help"], cwd=ROOT, capture_output=True, text=True,
                   env={**os.environ})
ok("CLI --help is English", r.returncode == 0 and not CJK.search(r.stdout), r.stdout[:200] + r.stderr[-200:])
print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAIL: {fails}"))
sys.exit(1 if fails else 0)
