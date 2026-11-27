"""FastAPI app: StockHawk for OVH, multi-user OVH stock watcher / auto-order web UI."""
from __future__ import annotations

import asyncio
import json
import datetime as dt
import logging
import re
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import config
from . import ovh_engine as E
from .catalog import cur_symbol, get_catalog, plan_detail, plan_summaries
from .db import Base, SessionLocal, auto_migrate, engine, get_db
from .models import AppSetting, AuditLog, Credential, NotifySetting, Order, Task, TaskLog, User, task_plans, utcnow
from .notify import CATEGORIES, CATEGORY_HELP, send_test as notify_send_test
from .netguard import check_url
from .tracker import tracker, check_order
from .poller import fetch_once, hub
from .proxy import CloudflareProxy
from .security import SESSION_SECRET, USERNAME_RE, check_password, csrf_ok, dec, enc, hash_password, mask, new_csrf, password_problem
from .worker import manager
from . import account as A
from . import fx as FX
from . import labels as LB
from . import pricing as P
from . import dcmap as DCM
from .i18n import T, TDict, get_lang, js_catalog, norm, set_lang, tr

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
TZ = ZoneInfo(config.APP_TZ)
BASE_DIR = __file__.rsplit("/", 1)[0]


@asynccontextmanager
async def lifespan(app: FastAPI):
    auto_migrate()
    _secure_files()
    await manager.resume_all()
    tracker.start()
    yield
    await tracker.stop()
    await manager.shutdown()
    await hub.close()


def _secure_files() -> None:
    """SQLite database, WAL files and the data dir readable by the app user only."""
    import os
    try:
        os.chmod(config.DATA_DIR, 0o700)
        for name in os.listdir(config.DATA_DIR):
            path = os.path.join(config.DATA_DIR, name)
            if os.path.isfile(path) and (name.startswith("app.db") or name.endswith(".json")):
                os.chmod(path, 0o600)
    except OSError:
        pass


app = FastAPI(title="StockHawk for OVH", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, session_cookie="ovhsn_session",
                   max_age=14 * 86400, same_site="lax", https_only=config.COOKIE_SECURE)
app.mount("/static", StaticFiles(directory=f"{BASE_DIR}/static"), name="static")
templates = Jinja2Templates(directory=f"{BASE_DIR}/templates")


def _aware(v):
    if isinstance(v, (int, float)):
        return dt.datetime.fromtimestamp(v, dt.timezone.utc)
    return v.replace(tzinfo=dt.timezone.utc) if v.tzinfo is None else v


def localtime(v):
    if not v:
        return "-"
    return _aware(v).astimezone(TZ).strftime("%m-%d %H:%M:%S")


def iso(v):
    return _aware(v).isoformat(timespec="seconds") if v else None


def time_tag(v):
    """<time> that app.js re-renders in the BROWSER's time zone (server zone text as fallback)."""
    if not v:
        return "-"
    return Markup('<time datetime="{}" data-lt>{}</time>').format(iso(v), localtime(v))


templates.env.filters["lt"] = time_tag
def order_money(o) -> dict:
    """Price of an Order row in its catalog currency plus the CNY line (never blocks on the FX API)."""
    try:
        d = json.loads(o.detail or "{}")
    except Exception:
        d = {}
    code = FX.currency_for(d.get("subsidiary"), code=d.get("currency"))
    v = o.price_with_tax
    return {"text": f"{cur_symbol(code)}{v:.2f}" if v is not None else "-", "cny": FX.cny(v, code), "code": code}


templates.env.globals["order_money"] = order_money
templates.env.tests["search"] = lambda v, pat: re.search(pat, str(v or "")) is not None
templates.env.filters["iso"] = iso
templates.env.globals.update(_=T, tr=tr, LANG=get_lang, I18N_JS=js_catalog)
templates.env.globals.update(DC_LABEL=E.DC_LABEL, ENDPOINTS=E.ENDPOINTS, SUBSIDIARIES=E.SUBSIDIARIES,
                             CATALOG_TYPES=E.CATALOG_TYPES, MIN_POLL=config.MIN_POLL_INTERVAL, TZNAME=config.APP_TZ)

STATUS_CN = TDict({"stopped": "已停止", "running": "运行中", "done": "已完成", "error": "出错",
             "pending_confirm": "待人工确认", "starting": "启动中", "checkout": "结账中"})
templates.env.globals["STATUS_CN"] = STATUS_CN
templates.env.globals["VAT_TABLE"] = P.VAT
PAY_MODE_CN = TDict({"notify": "仅提醒", "dry": "只预览", "order": "只下单不付款", "pay": "下单并自动付款"})
templates.env.globals["PAY_MODE_CN"] = PAY_MODE_CN


def tg_status(db, user_id: int) -> tuple[bool, str]:
    """Can a 仅提醒 (notify-only) task reach the owner? -> (ok, reason). Needs a Telegram bot token + chat id and
    the 有货通知 category switched on."""
    ns = db.get(NotifySetting, user_id)
    if not ns or not (ns.telegram_chat_id or "").strip() or not dec(ns.telegram_token_enc or ""):
        return False, tr("还没有配置 Telegram 通知")
    if ns.notify_stock is False:
        return False, tr("「有货通知」已关闭")
    return True, ""


UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def _same_origin(request: Request) -> bool:
    """Defence in depth next to the CSRF token: a browser POST must come from our own origin."""
    src = request.headers.get("origin") or request.headers.get("referer")
    if not src or src == "null":
        return src is None   # no header (non-browser client): rely on the CSRF token; "null" origin: refuse
    host = request.headers.get("x-forwarded-host") if config.TRUST_PROXY else None
    host = (host or request.headers.get("host") or "").lower()
    parts = urlsplit(src)
    if parts.netloc.lower() == host:
        return True
    return f"{parts.scheme}://{parts.netloc}".lower() in config.ALLOWED_ORIGINS


LANG_COOKIE = "ovhsn_lang"


def lang_cookie(resp, lang: str) -> None:
    resp.set_cookie(LANG_COOKIE, norm(lang), max_age=365 * 86400, samesite="lax", httponly=True,
                    secure=config.COOKIE_SECURE, path="/")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    set_lang(request.cookies.get(LANG_COOKIE))   # English unless this browser/user picked Chinese (never Accept-Language)
    if request.method in UNSAFE and not _same_origin(request):
        return JSONResponse({"error": T("跨站请求被拒绝")}, status_code=403)
    nonce = secrets.token_urlsafe(16)
    request.state.csp_nonce = nonce
    resp = await call_next(request)
    h = resp.headers
    h.setdefault("Content-Security-Policy",
                 f"default-src 'self'; script-src 'self' 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; "
                 "img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
                 "frame-ancestors 'none'; form-action 'self'")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "same-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    if config.COOKIE_SECURE:
        h.setdefault("Strict-Transport-Security", "max-age=31536000")
    if not request.url.path.startswith("/static/"):
        h.setdefault("Cache-Control", "no-store")
    return resp


# ---------------------------------------------------------------- rate limits / audit
class Limiter:
    def __init__(self):
        self.hits: dict[str, deque] = {}

    def check(self, key: str, limit: int, window: float, record: bool = True) -> bool:
        """True = allowed. `record=False` only peeks (e.g. login: count failures, not attempts)."""
        now = time.time()
        q = self.hits.setdefault(key, deque())
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        if record:
            q.append(now)
        if len(self.hits) > 20000:
            for k in [k for k, v in self.hits.items() if not v or now - v[-1] > 3600]:
                self.hits.pop(k, None)
        return True

    def add(self, key: str) -> None:
        self.hits.setdefault(key, deque()).append(time.time())


limiter = Limiter()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def audit(request: Request | None, user_id: int | None, action: str, detail: str = "") -> None:
    from .db import SessionLocal
    try:
        with SessionLocal() as db:
            db.add(AuditLog(user_id=user_id, action=action[:48], detail=detail[:500],
                            ip=client_ip(request) if request else ""))
            db.commit()
            if secrets.randbelow(200) == 0:   # keep the table bounded
                cut = db.scalar(select(AuditLog.id).order_by(AuditLog.id.desc()).offset(20000).limit(1))
                if cut:
                    db.execute(delete(AuditLog).where(AuditLog.id <= cut))
                    db.commit()
    except Exception:
        logging.getLogger("audit").exception("audit write failed")


# ---------------------------------------------------------------- helpers
def get_setting(db: Session, key: str, default: str = "") -> str:
    s = db.get(AppSetting, key)
    return s.value if s else default


def put_setting(db: Session, key: str, value: str) -> None:
    s = db.get(AppSetting, key)
    if s:
        s.value = value
    else:
        db.add(AppSetting(key=key, value=value))
    db.commit()


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = new_csrf()
        request.session["csrf"] = tok
    return tok


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    u = db.get(User, uid)
    now = int(time.time())
    if (not u or not u.is_active or request.session.get("ep", 0) != (u.session_epoch or 0)
            or now - int(request.session.get("seen", now)) > SESSION_IDLE):
        request.session.clear()
        return None
    if now - int(request.session.get("seen", 0)) > 300:   # sliding idle timeout, re-signed at most every 5 min
        request.session["seen"] = now
    return u


SESSION_IDLE = 3 * 86400        # log out after 3 days without activity (cookie itself expires after 14 days)


def start_session(request: Request, u: User) -> None:
    """Fresh session on every login (no fixation: the whole signed cookie is replaced) bound to the user's
    session epoch, so a password change or "log out everywhere" invalidates all older cookies."""
    request.session.clear()
    request.session.update(uid=u.id, csrf=new_csrf(), ep=u.session_epoch or 0, seen=int(time.time()))


class LoginRequired(Exception):
    pass


@app.exception_handler(LoginRequired)
async def _login_redirect(request: Request, exc: LoginRequired):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": T("未登录")}, status_code=401)
    return RedirectResponse("/login", status_code=303)


def require_user(user: User | None = Depends(current_user)) -> User:
    if not user:
        raise LoginRequired()
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, T("需要管理员权限"))
    return user


async def form_csrf(request: Request) -> dict:
    form = await request.form()
    if not csrf_ok(request.session.get("csrf"), form.get("csrf")):
        raise HTTPException(400, T("CSRF 校验失败，请刷新页面重试"))
    return form


def render(request: Request, name: str, user: User | None = None, **ctx):
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, name, {"user": user, "csrf": csrf_token(request), "flash": flash,
                                                      "csp_nonce": getattr(request.state, "csp_nonce", ""), **ctx})


def flash(request: Request, msg: str, kind: str = "ok"):
    request.session["flash"] = {"msg": msg, "kind": kind}


def redirect(url: str):
    return RedirectResponse(url, status_code=303)


DUMMY_HASH = hash_password(secrets.token_urlsafe(12))


# ---------------------------------------------------------------- auth
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    if user:
        return redirect("/")
    has_users = db.scalar(select(func.count(User.id))) > 0   # single-owner: the account is created with app.cli
    return render(request, "login.html", None, has_users=has_users)


@app.post("/login")
async def login(request: Request, db: Session = Depends(get_db)):
    form = await form_csrf(request)
    ip = client_ip(request)
    name = (form.get("username") or "").strip()[:64]
    if not limiter.check(f"lf-ip:{ip}", 10, 600, record=False) or not limiter.check(f"lf-u:{name.lower()}", 8, 900, record=False):
        flash(request, T("失败次数过多，请 15 分钟后再试"), "err")
        return redirect("/login")
    u = db.scalar(select(User).where(User.username == name))
    ok = check_password(form.get("password") or "", u.password_hash if u else DUMMY_HASH)  # same cost either way
    if not u or not ok or not u.is_active:
        limiter.add(f"lf-ip:{ip}")
        limiter.add(f"lf-u:{name.lower()}")
        audit(request, u.id if u else None, "login_fail", f"username={name}")
        flash(request, T("用户名或密码错误（或账号已被停用）"), "err")
        return redirect("/login")
    start_session(request, u)
    u.last_login_at, u.last_login_ip = utcnow(), ip
    if not u.lang and request.cookies.get(LANG_COOKIE):   # language picked on the login page sticks to the account
        u.lang = norm(request.cookies.get(LANG_COOKIE))
    db.commit()
    audit(request, u.id, "login")
    resp = redirect("/")
    if u.lang:
        lang_cookie(resp, u.lang)
    return resp


@app.post("/lang")
async def set_language(request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    """Switch UI language (en | zh). Stored per user (User.lang) and in a cookie for the login page."""
    form = await form_csrf(request)
    lang = norm(form.get("lang"))
    if user:
        u = db.get(User, user.id)
        u.lang = lang
        db.commit()
    nxt = form.get("next") or "/"
    if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt:
        nxt = "/"
    resp = redirect(nxt)
    lang_cookie(resp, lang)
    return resp


@app.post("/logout")
async def logout(request: Request):
    await form_csrf(request)
    audit(request, request.session.get("uid"), "logout")
    request.session.clear()
    return redirect("/login")


# ---------------------------------------------------------------- account & security
@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    logs = list(db.scalars(select(AuditLog).where(AuditLog.user_id == user.id).order_by(AuditLog.id.desc()).limit(50)))
    return render(request, "account.html", user, logs=logs, ACTION_CN=ACTION_CN)


@app.post("/account/password")
async def account_password(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    form = await form_csrf(request)
    if not limiter.check(f"pw:{user.id}", 5, 900):
        flash(request, T("尝试过于频繁，请稍后再试"), "err")
        return redirect("/account")
    if not check_password(form.get("old") or "", user.password_hash):
        audit(request, user.id, "password_fail")
        flash(request, T("当前密码不正确"), "err")
        return redirect("/account")
    pw = form.get("new") or ""
    problem = password_problem(pw, user.username)
    if problem or pw != (form.get("new2") or ""):
        flash(request, problem or T("两次输入的新密码不一致"), "err")
        return redirect("/account")
    user.password_hash = hash_password(pw)
    user.session_epoch = (user.session_epoch or 0) + 1
    db.commit()
    start_session(request, user)
    audit(request, user.id, "password_change")
    flash(request, T("密码已修改，其他设备上的登录已全部失效"))
    return redirect("/account")


@app.post("/account/logout-all")
async def account_logout_all(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    await form_csrf(request)
    user.session_epoch = (user.session_epoch or 0) + 1
    db.commit()
    audit(request, user.id, "logout_all")
    request.session.clear()
    flash(request, T("已退出所有设备，请重新登录"))
    return redirect("/login")


ACTION_CN = TDict({"login": "登录", "login_fail": "登录失败", "logout": "退出登录", "logout_all": "退出所有设备", "register": "注册", "owner_created": "创建所有者（命令行）",
             "password_reset": "重置密码（命令行）",
             "password_change": "修改密码", "password_fail": "修改密码失败（旧密码错误）", "keys_save": "保存 OVH 密钥",
             "keys_delete": "删除 OVH 密钥", "keys_verify": "验证 OVH 密钥", "ck_request": "申请 Consumer Key",
             "notify_save": "修改通知设置", "task_create": "新建任务", "task_update": "修改任务", "task_delete": "删除任务",
             "task_start": "启动任务", "task_stop": "停止任务", "task_clone": "复制任务", "admin_user": "管理员启用/停用用户",
             "admin_settings": "管理员修改设置", "order_check": "手动检查订单交付"})
templates.env.globals["ACTION_CN"] = ACTION_CN


# ---------------------------------------------------------------- human-readable plan / addon names
def nice_plans(t: Task) -> list[dict]:
    """task_plans + model name, CPU and readable RAM/disk/bandwidth (cached catalog only; raw code fallback)."""
    try:
        cfg = json.loads(t.plan_configs or "{}")
    except Exception:
        cfg = {}
    out = []
    for p in task_plans(t):
        ctype = p.get("type") or (cfg.get(p["planCode"]) or {}).get("type") or t.catalog_type
        info = LB.plan_info(t.endpoint, t.subsidiary, p["planCode"], ctype)
        name = info["name"] if info["known"] else (p.get("label") or p["planCode"])
        out.append({**p, "name": name, "cpu": info["cpu"], "cores": info["cores"],
                    "title": name + (f" · {info['cpu']}" if info["cpu"] else ""),
                    "memL": LB.ram_label(p["memory"]), "stoL": LB.disk_label(p["storage"]),
                    "bwL": LB.bw_label(p["bandwidth"]),
                    # per-item datacenters (v1.9); legacy rows fall back to the task-wide list
                    "dcL": " → ".join(E.dc_label(d) for d in (p["dcs"] if p.get("dcs") is not None else
                                                              [d for d in (t.datacenters or "").split(",") if d]))})
    return out


def order_plan(o) -> dict:
    """Model name/CPU and readable config for an order row (uses the task's region when known)."""
    info = LB.plan_info(getattr(o, "endpoint", None) or "ovh-eu", getattr(o, "subsidiary", None) or "IE", o.plan_code or "")
    return {"name": info["name"] if info["known"] else (o.plan_code or ""), "cpu": info["cpu"], "known": info["known"],
            "memL": LB.ram_label(o.memory or ""), "stoL": LB.disk_label(o.storage or "")}


templates.env.globals.update(addon_label=LB.addon_label, ram_label=LB.ram_label, disk_label=LB.disk_label,
                             bw_label=LB.bw_label, order_plan=order_plan)


# ---------------------------------------------------------------- dashboard
def task_view(t: Task) -> dict:
    st = manager.state(t.id)
    return {"t": t, "live": st, "running": st is not None, "plans": nice_plans(t)}


def account_ctx(db: Session, user: User) -> tuple[list[str], dict]:
    """Endpoints with complete saved keys (for the read-only account views) + re-authorize links."""
    eps = [c.endpoint for c in db.scalars(select(Credential).where(Credential.user_id == user.id))
           if c.endpoint in E.ENDPOINTS and c.app_key_enc and c.app_secret_enc and c.consumer_key_enc]
    eps.sort(key=list(E.ENDPOINTS).index)
    guide = {ep: {"name": E.ENDPOINT_HELP[ep][0], "token": E.create_token_url(ep)} for ep in eps}
    return eps, guide


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    tasks = list(db.scalars(select(Task).where(Task.user_id == user.id).order_by(Task.id.desc())))
    creds = {c.endpoint for c in db.scalars(select(Credential).where(Credential.user_id == user.id))
             if c.consumer_key_enc}
    orders = list(db.scalars(select(Order).where(Order.user_id == user.id).order_by(Order.id.desc()).limit(10)))
    views = [task_view(t) for t in tasks]
    n_orders = db.scalar(select(func.count(Order.id)).where(Order.user_id == user.id, Order.kind == "order")) or 0
    n_prev = db.scalar(select(func.count(Order.id)).where(Order.user_id == user.id, Order.kind == "preview")) or 0
    stats = {"running": sum(1 for v in views if v["running"]), "total": len(views),
             "hits": sum(1 for v in views if v["live"] and v["live"].get("in_stock")),
             "orders": n_orders, "previews": n_prev, "feeds": len(hub.feeds), "requests": hub.requests}
    ns = db.get(NotifySetting, user.id)
    has_notify = bool(ns and ((ns.telegram_token_enc and ns.telegram_chat_id) or ns.webhook_url_enc))
    eps, guide = account_ctx(db, user)
    return render(request, "dashboard.html", user, tasks=views, creds=creds, orders=orders, stats=stats, has_notify=has_notify,
                  account_eps=eps, guide=guide)


@app.get("/api/tasks/status")
def api_status(user: User = Depends(require_user), db: Session = Depends(get_db)):
    out = []
    for t in db.scalars(select(Task).where(Task.user_id == user.id)):
        st = manager.state(t.id) or {}
        out.append({"id": t.id, "status": t.status, "status_cn": STATUS_CN.get(t.status, t.status),
                    "status_text": t.status_text, "running": t.id in manager.runners, "orders_done": t.orders_done,
                    "polls": st.get("polls", 0), "last_poll": localtime(st.get("last_poll")) if st.get("last_poll") else "-",
                    "in_stock": st.get("in_stock", False), "last_ms": st.get("last_ms", 0)})
    return out


# ---------------------------------------------------------------- OVH keys
@app.get("/keys", response_class=HTMLResponse)
def keys_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    rows = {c.endpoint: c for c in db.scalars(select(Credential).where(Credential.user_id == user.id))}
    view = {}
    for ep in E.ENDPOINTS:
        c = rows.get(ep)
        view[ep] = None if not c else {
            "app_key": mask(dec(c.app_key_enc)), "app_secret": T("已保存（隐藏）") if c.app_secret_enc else "",
            "consumer_key": mask(dec(c.consumer_key_enc)), "pending": bool(c.pending_ck_enc),
            "nichandle": c.nichandle, "verified_at": c.verified_at}
    pending_url = request.session.get("ck_validation_url")
    guide = {ep: {"name": E.ENDPOINT_HELP[ep][0], "who": E.ENDPOINT_HELP[ep][1], "token": E.create_token_url(ep),
                  "app": E.create_app_url(ep)} for ep in E.ENDPOINTS}
    return render(request, "keys.html", user, creds=view, pending_url=pending_url, guide=guide,
                  rules=E.ACCESS_RULES, pending_ep=request.session.get("ck_endpoint"))


@app.get("/api/keys")
def api_keys(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Masked view only. Secrets are never returned after saving."""
    return [{"endpoint": c.endpoint, "app_key": mask(dec(c.app_key_enc)),
             "app_secret": "********" if c.app_secret_enc else "", "consumer_key": mask(dec(c.consumer_key_enc)),
             "nichandle": c.nichandle} for c in db.scalars(select(Credential).where(Credential.user_id == user.id))]


def _get_cred(db, user, ep, create=False) -> Credential | None:
    c = db.scalar(select(Credential).where(Credential.user_id == user.id, Credential.endpoint == ep))
    if not c and create:
        c = Credential(user_id=user.id, endpoint=ep)
        db.add(c)
    return c


@app.post("/keys/save")
async def keys_save(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    form = await form_csrf(request)
    ep = form.get("endpoint")
    if ep not in E.ENDPOINTS:
        raise HTTPException(400)
    c = _get_cred(db, user, ep, create=True)
    ak, asec, ck = (form.get(k, "").strip() for k in ("app_key", "app_secret", "consumer_key"))
    if ak:
        c.app_key_enc = enc(ak)
    if asec:
        c.app_secret_enc = enc(asec)
    if ck:
        c.consumer_key_enc, c.pending_ck_enc, c.verified_at, c.nichandle = enc(ck), "", None, ""
    db.commit()
    A.cache.drop_user(user.id)
    audit(request, user.id, "keys_save", ep)
    flash(request, T("{0} 密钥已加密保存（留空的字段保持不变）", ep))
    return redirect("/keys")


@app.post("/keys/ck-request")
async def keys_ck_request(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Generate a consumer key: POST /auth/credential with the needed access rules."""
    form = await form_csrf(request)
    ep = form.get("endpoint")
    if ep not in E.ENDPOINTS:
        raise HTTPException(400)
    if not limiter.check(f"ck:{user.id}", 10, 600):
        flash(request, T("申请过于频繁，请 10 分钟后再试"), "err")
        return redirect("/keys")
    audit(request, user.id, "ck_request", ep)
    c = _get_cred(db, user, ep, create=True)
    ak, asec = (form.get("app_key") or "").strip(), (form.get("app_secret") or "").strip()
    if ak:
        c.app_key_enc = enc(ak)
    if asec:
        c.app_secret_enc = enc(asec)
    db.commit()
    ak, asec = dec(c.app_key_enc), dec(c.app_secret_enc)
    if not ak or not asec:
        flash(request, T("请先填写 Application Key 和 Application Secret"), "err")
        return redirect("/keys")
    try:
        res = await asyncio.to_thread(E.request_consumer_key, ep, ak, asec, str(request.url_for("keys_page")))
    except Exception as e:
        flash(request, T("OVH 拒绝了申请: {0}: {1}", type(e).__name__, e), "err")
        return redirect("/keys")
    c.pending_ck_enc = enc(res["consumerKey"])
    db.commit()
    request.session["ck_validation_url"] = res.get("validationUrl")
    request.session["ck_endpoint"] = ep
    flash(request, T("已向 OVH 申请 Consumer Key。请点击下方链接登录 OVH 并授权，然后回来点「我已授权，验证并保存」。"))
    return redirect("/keys")


@app.post("/keys/verify")
async def keys_verify(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    form = await form_csrf(request)
    ep = form.get("endpoint")
    c = _get_cred(db, user, ep)
    if not c:
        flash(request, T("没有这个区域的密钥"), "err")
        return redirect("/keys")
    ck = dec(c.pending_ck_enc) or dec(c.consumer_key_enc)
    try:
        client = E.make_client(ep, dec(c.app_key_enc), dec(c.app_secret_enc), ck, timeout=(5, 20))
        info = await asyncio.to_thread(E.account_info, client)
    except Exception as e:
        flash(request, T("验证失败（如刚申请，请确认已在 OVH 页面授权）: {0}: {1}", type(e).__name__, e), "err")
        return redirect("/keys")
    c.consumer_key_enc, c.pending_ck_enc = enc(ck), ""
    c.nichandle, c.verified_at = info.get("nichandle") or "", utcnow()
    db.commit()
    A.cache.drop_user(user.id)
    audit(request, user.id, "keys_verify", ep)
    request.session.pop("ck_validation_url", None)
    pm = info.get("default_payment")
    flash(request, T("验证成功：账号 {0}（子公司 {1}，币种 {2}）", info.get('nichandle'), info.get('subsidiary'), info.get('currency'))
                   + (T("，默认支付方式 {0}", A.PAY_CN.get(pm, pm)) if pm else T("。注意：没有默认支付方式，自动扣款会失败，请先在 OVH 控制台绑定")))
    return redirect("/keys")


@app.post("/keys/delete")
async def keys_delete(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    form = await form_csrf(request)
    c = _get_cred(db, user, form.get("endpoint"))
    if c:
        db.delete(c)
        db.commit()
        audit(request, user.id, "keys_delete", c.endpoint)
    A.cache.drop_user(user.id)
    flash(request, T("已删除该区域的密钥"))
    return redirect("/keys")


# ---------------------------------------------------------------- notifications
@app.get("/notify", response_class=HTMLResponse)
def notify_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    ns = db.get(NotifySetting, user.id)
    view = {"tg_token": mask(dec(ns.telegram_token_enc)) if ns else "", "tg_chat": ns.telegram_chat_id if ns else "",
            "webhook": _mask_url(dec(ns.webhook_url_enc)) if ns else "",
            "cats": {k: (getattr(ns, f"notify_{k}") if ns else True) for k in CATEGORIES},
            "debounce": (ns.stock_debounce if ns and ns.stock_debounce is not None else 600) // 60,
            "lang": (ns.lang or "") if ns else ""}
    return render(request, "notify.html", user, n=view, CATEGORIES=CATEGORIES, CATEGORY_HELP=CATEGORY_HELP)


def _mask_url(u: str) -> str:
    if not u:
        return ""
    p = urlsplit(u)
    return f"{p.scheme}://{p.hostname}{'/…' if p.path not in ('', '/') or p.query else ''}"


@app.post("/notify/save")
async def notify_save(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    form = await form_csrf(request)
    ns = db.get(NotifySetting, user.id) or NotifySetting(user_id=user.id)
    db.add(ns)
    tok, hook = (form.get("tg_token") or "").strip(), (form.get("webhook") or "").strip()
    if tok:
        if not re.fullmatch(r"\d{5,16}:[\w-]{20,80}", tok):
            flash(request, T("Telegram Bot Token 格式不对（应形如 123456:ABC-DEF…）"), "err")
            return redirect("/notify")
        ns.telegram_token_enc = enc(tok)
    if hook:
        err = await asyncio.to_thread(check_url, hook)
        if err or len(hook) > 1000:
            flash(request, T("Webhook 地址不可用：{0}（只允许公网 http(s) 地址）", err or T("太长")), "err")
            return redirect("/notify")
        ns.webhook_url_enc = enc(hook)
    if form.get("clear_tg"):
        ns.telegram_token_enc = ""
    if form.get("clear_hook"):
        ns.webhook_url_enc = ""
    chat = (form.get("tg_chat") or "").strip()
    if chat and not re.fullmatch(r"-?\d{1,20}|@[A-Za-z0-9_]{5,32}", chat):
        flash(request, T("Chat ID 应为数字（群组为负数）或 @频道名"), "err")
        return redirect("/notify")
    ns.telegram_chat_id = chat
    for k in CATEGORIES:
        setattr(ns, f"notify_{k}", bool(form.get(f"cat_{k}")))
    ns.stock_debounce = min(1440, max(0, _f(form, "debounce", int, 10))) * 60
    ns.lang = form.get("nlang") if form.get("nlang") in ("en", "zh") else ""
    db.commit()
    audit(request, user.id, "notify_save")
    flash(request, T("通知设置已保存"))
    return redirect("/notify")


@app.post("/notify/test")
async def notify_test(request: Request, user: User = Depends(require_user)):
    form = await form_csrf(request)
    cat = form.get("category") if form.get("category") in CATEGORIES else "system"
    if not limiter.check(f"nt:{user.id}", 6, 60):
        flash(request, T("测试发送过于频繁，请 1 分钟后再试"), "err")
        return redirect("/notify")
    errs = await asyncio.to_thread(notify_send_test, user.id, cat)
    flash(request, (T("「{0}」测试发送失败: ", CATEGORIES[cat]) + "; ".join(errs)) if errs
          else T("已发送「{0}」测试消息（如未收到，请检查 Token / Chat ID / Webhook）", CATEGORIES[cat]), "err" if errs else "ok")
    return redirect("/notify")


# ---------------------------------------------------------------- catalog API
@app.get("/api/catalog")
async def api_catalog(endpoint: str = "ovh-eu", subsidiary: str = "IE", type: str = "all", q: str = "",
                      refresh: int = 0, user: User = Depends(require_user)):
    # Advance/Scale plans are listed in BOTH public catalogs. Live probe: both cart endpoints accept them,
    # but only /baremetalServers exposes their full option set, so the bare-metal entry wins.
    types = ["baremetalServers", "eco"] if type == "all" else [type]
    out, cur, count, seen = [], "EUR", 0, set()
    for ct in types:
        try:
            cat = await asyncio.to_thread(get_catalog, endpoint, subsidiary, ct, bool(refresh))
        except Exception as e:
            return JSONResponse({"error": f"{ct}: {e.__class__.__name__}: {e}"}, status_code=502)
        cur = cat.get("currency")
        for p in plan_summaries(cat, q):
            if p["planCode"] in seen:
                continue
            seen.add(p["planCode"])
            count += 1
            p["type"] = ct
            p["typeLabel"] = "Eco" if ct == "eco" else "Bare Metal"
            out.append(p)
    out.sort(key=lambda x: (x["name"].split(" |")[0], x["planCode"]))
    return {"currency": cur, "count": count, "plans": out}


@app.get("/api/plan")
async def api_plan(endpoint: str, subsidiary: str, type: str, plan: str, user: User = Depends(require_user)):
    cat = await asyncio.to_thread(get_catalog, endpoint, subsidiary, type)
    d = plan_detail(cat, plan)
    if not d:
        return JSONResponse({"error": T("目录中没有该 planCode")}, status_code=404)
    return d


@app.get("/api/availability")
async def api_availability(endpoint: str, plan: str, user: User = Depends(require_user)):
    if endpoint not in E.ENDPOINTS or not re.fullmatch(r"[\w.\-]{2,80}", plan):
        raise HTTPException(400)
    data = await fetch_once(endpoint, plan)
    rows = E.live_stock(plan, None, data)
    return {"plan": plan, "configs": len(data),
            "in_stock": [{"memory": r["memory"], "storage": r["storage"], "datacenters": r["datacenters"]} for r in rows]}


# ---------------------------------------------------------------- live stock (public availability, shared cache)
AV_RANK = {"1H-high": 0, "1H-low": 1, "24H": 2, "72H": 3, "240H": 4, "480H": 5, "720H": 6, "1440H": 7, "comingSoon": 8,
           "unknown": 9, "unavailable": 10}


# VAT applied by OVH per subsidiary (for the first-invoice estimate only; the cap check always uses OVH's preview)
VAT_GUESS = P.VAT


async def _stock_rows(endpoint: str, subsidiary: str, items: list[tuple[str, str]]) -> dict:
    """items = [(planCode, catalog_type)]. Registers viewer subscriptions; never polls OVH directly."""
    rows, feeds_info = [], []
    cats = {}
    for pc, ct in items:
        if ct not in cats:
            try:
                cats[ct] = await asyncio.to_thread(get_catalog, endpoint, subsidiary, ct)
            except Exception:
                cats[ct] = None
        feed = hub.watch(endpoint, pc)
        cat = cats[ct]
        plan_obj = cat["plans"].get(pc) if cat else None
        sym = cur_symbol(cat.get("currency", "EUR")) if cat else ""
        name = (plan_obj or {}).get("invoiceName", pc).split(" |")[0]
        cpu = ((plan_obj or {}).get("invoiceName", "").split(" | ") + [""])[1]
        feeds_info.append({"plan": pc, "name": name, "updated": localtime(feed.last_ok) if feed.last_ok else None, "updatedTs": feed.last_ok or None,
                           "error": feed.error, "interval": feed.interval(), "loading": feed.data is None})
        base_mo = E.monthly_and_setup(plan_obj)[0] if plan_obj else None
        for e in feed.data or []:
            if e.get("planCode") and e.get("planCode") != pc:
                continue
            mem, sto = e.get("memory") or "", e.get("storage") or ""
            price = first = None
            if plan_obj and base_mo is not None:
                price, setup = base_mo, E.monthly_and_setup(plan_obj)[1] or 0.0
                for fam, want in (("memory", mem), ("storage", sto)):
                    code = E.addon_code(plan_obj, fam, want, synthesize=False)
                    a = cat["addons"].get(code or "")
                    if a:
                        mo_a, set_a = E.monthly_and_setup(a)
                        price += mo_a or 0.0
                        setup += set_a or 0.0
                # what OVH bills on the first invoice (checked live against /order/cart/{id}/summary: RISE-1 IE = 140.20)
                first = round((price + setup) * VAT_GUESS.get(subsidiary.upper(), 1.0), 2)
            is_def = E.is_default_config(plan_obj, mem, sto) if plan_obj else False
            can, why = E.orderable(plan_obj, mem, sto)
            if not can:
                price = first = None   # no such addon: OVH has no price for this variant
            for d in e.get("datacenters") or []:
                av = d.get("availability") or "unknown"
                rows.append({"name": name, "cpu": cpu, "plan": pc, "type": ct, "dc": d.get("datacenter"),
                             "dcLabel": E.dc_label(d.get("datacenter") or ""), "memory": mem, "storage": sto,
                             "ram": E.short_ram(mem), "disk": E.short_disk(sto), "default": is_def,
                             "availability": av, "inStock": av not in E.UNAVAIL, "rank": AV_RANK.get(av, 6),
                             "price": round(price, 2) if price is not None else None, "first": first, "currency": sym,
                             "orderable": can, "why": why})
    rows.sort(key=lambda r: (not r["inStock"], r["rank"], r["plan"], r["price"] or 0, r["dc"]))
    code = next((c.get("currency") for c in cats.values() if c), "EUR")
    fx = await asyncio.to_thread(FX.rate, code)
    return {"rows": rows, "feeds": feeds_info, "now": localtime(time.time()), "nowTs": time.time(), "requests": hub.requests,
            "fx": _fx_view(fx)}


def _fx_view(fx: dict) -> dict:
    return {"base": fx["base"], "rate": fx["rate"], "source": fx["source"], "live": fx["live"],
            "fetched": localtime(fx["fetched"]) if fx.get("fetched") else None,
            "fetchedIso": dt.datetime.fromtimestamp(fx["fetched"], dt.timezone.utc).isoformat() if fx.get("fetched") else None}


@app.get("/api/price")
async def api_price(endpoint: str, subsidiary: str = "IE", plans: str = "[]", duration: str = "P1M",
                    pricing_mode: str = "default", extras: str = "", user: User = Depends(require_user)):
    """Live price breakdown for the task form: [{planCode,type,memory,storage,bandwidth}] -> monthly / setup /
    VAT / first invoice per plan, plus the CNY conversion."""
    if endpoint not in E.ENDPOINTS:
        raise HTTPException(400)
    sub = re.sub(r"[^A-Z]", "", subsidiary.upper())[:6] or "IE"
    if not limiter.check(f"price:{user.id}", 240, 60):
        return JSONResponse({"error": T("请求过于频繁")}, status_code=429)
    try:
        items = json.loads(plans)[:20]
        if not (isinstance(items, list) and all(isinstance(x, dict) for x in items)):
            raise ValueError("plans")
    except Exception:
        raise HTTPException(400)
    ex = [x for x in extras.split(",") if re.fullmatch(r"[\w.\-]{2,120}", x)]
    out, cur = [], "EUR"
    for it in items:
        pc = str(it.get("planCode") or "")
        ct = it.get("type") if it.get("type") in E.CATALOG_TYPES else "eco"
        if not re.fullmatch(r"[\w.\-]{2,80}", pc):
            continue
        try:
            cat = await asyncio.to_thread(get_catalog, endpoint, sub, ct)
        except Exception as e:
            out.append({"planCode": pc, "error": T("目录加载失败: {0}", type(e).__name__)})
            continue
        cur = cat.get("currency", "EUR")
        b = P.breakdown(cat, pc, str(it.get("memory") or "")[:120], str(it.get("storage") or "")[:160],
                        str(it.get("bandwidth") or "")[:120], ex if len(items) == 1 else [], duration, pricing_mode, sub)
        out.append(b or {"planCode": pc, "error": T("目录中没有该型号的价格")})
    fx = await asyncio.to_thread(FX.rate, cur)
    return {"currency": cur, "vatRate": P.vat_for(sub), "subsidiary": sub, "plans": out, "fx": _fx_view(fx)}


def _mine(db: Session, user: User) -> list[dict]:
    """Models used in the owner's tasks (the 「导入我的任务型号」 button of the live-stock browser)."""
    mine = []
    for t in db.scalars(select(Task).where(Task.user_id == user.id)):
        for p in _plans_with_type(t):
            if not any(m["planCode"] == p["planCode"] and m["endpoint"] == t.endpoint for m in mine):
                mine.append({"planCode": p["planCode"], "type": p["type"], "label": p["label"] or p["planCode"],
                             "endpoint": t.endpoint, "subsidiary": t.subsidiary})
    return mine


@app.get("/stock", response_class=HTMLResponse)
def stock_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return render(request, "stock.html", user, mine=_mine(db, user))


@app.get("/api/stock")
async def api_stock(endpoint: str = "ovh-eu", subsidiary: str = "IE", plans: str = "",
                    user: User = Depends(require_user)):
    if endpoint not in E.ENDPOINTS:
        raise HTTPException(400)
    items = []
    for part in plans.split(",")[:30]:
        pc, _, ct = part.partition(":")
        if re.fullmatch(r"[\w.\-]{2,80}", pc):
            items.append((pc, ct if ct in E.CATALOG_TYPES else "eco"))
    try:
        return await _stock_rows(endpoint, re.sub(r"[^A-Z]", "", subsidiary.upper())[:6] or "IE", items)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=429)


@app.get("/api/stock/mine")
async def api_stock_mine(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Dashboard widget: in-stock rows for every plan in the user's tasks."""
    groups: dict[tuple, list] = {}
    for t in db.scalars(select(Task).where(Task.user_id == user.id)):
        for p in _plans_with_type(t):
            lst = groups.setdefault((t.endpoint, t.subsidiary), [])
            if (p["planCode"], p["type"]) not in lst:
                lst.append((p["planCode"], p["type"]))
    rows, feeds = [], []
    for (ep, sub), items in groups.items():
        r = await _stock_rows(ep, sub, items[:30])
        for x in r["rows"]:
            x["endpoint"] = ep
            x["cny"] = FX.cny(x.get("price"), FX.currency_for(sub, symbol=x.get("currency"))) if x["inStock"] else ""
        rows += [x for x in r["rows"] if x["inStock"]]
        feeds += r["feeds"]
    rows.sort(key=lambda r: (r["rank"], r["plan"], r["price"] or 0))
    return {"rows": rows[:200], "feeds": feeds, "now": localtime(time.time()), "nowTs": time.time()}


# Plans the map shows when the owner has no task yet (RISE-1 / ADVANCE-1 in ovh-eu, the most watched models).
MAP_DEMO = [("ovh-eu", "IE", "24rise01-v2", "eco"), ("ovh-eu", "IE", "24adv01-v3", "baremetalServers")]


def _map_scope(db: Session, user: User) -> tuple[str, dict]:
    """Which plans the datacenter map aggregates: the owner's task plans (running tasks first) -> plans the shared
    poller is already watching (e.g. the 实时库存 page) -> MAP_DEMO. Returns (scope kind, {(ep, sub): [(plan, type)]})."""
    groups: dict[tuple, list] = {}
    tasks = sorted(db.scalars(select(Task).where(Task.user_id == user.id)), key=lambda t: (t.id not in manager.runners, -t.id))
    for t in tasks:
        for p in _plans_with_type(t):
            lst = groups.setdefault((t.endpoint, t.subsidiary), [])
            if (p["planCode"], p["type"]) not in lst:
                lst.append((p["planCode"], p["type"]))
    if groups:
        return "tasks", groups
    for (ep, pc), f in list(hub.feeds.items()):
        if f.data is not None and ep in E.ENDPOINTS:
            ct = "baremetalServers" if re.search(r"adv|scale|hgr|hci|sdl|sds", pc) else "eco"
            groups.setdefault((ep, E.SUBSIDIARIES[ep][0]), []).append((pc, ct))
    if groups:
        return "feeds", groups
    for ep, sub, pc, ct in MAP_DEMO:
        groups.setdefault((ep, sub), []).append((pc, ct))
    return "demo", groups


@app.get("/api/stock/map")
async def api_stock_map(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Dashboard map: per-datacenter stock level aggregated from the shared poller (never polls OVH directly)."""
    kind, groups = _map_scope(db, user)
    rows, plans, loading = [], set(), False
    for (ep, sub), items in groups.items():
        try:
            r = await _stock_rows(ep, sub, items[:30])
        except RuntimeError:
            continue
        loading = loading or any(f["loading"] for f in r["feeds"])
        for x in r["rows"]:
            x["endpoint"] = ep
        rows += r["rows"]
        plans.update((ep, pc) for pc, _ in items[:30])
    return {"sites": DCM.aggregate(rows, AV_RANK), "levels": DCM.legend(), "loading": loading,
            "scope": {"kind": kind, "plans": len(plans), "label": {"tasks": T("任务型号"), "feeds": T("正在监控的型号"),
                                                                    "demo": T("示例：RISE-1 / ADVANCE-1")}[kind]},
            "now": localtime(time.time()), "nowTs": time.time()}


# ---------------------------------------------------------------- tasks
def _task_or_404(db, user, tid) -> Task:
    t = db.get(Task, tid)
    if not t or (t.user_id != user.id and not user.is_admin):
        raise HTTPException(404)
    return t


@app.get("/tasks/new", response_class=HTMLResponse)
def task_new(request: Request, user: User = Depends(require_user)):
    t = Task(endpoint="ovh-eu", subsidiary="IE", catalog_type="eco", plan_code="", dry_run=True, auto_pay=False,
             poll_interval=max(10.0, config.MIN_POLL_INTERVAL), quantity=1, max_orders=1, os_code="none_64.en",
             duration="P1M", pricing_mode="default", vat_rate=1.23, any_config=False, prebuild=False,
             notify_only=False, alert_cooldown=10)
    qp = request.query_params
    plans = _deep_link_items(qp)
    if plans:
        if qp.get("endpoint") in E.ENDPOINTS:
            t.endpoint = qp["endpoint"]
        if re.fullmatch(r"[A-Za-z]{2,6}", qp.get("subsidiary", "")):
            t.subsidiary = qp["subsidiary"].upper()
    if qp.get("mode") == "notify":
        t.notify_only = True
    with SessionLocal() as db:
        tg_ok, tg_why = tg_status(db, user.id)
        mine = _mine(db, user)
    return render(request, "task_form.html", user, t=t, is_new=True, plans=plans, TG_OK=tg_ok, TG_WHY=tg_why, mine=mine)


@app.get("/tasks/{tid}/edit", response_class=HTMLResponse)
def task_edit(tid: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    t = _task_or_404(db, user, tid)
    tg_ok, tg_why = tg_status(db, user.id)
    return render(request, "task_form.html", user, t=t, is_new=False, plans=_form_plans(t), TG_OK=tg_ok, TG_WHY=tg_why,
                  mine=_mine(db, user))


def _plans_with_type(t: Task) -> list[dict]:
    try:
        cfg = json.loads(t.plan_configs or "{}")
    except Exception:
        cfg = {}
    return [{**p, "type": p.get("type") or (cfg.get(p["planCode"]) or {}).get("type") or t.catalog_type} for p in task_plans(t)]


def _form_plans(t: Task) -> list[dict]:
    """Selection items for the task form: legacy rows get the task-wide datacenters copied onto every item."""
    legacy = [d for d in (t.datacenters or "").split(",") if d]
    return [{**p, "dcs": p["dcs"] if p.get("dcs") is not None else list(legacy)} for p in _plans_with_type(t)]


DC_RE = re.compile(r"[a-z]{2,5}")


def _deep_link_items(qp) -> list[dict]:
    """/tasks/new deep links. Single row (Live stock "New task"): plan,type,dc,memory,storage. Several rows:
    repeated item=planCode|type|memory|storage|dc1+dc2 (in priority order)."""
    raw = []
    if qp.get("plan"):
        raw.append((qp.get("plan", ""), qp.get("type", ""), qp.get("memory", ""), qp.get("storage", ""), qp.get("dc", "")))
    for it in qp.getlist("item")[:20]:
        parts = (it.split("|") + [""] * 5)[:5]
        raw.append(tuple(parts))
    out = []
    for pc, ct, mem, sto, dcs in raw:
        if not re.fullmatch(r"[\w.\-]{2,80}", pc or ""):
            continue
        dl = [d for d in re.split(r"[,+ ]", dcs or "") if DC_RE.fullmatch(d)]
        out.append({"planCode": pc, "type": ct if ct in E.CATALOG_TYPES else "eco", "label": "",
                    "memory": (mem or "")[:120], "storage": (sto or "")[:160], "bandwidth": "", "dcs": list(dict.fromkeys(dl))})
    return out


def _f(form, k, cast, default):
    v = form.get(k)
    if v in (None, ""):
        return default
    try:
        return cast(v)
    except Exception:
        return default


@app.post("/tasks/save")
async def task_save(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    form = await form_csrf(request)
    tid = _f(form, "id", int, None)
    t = _task_or_404(db, user, tid) if tid else Task(user_id=user.id)
    if tid and tid in manager.runners:
        flash(request, T("请先停止任务再修改"), "err")
        return redirect(f"/tasks/{tid}")
    ep = form.get("endpoint")
    try:
        plist = json.loads(form.get("plans_json") or "[]")
    except Exception:
        plist = []
    legacy_dcs = [d for d in form.getlist("datacenters") if DC_RE.fullmatch(d)]   # pre-v1.9 forms / API clients
    clean, seen = [], set()
    for x in (plist if isinstance(plist, list) else [])[:40]:
        if not isinstance(x, dict):
            continue
        pc = str(x.get("planCode") or "").strip()
        ct = x.get("type") if x.get("type") in E.CATALOG_TYPES else "eco"
        if not re.fullmatch(r"[\w.\-]{2,80}", pc):
            continue
        dcs = x.get("dcs") if isinstance(x.get("dcs"), list) else legacy_dcs
        dcs = list(dict.fromkeys(str(d) for d in dcs if DC_RE.fullmatch(str(d))))[:20]
        item = {"planCode": pc, "type": ct, "label": str(x.get("label") or "")[:120],
                "memory": str(x.get("memory") or "")[:120], "storage": str(x.get("storage") or "")[:160],
                "bandwidth": str(x.get("bandwidth") or "")[:120], "dcs": dcs}
        key = (pc, item["memory"], item["storage"], item["bandwidth"], tuple(dcs))
        if key in seen:
            continue
        seen.add(key)
        clean.append(item)
        if len(clean) >= 20:
            break
    if ep not in E.ENDPOINTS or not clean:
        flash(request, T("请选择区域和至少一个型号"), "err")
        return redirect(f"/tasks/{tid}/edit" if tid else "/tasks/new")
    first = clean[0]
    t.endpoint, t.catalog_type, t.plan_code = ep, first["type"], first["planCode"]
    t.plan_codes = ",".join(dict.fromkeys(c["planCode"] for c in clean))
    # legacy view (first item per planCode) for older readers; plan_items is the source of truth
    legacy_cfg: dict = {}
    for c in clean:
        legacy_cfg.setdefault(c["planCode"], {k: v for k, v in c.items() if k not in ("planCode", "dcs")})
    t.plan_configs = json.dumps(legacy_cfg, ensure_ascii=False)
    t.plan_items = json.dumps(clean, ensure_ascii=False)
    t.plan_label = " / ".join(c["label"] or c["planCode"] for c in clean)[:200]
    t.memory, t.storage, t.bandwidth = first["memory"], first["storage"], first["bandwidth"]
    t.subsidiary = re.sub(r"[^A-Z]", "", (form.get("subsidiary") or "IE").upper())[:6] or "IE"
    t.name = (form.get("name") or "").strip()[:100]
    # task-wide list = ordered union of the items' datacenters (display only; empty when any item takes any DC)
    t.datacenters = "" if any(not c["dcs"] for c in clean) else ",".join(dict.fromkeys(d for c in clean for d in c["dcs"]))[:200]
    t.extra_addons = ",".join(x for x in form.getlist("extra_addons") if re.fullmatch(r"[\w.\-]{2,120}", x)) if len(clean) == 1 else ""
    t.any_config = bool(form.get("any_config"))
    t.os_code = re.sub(r"[^\w.\-]", "", (form.get("os_code") or "none_64.en"))[:64] or "none_64.en"
    t.duration = form.get("duration") if form.get("duration") in ("P1M", "P3M", "P6M", "P12M", "P24M") else "P1M"
    t.pricing_mode = re.sub(r"[^\w\-]", "", (form.get("pricing_mode") or "default"))[:32] or "default"
    t.quantity = min(10, max(1, _f(form, "quantity", int, 1)))
    t.max_orders = min(50, max(1, _f(form, "max_orders", int, 1)))
    t.price_cap = _f(form, "price_cap", float, None)
    t.vat_rate = min(2.0, max(1.0, _f(form, "vat_rate", float, 1.23)))
    t.poll_interval = max(config.MIN_POLL_INTERVAL, _f(form, "poll_interval", float, 10.0))
    pm = form.get("pay_mode")
    if pm not in PAY_MODE_CN:   # legacy form fields
        pm = "dry" if form.get("dry_run") else ("pay" if form.get("auto_pay") else "order")
    if pm == "notify":
        ok, why = tg_status(db, user.id)
        if not ok:   # server-side guard (the form blocks this too)
            db.rollback()
            flash(request, T("「仅提醒」只通过 Telegram 提醒你：{0}。请先到「通知」页配置 Telegram 再保存", why), "err")
            return redirect(f"/tasks/{tid}/edit" if tid else "/tasks/new?mode=notify")
    t.notify_only = pm == "notify"
    # notify-only is also a dry run, so every order path stays off even if a flag were ignored somewhere
    t.dry_run, t.auto_pay = pm in ("dry", "notify"), pm == "pay"
    t.prebuild = bool(form.get("prebuild")) and pm in ("order", "pay")
    t.alert_cooldown = min(1440, max(0, _f(form, "alert_cooldown", int, 10)))
    if not tid:
        t.status = "stopped"
        db.add(t)
    db.commit()
    audit(request, user.id, "task_update" if tid else "task_create", f"#{t.id} {PAY_MODE_CN[pm]} {t.plan_codes}")
    flash(request, T("任务已保存（{0}", PAY_MODE_CN[pm]) + (T("，有货只发 Telegram 提醒）") if pm == "notify" else T("，不会下单）") if t.dry_run else T("，有货会真实下单）")))
    return redirect(f"/tasks/{t.id}")


@app.get("/tasks/{tid}", response_class=HTMLResponse)
def task_detail(tid: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    t = _task_or_404(db, user, tid)
    orders = list(db.scalars(select(Order).where(Order.task_id == tid).order_by(Order.id.desc()).limit(50)))
    tg_ok, tg_why = tg_status(db, user.id) if t.notify_only else (True, "")
    return render(request, "task_detail.html", user, t=t, live=manager.state(tid), orders=orders, plans=nice_plans(t),
                  TG_OK=tg_ok, TG_WHY=tg_why)


@app.post("/tasks/{tid}/start")
async def task_start(tid: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    await form_csrf(request)
    t = _task_or_404(db, user, tid)
    if t.status == "pending_confirm":
        # an earlier checkout had an unknown result: a plain restart could order a second server (v2.1.1)
        flash(request, T("该任务上次结账结果不确定。请先到 OVH 控制台核对订单，再在任务页使用「已核对 OVH 订单，恢复任务」"), "err")
        return redirect(f"/tasks/{tid}")
    if t.notify_only:
        ok, why = tg_status(db, user.id)
        if not ok:
            flash(request, T("「仅提醒」只通过 Telegram 提醒你：{0}。请先到「通知」页配置 Telegram", why), "err")
            return redirect(f"/tasks/{tid}")
    if not t.dry_run:
        c = db.scalar(select(Credential).where(Credential.user_id == t.user_id, Credential.endpoint == t.endpoint))
        if not c or not c.consumer_key_enc:
            flash(request, T("真实下单需要先保存 {0} 的 OVH 密钥（含 Consumer Key）", t.endpoint), "err")
            return redirect(f"/tasks/{tid}")
    t.enabled, t.status, t.status_text = True, "starting", T("启动中")
    db.commit()
    audit(request, user.id, "task_start", f"#{tid}")
    manager.start(tid)
    flash(request, T("任务已启动"))
    return redirect(f"/tasks/{tid}")


def _orders_around_pending(t: Task, pending_ts: dt.datetime) -> list[dict]:
    """OVH orders created from 3 min before to 15 min after the uncertain checkout (raises on API errors)."""
    from .worker import load_creds
    creds = load_creds(t.user_id, t.endpoint)
    if not creds:
        raise RuntimeError(T("没有保存 {0} 的 OVH 密钥", t.endpoint))
    client = E.make_client(t.endpoint, *creds, timeout=(5, 20))
    utc = pending_ts.replace(tzinfo=dt.timezone.utc)
    ids = client.get("/me/order", **{"date.from": (utc - dt.timedelta(minutes=3)).isoformat(),
                                     "date.to": (utc + dt.timedelta(minutes=15)).isoformat()})
    out = []
    for oid in (ids or [])[-20:]:
        o = client.get(f"/me/order/{oid}")
        out.append({"orderId": oid, "date": o.get("date"), "url": o.get("url"),
                    "priceWithTax": E.price_value(o.get("priceWithTax"))})
    return out


@app.post("/tasks/{tid}/resume-confirmed")
async def task_resume_confirmed(tid: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Resume a pending_confirm task only after the owner ticked "I have checked my OVH orders". OVH orders created
    around the uncertain checkout are looked up first and counted towards the task's max orders, so a checkout that
    did go through is never bought twice. Fails closed: if OVH can't be queried the task stays stopped."""
    form = await form_csrf(request)
    t = _task_or_404(db, user, tid)
    if t.status != "pending_confirm":
        return redirect(f"/tasks/{tid}")
    if form.get("confirm") != "yes":
        flash(request, T("请先勾选「我已在 OVH 控制台核对过订单」"), "err")
        return redirect(f"/tasks/{tid}")
    last = db.scalar(select(TaskLog).where(TaskLog.task_id == tid, TaskLog.marker == "PENDING_CONFIRM")
                     .order_by(TaskLog.id.desc()).limit(1))
    pending_ts = last.ts if last else utcnow()
    try:
        found = await asyncio.to_thread(_orders_around_pending, t, pending_ts)
    except Exception as e:
        flash(request, T("无法查询 OVH 订单（{0}），为避免重复下单任务保持停止。请稍后重试", type(e).__name__), "err")
        return redirect(f"/tasks/{tid}")
    known = {str(o) for o in db.scalars(select(Order.order_id).where(Order.user_id == t.user_id, Order.order_id != ""))}
    new = [o for o in found if str(o["orderId"]) not in known]
    for o in new:
        db.add(Order(user_id=t.user_id, task_id=t.id, kind="order", order_id=str(o["orderId"]), url=o.get("url") or "",
                     plan_code=t.plan_code or "", price_with_tax=o.get("priceWithTax"), status="found-after-pending",
                     endpoint=t.endpoint, detail=json.dumps({"source": "resume-confirmed", "date": o.get("date")})))
    for o in db.scalars(select(Order).where(Order.task_id == tid, Order.kind == "pending", Order.status == "pending-confirm")):
        o.status = "pending-resolved"
    t.orders_done = (t.orders_done or 0) + len(new)
    note = T("恢复前查询到结账时段内的 OVH 订单 {0} 个（新记录 {1} 个）", len(found), len(new))
    db.add(TaskLog(task_id=tid, marker="INFO", msg=T("用户确认已核对 OVH 订单。") + note))
    if t.orders_done >= t.max_orders:
        t.enabled, t.status, t.status_text = False, "done", T("完成：已下单 {0}", t.orders_done)
        db.commit()
        audit(request, user.id, "task_resume_confirmed", f"#{tid} found={len(found)} new={len(new)} done")
        flash(request, note + T("；已达到最大下单数，任务标记为完成，不会再下单"))
        return redirect(f"/tasks/{tid}")
    t.enabled, t.status, t.status_text = True, "starting", T("启动中")
    db.commit()
    audit(request, user.id, "task_resume_confirmed", f"#{tid} found={len(found)} new={len(new)}")
    manager.start(tid)
    flash(request, note + T("；任务已恢复"))
    return redirect(f"/tasks/{tid}")


@app.post("/tasks/{tid}/stop")
async def task_stop(tid: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    await form_csrf(request)
    t = _task_or_404(db, user, tid)
    t.enabled = False
    db.commit()
    await manager.stop(tid)
    audit(request, user.id, "task_stop", f"#{tid}")
    if request.headers.get("x-requested-with") == "fetch":
        return JSONResponse({"ok": True})
    flash(request, T("任务已停止"))
    back = (await request.form()).get("next") or ""
    return redirect(back if back in ("/",) else f"/tasks/{tid}")   # local paths only (no open redirect)


@app.post("/tasks/{tid}/clone")
async def task_clone(tid: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    await form_csrf(request)
    t = _task_or_404(db, user, tid)
    skip = {"id", "user_id", "status", "status_text", "orders_done", "polls", "last_poll_at", "enabled", "created_at", "updated_at"}
    n = Task(**{c.name: getattr(t, c.name) for c in Task.__table__.columns if c.name not in skip})
    n.user_id, n.name, n.status = user.id, ((t.name or t.plan_label or t.plan_code)[:90] + T("（副本）")), "stopped"
    db.add(n)
    db.commit()
    audit(request, user.id, "task_clone", f"#{tid} -> #{n.id}")
    flash(request, T("已复制为任务 #{0}（未启动），可修改后再启动", n.id))
    return redirect(f"/tasks/{n.id}/edit")


@app.post("/tasks/{tid}/delete")
async def task_delete(tid: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    await form_csrf(request)
    t = _task_or_404(db, user, tid)
    await manager.stop(tid)
    db.delete(t)
    db.commit()
    audit(request, user.id, "task_delete", f"#{tid}")
    flash(request, T("任务已删除"))
    return redirect("/")


def _logs_payload(db, t: Task, after: int) -> dict:
    q = select(TaskLog).where(TaskLog.task_id == t.id)
    if after:
        rows = list(db.scalars(q.where(TaskLog.id > after).order_by(TaskLog.id).limit(500)))
    else:
        rows = list(reversed(list(db.scalars(q.order_by(TaskLog.id.desc()).limit(300)))))
    st = manager.state(t.id) or {}
    return {"status": t.status, "status_cn": STATUS_CN.get(t.status, t.status), "status_text": t.status_text,
            "running": t.id in manager.runners, "polls": st.get("polls", 0), "in_stock": st.get("in_stock"),
            "lastPoll": st.get("last_poll"), "lastMs": st.get("last_ms", 0), "phase": st.get("phase"),
            "feedError": st.get("feed_error"), "ordersDone": t.orders_done, "now": time.time(),
            "stock": st.get("stock", []), "prebuilt": st.get("prebuilt"),
            "logs": [{"id": r.id, "ts": localtime(r.ts), "iso": iso(r.ts), "marker": r.marker, "msg": r.msg} for r in rows]}


@app.get("/api/tasks/{tid}/logs")
def api_logs(tid: int, after: int = 0, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return _logs_payload(db, _task_or_404(db, user, tid), after)


SSE_LIFETIME = 25.0   # each stream ends after this; EventSource reconnects in ~1s (keeps restarts/shutdown fast)


@app.get("/api/tasks/{tid}/stream")
async def api_log_stream(tid: int, request: Request, after: int = 0, user: User = Depends(require_user),
                         db: Session = Depends(get_db)):
    """Server-Sent Events: new log lines + live task state, pushed within ~1s. Same data as /logs."""
    _task_or_404(db, user, tid)
    db.close()   # don't hold a SQLite read transaction open for the life of the stream
    try:
        after = max(after, int(request.headers.get("last-event-id") or 0))
    except ValueError:
        pass
    uid, admin = user.id, user.is_admin

    def snapshot(a: int) -> dict | None:
        with SessionLocal() as s:
            t = s.get(Task, tid)
            if not t or (t.user_id != uid and not admin):
                return None
            return _logs_payload(s, t, a)

    async def gen():
        nonlocal after
        yield "retry: 1000\n\n"
        end, last_sig, last_sent = time.monotonic() + SSE_LIFETIME, None, 0.0
        while time.monotonic() < end:
            if await request.is_disconnected():
                return
            d = await asyncio.to_thread(snapshot, after)
            if d is None:
                yield "event: gone\ndata: {}\n\n"
                return
            sig = (d["status"], d["status_text"], d["running"], d["polls"], d["in_stock"], d["prebuilt"], d["phase"])
            if d["logs"] or sig != last_sig or time.monotonic() - last_sent > 10:
                if d["logs"]:
                    after = d["logs"][-1]["id"]
                last_sig, last_sent = sig, time.monotonic()
                yield f"id: {after}\ndata: {json.dumps(d, ensure_ascii=False, default=str)}\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


@app.get("/api/tasks/running")
def api_running(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Live strip on the task pages: every running task of this user."""
    out = []
    for t in db.scalars(select(Task).where(Task.user_id == user.id).order_by(Task.id)):
        st = manager.state(t.id)
        if st is None:
            continue
        pm = t.pay_mode
        out.append({"id": t.id, "name": t.name or "", "plans": [{"code": p["planCode"], "name": p["name"], "cpu": p["cpu"]} for p in nice_plans(t)],
                    "mode": pm, "modeCn": PAY_MODE_CN[pm], "polls": st.get("polls", 0), "lastPoll": st.get("last_poll"),
                    "lastMs": st.get("last_ms", 0), "inStock": bool(st.get("in_stock")), "phase": st.get("phase"),
                    "statusText": t.status_text, "ordersDone": t.orders_done, "maxOrders": t.max_orders,
                    "interval": t.poll_interval, "error": st.get("feed_error")})
    return {"now": time.time(), "tasks": out}


@app.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    orders = list(db.scalars(select(Order).where(Order.user_id == user.id).order_by(Order.id.desc()).limit(300)))
    eps, guide = account_ctx(db, user)
    app_orders = {}
    for o in orders:
        if o.order_id and o.kind == "order":
            app_orders.setdefault(str(o.order_id), o.task_id)
    return render(request, "orders.html", user, orders=orders, account_eps=eps, app_orders=app_orders, guide=guide)


@app.get("/orders/{oid}", response_class=HTMLResponse)
def order_detail(oid: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    o = db.get(Order, oid)
    if not o or o.user_id != user.id:
        raise HTTPException(404)
    try:
        spec = json.loads(o.spec_json) if o.spec_json else None
    except Exception:
        spec = None
    try:
        detail = json.loads(o.detail or "{}")
    except Exception:
        detail = {}
    official = spec.get("official") if spec else None
    if not official and o.kind == "order":
        from .tracker import official_for
        try:
            official = official_for(o)[0]
        except Exception:
            official = None
    task = db.get(Task, o.task_id) if o.task_id else None
    return render(request, "order_detail.html", user, o=o, spec=spec, official=official, detail=detail, task=task,
                  OVH_STATUS_CN=A.ORDER_STATUS_CN)


@app.post("/orders/{oid}/check")
async def order_check(oid: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    await form_csrf(request)
    o = db.get(Order, oid)
    if not o or o.user_id != user.id or o.kind != "order":
        raise HTTPException(404)
    if not limiter.check(f"oc:{user.id}", 6, 60):
        flash(request, T("检查过于频繁，请稍后再试"), "err")
        return redirect(f"/orders/{oid}")
    if o.track_state in ("", "timeout"):
        o.track_state = "tracking"
        db.commit()
    audit(request, user.id, "order_check", f"#{o.order_id}")
    res = await asyncio.to_thread(check_order, oid)
    flash(request, T("已向 OVH 查询：") + (T("状态 {0}", A.ORDER_STATUS_CN.get(res.get('status'), res.get('status'))) if res.get("status")
                                        else T("暂时无法查询（") + (res.get("error") or (T("缺少密钥") if res.get("nocreds") else T("未知"))) + T("）"))
          + (T("，已交付") if res.get("delivered") else ""))
    return redirect(f"/orders/{oid}")


@app.get("/api/account")
async def api_account(request: Request, refresh: int = 0, user: User = Depends(require_user),
                      db: Session = Depends(get_db)):
    """Existing OVH orders and dedicated servers, read-only. Returns cached data at once and fetches in the
    background (5 min cache); the page polls while `loading` is true."""
    from .worker import load_creds
    out = []
    for ep in E.ENDPOINTS:
        creds = load_creds(user.id, ep)
        if not creds or not all(creds):
            continue
        out.append(A.cache.get(user.id, ep, creds, refresh=bool(refresh) and limiter.check(f"ar:{user.id}:{ep}", 10, 600)))
    return {"endpoints": out, "ttl": A.CACHE_TTL, "orderDays": A.ORDER_DAYS, "orderLimit": A.ORDER_LIMIT}


# ---------------------------------------------------------------- admin
@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    users = list(db.scalars(select(User).order_by(User.id)))
    counts = dict(db.execute(select(Task.user_id, func.count(Task.id)).group_by(Task.user_id)).all())
    running = {}
    for tid, r in manager.runners.items():
        uid = getattr(getattr(r, "t", None), "user_id", None)
        running[uid] = running.get(uid, 0) + 1
    audits = list(db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(100)))
    names = {u.id: u.username for u in users}
    return render(request, "admin.html", user, users=users, counts=counts, running=running, audits=audits, names=names,
                  feeds=hub.stats(), total_requests=hub.requests)


@app.get("/healthz")
def healthz():
    return {"ok": True, "runners": len(manager.runners), "feeds": len(hub.feeds)}


# outermost: real client IP / https scheme from Cloudflare edges only (TRUST_CLOUDFLARE=1), per-request Secure cookies
app.add_middleware(CloudflareProxy, force_secure=config.COOKIE_SECURE)
