"""OVH order / service / payment enums are translated in the reader's language, also when fetched in worker threads.
python tests/order_status_i18n_test.py"""
import os, re, sys, tempfile
from concurrent.futures import ThreadPoolExecutor
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("SECRET_KEY", "unit-only-" + "k" * 40)
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="ovhsn-ost-")
from app import account as A, tracker  # noqa: E402
from app.i18n import T, lang_scope, relang  # noqa: E402
fails = []


def ok(n, c, x=""):
    print(("PASS " if c else "FAIL ") + n + ("" if c else f"  ({x})")); c or fails.append(n)


STATUSES = ["notPaid", "delivered", "delivering", "checking", "cancelled", "cancelling", "documentsRequested", "unknown", "expired"]


class Fake:
    def get(self, path, **kw):
        if path == "/me/order":
            return list(range(1, len(STATUSES) + 1))
        m = re.match(r"/me/order/(\d+)(/.*)?$", path)
        oid, sub = int(m.group(1)), m.group(2) or ""
        if sub == "/status":
            return STATUSES[oid - 1]
        if sub == "/details":
            return []
        if sub == "/payment":
            return {"paymentType": "debtAccount", "paymentDate": "2026-11-20T10:00:00+01:00"}
        return {"date": f"2026-11-{10 + oid:02d}T10:00:00+01:00", "priceWithTax": {"value": 10, "currencyCode": "EUR", "text": "€10.00"}}
    def __getattr__(self, k):
        raise AttributeError(k)


ZH = {"notPaid": "未付款", "delivered": "已交付", "delivering": "交付中", "checking": "核查中", "cancelled": "已取消",
      "cancelling": "取消中", "documentsRequested": "需提交资料", "unknown": "未知", "expired": "已过期"}
EN = {"notPaid": "Unpaid", "delivered": "Delivered", "delivering": "Delivering", "cancelled": "Cancelled", "expired": "Expired"}
for lang in ("zh", "en"):
    with lang_scope(lang), ThreadPoolExecutor(max_workers=4) as pool:   # same as fetch_account: rows built in pool threads
        rows = {r["status"]: r["statusCn"] for r in A.fetch_orders(Fake(), pool)["rows"]}
    if lang == "zh":
        for k, v in ZH.items():
            ok(f"zh order status {k} -> {v}", rows.get(k) == v, rows.get(k))
    else:
        for k, v in EN.items():
            ok(f"en order status {k} -> {v}", rows.get(k) == v, rows.get(k))
with lang_scope("zh"):
    ok("unknown enum value falls back to raw", A.ORDER_STATUS_CN.get("somethingNew", "somethingNew") == "somethingNew")
    ok("zh payment debtAccount -> 欠款账户", A.PAY_CN.get("debtAccount") == "欠款账户")
    ok("zh service status suspended", A.SERVICE_STATUS_CN.get("suspended") == "已暂停")
with lang_scope("en"):
    ok("en payment debtAccount -> Debt account", A.PAY_CN.get("debtAccount") == "Debt account")
# Telegram: text built in the panel language is re-rendered in the notification language (status included)
with lang_scope("en"):
    msg = T("OVH 状态：{0}", tracker.STATUS_CN.get("delivering", "delivering"))
ok("Telegram text re-renders the OVH status in Chinese", str(relang(msg, "zh")) == "OVH 状态：交付中", relang(msg, "zh"))
with lang_scope("zh"):
    msg = T("OVH 状态：{0}", tracker.STATUS_CN.get("notPaid", "notPaid"))
ok("…and in English", str(relang(msg, "en")) == "OVH status: Unpaid", relang(msg, "en"))
# the browser-side maps cover the same enums
js = open(os.path.join(ROOT, "app/static/account.js"), encoding="utf-8").read()
for k in ZH:
    ok(f"account.js ORDER_CN has {k}", re.search(rf"\b{k}: T\('{ZH[k]}'\)", js) is not None)
ok("account.js PAY_CN has debtAccount", "debtAccount: T('欠款账户')" in js)
print("ALL PASS" if not fails else f"FAILED ({len(fails)}): {fails}")
sys.exit(1 if fails else 0)
