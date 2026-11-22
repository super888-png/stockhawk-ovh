"""Per-user notifications by category: Telegram bot and/or a generic JSON webhook.

Categories (each switchable per user): stock 有货 / order 提交订单 / paid 付款 / delivered 交付 / system 任务提醒.
有货 is debounced per (user, key) so a flapping stock signal doesn't spam."""
from __future__ import annotations

import datetime as dt
import html
import logging
import re
import threading
import time

import httpx

from .db import SessionLocal
from .models import NotifySetting, User
from .netguard import Blocked, post_json
from .security import dec
from .i18n import T, TDict, lang_scope, norm, relang, tr
from . import stockcard

log = logging.getLogger("notify")

CATEGORIES = TDict({"stock": "有货通知", "order": "提交订单通知", "paid": "付款通知", "delivered": "交付通知", "system": "任务提醒"})
CATEGORY_HELP = TDict({"stock": "任务匹配到库存时（同一型号默认 10 分钟内只提醒一次）",
                 "order": "订单提交成功：订单号、金额、付款链接；只下单不付款模式也会提醒去付款",
                 "paid": "订单付款成功（自动扣款或你在 OVH 手动付款后检测到）",
                 "delivered": "服务器交付完成：实际配置对比官方配置，抽到升级配置会恭喜你",
                 "system": "结账结果不确定、密钥失效、任务完成或出错"})
EVENT_CATEGORY = {"STOCK_FOUND": "stock", "PREVIEW_OK": "stock",
                  "ORDER_PLACED": "order", "ORDER_UNPAID": "order", "ORDER_OVER_CAP": "order",
                  "ORDER_PAID": "paid", "DELIVERED": "delivered",
                  "PENDING_CONFIRM": "system", "AUTH_FAIL": "system", "DONE": "system", "TASK_ERROR": "system"}
IMPORTANT = set(EVENT_CATEGORY) | {"TEST"}
_last: dict[tuple, float] = {}
_lock = threading.Lock()


def _scrub(s: str, *secrets: str) -> str:
    for x in secrets:
        if x:
            s = s.replace(x, "***")
    return re.sub(r"bot\d+:[\w-]{20,}", "bot***", s)


def _debounced(user_id: int, key: str, gap: int) -> bool:
    now = time.time()
    with _lock:
        k = (user_id, key)
        if gap > 0 and now - _last.get(k, 0) < gap:
            return True
        _last[k] = now
        if len(_last) > 5000:
            for kk in [kk for kk, v in _last.items() if now - v > 3600]:
                _last.pop(kk, None)
    return False


def _settings(user_id: int) -> dict | None:
    with SessionLocal() as db:
        ns = db.get(NotifySetting, user_id)
        if not ns:
            return None
        u = db.get(User, user_id)
        return {"lang": norm(ns.lang or (u.lang if u else "") or None), "token": dec(ns.telegram_token_enc),
                "chat": ns.telegram_chat_id, "hook": dec(ns.webhook_url_enc), "ns": {
                    c: getattr(ns, f"notify_{c}", True) for c in CATEGORIES},
                "gap": ns.stock_debounce if ns.stock_debounce is not None else 600}


def tg_call(token: str, method: str, payload: dict) -> tuple[dict | None, str]:
    """Telegram Bot API call -> (result, error). Errors never contain the token."""
    try:
        r = httpx.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=10)
        try:
            j = r.json()
        except Exception:
            j = {}
        if r.status_code != 200 or not j.get("ok", False):
            return None, _scrub(f"Telegram HTTP {r.status_code} {str(j.get('description', ''))[:160]}", token)
        return j.get("result") or {}, ""
    except Exception as e:
        return None, f"Telegram: {type(e).__name__}"


# ---------------------------------------------------------------- restock cards (send, then edit when sold out)
_cards: dict[str, dict] = {}          # ref -> {"token", "chat", "mid", "info", "lang"}
_pending: dict[str, threading.Event] = {}
CARD_KEEP = 2000


def _local(ts: float) -> str:
    from zoneinfo import ZoneInfo
    from .config import APP_TZ
    try:
        tz = ZoneInfo(APP_TZ)
    except Exception:
        tz = dt.timezone.utc
    return dt.datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M:%S")


def send_card(user_id: int, info: dict, *, ref: str, dedupe_key: str | None = None, event: str = "STOCK_FOUND",
              force: bool = False) -> list[str]:
    """Restock card (stockcard.render) to Telegram with order buttons; the message id is kept under `ref` so
    mark_sold() can strike it through later. The webhook gets the same text as plain lines + the raw fields."""
    ev = _pending.setdefault(ref, threading.Event())
    try:
        st = _settings(user_id)
        if not st:
            return []
        if not force:
            if st["ns"].get("stock") is False:
                return []
            if _debounced(user_id, dedupe_key or ref, st["gap"]):
                return []
        info = {**info, "detected": info.get("detected") or _local(info.get("ts") or time.time())}
        with lang_scope(st["lang"]):
            info_l = {k: relang(v, st["lang"]) if isinstance(v, str) else v for k, v in info.items()}
            # language-dependent labels are built here, inside the notification language
            from . import labels as LB
            from .dcmap import SITES
            from .ovh_engine import dc_label
            info_l["memory"] = LB.ram_label(info.get("memory_code") or "") or info.get("memory") or ""
            info_l["storage"] = LB.disk_label(info.get("storage_code") or "") or info.get("storage") or ""
            site = SITES.get(info.get("dc") or "")
            info_l["city"] = info.get("city") or (site[0] if site else "")
            if info.get("others_codes"):
                info_l["others"] = ", ".join(dc_label(d) for d in info["others_codes"])
            if isinstance(info.get("order"), dict):   # buttons: labels in the notification language
                info_l["links"] = stockcard.order_links(**info["order"])
            tg, plain, kb = stockcard.render(info_l)
            card_title = T("OVH 补货通知")
        errors = []
        if st["token"] and st["chat"]:
            payload = {"chat_id": st["chat"], "text": tg, "parse_mode": "HTML", "disable_web_page_preview": True}
            if kb:
                payload["reply_markup"] = {"inline_keyboard": kb}
            res, err = tg_call(st["token"], "sendMessage", payload)
            if err:
                errors.append(err)
            elif res.get("message_id"):
                with _lock:
                    _cards[ref] = {"token": st["token"], "chat": st["chat"], "mid": res["message_id"], "info": info_l,
                                   "lang": st["lang"], "user": user_id}
                    while len(_cards) > CARD_KEEP:
                        _cards.pop(next(iter(_cards)))
        if st["hook"]:
            try:
                r = post_json(st["hook"], {"event": event, "category": "stock", "categoryName": CATEGORIES.get("stock"),
                                           "lang": st["lang"], "title": card_title, "text": plain,
                                           "link": (info_l.get("links") or [(None, None)])[0][1], "data": info_l,
                                           "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
                if r.status_code >= 400:
                    errors.append(f"Webhook HTTP {r.status_code}")
            except Blocked as e:
                errors.append(T("Webhook 地址被拒绝: {0}", e))
            except Exception as e:
                errors.append(f"Webhook: {type(e).__name__}")
        errors = [_scrub(e, st["token"], st["hook"]) for e in errors]
        for e in errors:
            log.warning("notify card user=%s: %s", user_id, e)
        return errors
    finally:
        ev.set()


def mark_sold(ref: str, ts: float | None = None, wait: float = 30.0) -> list[str]:
    """Edit the restock card sent under `ref`: body struck through + "已被抢完：<time>", buttons removed.
    Waits (briefly) for a send that is still in flight; no card (debounced / Telegram off) -> nothing to do."""
    ev = _pending.get(ref)
    if ev:
        ev.wait(wait)
    with _lock:
        c = _cards.pop(ref, None)
        _pending.pop(ref, None)
    if not c:
        return []
    with lang_scope(c["lang"]):
        tg, _plain, _kb = stockcard.render(c["info"], sold_at=_local(ts or time.time()))
    _res, err = tg_call(c["token"], "editMessageText", {"chat_id": c["chat"], "message_id": c["mid"], "text": tg,
                                                       "parse_mode": "HTML", "disable_web_page_preview": True})
    if err:
        log.warning("notify card edit user=%s: %s", c.get("user"), err)
        return [err]
    return []


def format_message(category: str, title: str, lines: list[str], link: str | None = None) -> tuple[str, str]:
    """-> (telegram HTML, plain text)"""
    head = T("【{0}】{1}", CATEGORIES.get(category, T("通知")), title)
    plain = "\n".join([head, *lines] + ([link] if link else []))
    tg = "\n".join([f"<b>{html.escape(head)}</b>", *(html.escape(x) for x in lines)]
                   + ([T("<a href=\"{0}\">打开链接</a>", html.escape(link, quote=True))] if link else []))
    return tg, plain


def send(user_id: int, event: str, text: str, data: dict | None = None, *, title: str = "",
         link: str | None = None, dedupe_key: str | None = None, category: str | None = None,
         force: bool = False) -> list[str]:
    """Blocking send; returns error strings (empty = sent, skipped by a switch, or nothing configured)."""
    cat = category or EVENT_CATEGORY.get(event, "system")
    errors: list[str] = []
    with SessionLocal() as db:
        ns = db.get(NotifySetting, user_id)
        if not ns:
            return errors
        u = db.get(User, user_id)
        lang = norm(ns.lang or (u.lang if u else "") or None)
        token, chat, hook = dec(ns.telegram_token_enc), ns.telegram_chat_id, dec(ns.webhook_url_enc)
        enabled = getattr(ns, f"notify_{cat}", True)
        gap = ns.stock_debounce if ns.stock_debounce is not None else 600
    if not force:
        if enabled is False:
            return errors
        if cat == "stock" and _debounced(user_id, dedupe_key or text[:120], gap):
            return errors
    with lang_scope(lang):
        text, title = relang(text or "", lang), relang(title or "", lang)
        lines = [x for x in text.splitlines() if x.strip()]
        tg, plain = format_message(cat, title or event, lines, link)
        cat_name = CATEGORIES.get(cat)
    if token and chat:
        try:
            r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                           json={"chat_id": chat, "text": tg[:4000], "parse_mode": "HTML", "disable_web_page_preview": True},
                           timeout=10)
            if r.status_code != 200:
                desc = ""
                try:
                    desc = r.json().get("description", "")
                except Exception:
                    pass
                errors.append(f"Telegram HTTP {r.status_code} {desc[:160]}")
        except Exception as e:
            errors.append(f"Telegram: {type(e).__name__}")
    if hook:
        try:
            r = post_json(hook, {"event": event, "category": cat, "categoryName": cat_name, "lang": lang, "title": title or event,
                                 "text": plain, "link": link, "data": data or {},
                                 "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
            if r.status_code >= 400:
                errors.append(f"Webhook HTTP {r.status_code}")
        except Blocked as e:
            errors.append(T("Webhook 地址被拒绝: {0}", e))
        except Exception as e:
            errors.append(f"Webhook: {type(e).__name__}")
    errors = [_scrub(e, token, hook) for e in errors]
    for e in errors:
        log.warning("notify user=%s cat=%s: %s", user_id, cat, e)
    return errors


SAMPLES = TDict({
    "stock": ("RISE-1 有货", ["任务 #3 Rise 欧洲", "RISE-1 [24rise01-v2] 法国 GRA 现货·少量", "32 GB / 2×512 GB NVMe", "（这是一条测试消息）"], None),
    "order": ("订单已提交 #231890455", ["任务 #3 Rise 欧洲", "RISE-1 法国 GRA · 32 GB / 2×512 GB NVMe", "首期含税 €140.20（≈ ¥1029.36）",
                                       "付款方式：只下单不付款，请尽快到 OVH 付款", "（这是一条测试消息）"],
              "https://www.ovh.com/manager/#/billing/orders"),
    "paid": ("订单 #231890455 已付款", ["RISE-1 法国 GRA", "OVH 状态：交付中", "（这是一条测试消息）"], None),
    "delivered": ("服务器已交付 ns3141592.ip-203-0-113.eu", ["RISE-1 法国 GRA · 订单 #231890455",
                                                          "实际配置：Intel Xeon-E 2386G 6C/12T · 64 GB · 2×512 GB NVMe",
                                                          "官方配置：Intel Xeon-E 2386G 6C/12T · 32 GB · 2×512 GB NVMe",
                                                          "恭喜中奖！内存 32 GB → 64 GB", "（这是一条测试消息）"], None),
    "system": ("任务 #3 已完成", ["已达到最大下单数 1，任务结束", "（这是一条测试消息）"], None),
})


SAMPLE_CARD = {"name": "KS-LE-B", "cpu": "Intel Xeon-E 2274G", "memory_code": "ram-32g-ecc-2666", "storage_code": "softraid-2x4000sa",
               "dc": "rbx", "availability": "1H-low", "sym": "€", "price": 25.99, "setup": 0.0, "cny_price": "≈ ¥190.82",
               "order": {"endpoint": "ovh-eu", "subsidiary": "IE", "plan_code": "24sklb01", "catalog_type": "eco", "dc": "rbx",
                         "region": "europe"}}


def send_test(user_id: int, category: str) -> list[str]:
    if category == "stock":   # the real restock card layout (with order buttons)
        return send_card(user_id, {**SAMPLE_CARD, "mode": T("这是一条测试消息")}, ref=f"test:{user_id}:{time.time()}", force=True)
    title, lines, link = dict.__getitem__(SAMPLES, category)   # untranslated ids; send() renders them in the notify language
    title, lines = T(title), [T(x) for x in lines]
    return send(user_id, "TEST", "\n".join(lines), title=title, link=link, category=category, force=True)
