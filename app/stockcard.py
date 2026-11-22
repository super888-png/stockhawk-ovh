"""Telegram restock card: one HTML message per matching stock event, edited to strikethrough when it sells out.

    <b>OVH 补货通知</b>                     (English: OVH Restock)
    KS-LE-B 补货了（仅提醒）
    (blank)
    产品：KS-LE-B | Intel Xeon-E 2274G
    CPU / 内存 / 存储 / 机房：rbx（鲁贝 Roubaix） / 库存：1小时内有货 - 低库存 / 价格：€25.99/月（≈ ¥215.30） / 安装费
    (blank)
    检测时间：2026-11-21 15:04:05
  + inline URL buttons: OVH express order page for the plan (task subsidiary, datacenter preselected) + product page.
Sold out => the same message is edited: body in <s>…</s> + "已被抢完：<time>" (buttons removed).
All text is HTML-escaped; rendering happens inside the notification language scope (T() = Chinese ids -> English)."""
from __future__ import annotations

import html
import os
import urllib.parse

from .i18n import T, TDict

AV_TEXT = TDict({"1H-high": "1小时内有货 - 库存充足", "1H-low": "1小时内有货 - 低库存", "24H": "24小时内交付",
                 "72H": "72小时内交付", "240H": "约10天交付", "480H": "约20天交付", "comingSoon": "即将上架",
                 "unavailable": "无货", "unknown": "未知"})

# express order app + product pages per OVH subsidiary (checked live 2026-11; unknown -> generic English pages)
ORDER_HOST = {"ovh-eu": "https://order.eu.ovhcloud.com", "ovh-ca": "https://order.ca.ovhcloud.com", "ovh-us": "https://order.us.ovhcloud.com"}
ORDER_LOC = {"FR": "fr", "IE": "en-ie", "GB": "en-gb", "DE": "de", "ES": "es", "IT": "it", "NL": "nl", "PL": "pl", "PT": "pt",
             "MA": "ma", "SN": "sn", "TN": "tn", "CA": "en", "QC": "fr", "ASIA": "asia", "AU": "au", "SG": "sg", "IN": "in",
             "WS": "es", "US": "en"}
SITE_LOC = {"FR": "fr", "IE": "en-ie", "GB": "en-gb", "DE": "de", "ES": "es-es", "IT": "it", "NL": "nl", "PL": "pl", "PT": "pt",
            "MA": "fr-ma", "CA": "en-ca", "QC": "fr-ca", "ASIA": "asia", "AU": "en-au", "SG": "en-sg", "IN": "en-in"}


def _jsurl(v) -> str:
    """Minimal JSURL encoder (the format OVH's express order app reads from ?products=)."""
    if isinstance(v, dict):
        return "~(" + "~".join(f"{k}{_jsurl(x)}" for k, x in v.items()) + ")"
    if isinstance(v, list):
        return "~(" + "".join(_jsurl(x) for x in v) + ")"
    if isinstance(v, bool):
        return "~" + ("true" if v else "false")
    if isinstance(v, (int, float)):
        return "~" + str(v)
    s = "".join(c if c.isalnum() or c in "-_.*" else ("!" + c if c in "~'!()" else c if c == " " else
                                                      "*" + format(ord(c), "x").rjust(2, "0")) for c in str(v))
    return "~'" + s


def order_links(endpoint: str, subsidiary: str, plan_code: str, catalog_type: str, dc: str = "", region: str = "",
                duration: str = "P1M", pricing_mode: str = "default", os_code: str = "none_64.en") -> list[tuple[str, str]]:
    """[(button label, url)] — express order page with the plan (+ datacenter / region) preselected, then the product page.
    ORDER_LINK_AFF in .env (optional) is appended to every link as a query string (affiliate slot, e.g. "aff=xyz")."""
    sub = (subsidiary or "IE").upper()
    host = ORDER_HOST.get(endpoint, ORDER_HOST["ovh-eu"])
    loc = ORDER_LOC.get(sub, "en")
    cfg = [{"label": "dedicated_datacenter", "value": dc}] if dc else []
    if region:
        cfg.append({"label": "region", "value": region})
    if os_code:
        cfg.append({"label": "dedicated_os", "value": os_code})
    prod = {"planCode": plan_code, "productId": "baremetalServers" if catalog_type == "baremetalServers" else "eco",
            "pricingMode": pricing_mode or "default", "quantity": 1, "duration": duration or "P1M", "configuration": cfg}
    aff = os.environ.get("ORDER_LINK_AFF", "").strip().lstrip("?&")
    order = f"{host}/{loc}/express/{('?' + aff) if aff else ''}#/express/review?products={_jsurl([prod])}"
    if endpoint == "ovh-us":
        page = "https://us.ovhcloud.com/bare-metal/"
    else:
        sl = SITE_LOC.get(sub, "en")
        page = (f"https://eco.ovhcloud.com/{sl}/" if catalog_type != "baremetalServers" else f"https://www.ovhcloud.com/{sl}/bare-metal/")
    if aff:
        page += "?" + aff
    return [(T("立即下单（{0}）", sub), order), (T("OVH 官网产品页"), page)]


def _money(sym: str, v) -> str:
    return "–" if v is None else f"{sym}{v:,.2f}"


def render(info: dict, sold_at: str | None = None) -> tuple[str, str, list[list[dict]]]:
    """info: name, cpu, memory, storage, dc, city, availability, sym, price, setup, cny_price, mode, task, detected,
    links=[(label,url)]. -> (telegram HTML, plain text, inline_keyboard)"""
    e = lambda s: html.escape(str(s or ""), quote=False)
    title = T("OVH 补货通知")
    head = T("{0} 补货了", info.get("name") or "") + (T("（{0}）", info["mode"]) if info.get("mode") else "")
    av = info.get("availability") or "unknown"
    price = _money(info.get("sym", "€"), info.get("price"))
    rows = [
        (T("产品"), (info.get("name") or "") + (f" | {info['cpu']}" if info.get("cpu") else "")),
        ("CPU", info.get("cpu") or "–"),
        (T("内存"), info.get("memory") or "–"),
        (T("存储"), info.get("storage") or "–"),
        (T("机房"), (info.get("dc") or "").lower() + (T("（{0}）", info["city"]) if info.get("city") else "")),
        *([(T("其他有货机房"), info["others"])] if info.get("others") else []),
        (T("库存"), AV_TEXT.get(av, av)),
        (T("价格"), (T("{0}/月", price) if info.get("price") is not None else "–") + (T("（{0}）", info["cny_price"]) if info.get("cny_price") else "")),
        (T("安装费"), (T("免费") if info.get("setup") == 0 else _money(info.get("sym", "€"), info.get("setup")))),
    ]
    if info.get("cart"):
        rows.append((None, str(info["cart"]).strip()))
    tail = []
    if info.get("task"):
        tail.append((None, info["task"]))
    tail.append((T("检测时间"), info.get("detected") or ""))
    sep = T("：")
    body_plain = [head, ""] + [f"{k}{sep}{v}" if k else str(v) for k, v in rows] + [""] + [f"{k}{sep}{v}" if k else str(v) for k, v in tail]
    body_html = ([e(head), ""] + [f"<b>{e(k)}</b>{e(sep)}{e(v)}" if k else e(v) for k, v in rows] + [""]
                 + [f"{e(k)}{e(sep)}{e(v)}" if k else e(v) for k, v in tail])
    if sold_at:
        html_text = f"<b>{e(title)}</b>\n<s>" + "\n".join(body_html) + f"</s>\n\n<b>{e(T('已被抢完：{0}', sold_at))}</b>"
        plain = "\n".join([title, *body_plain, "", T("已被抢完：{0}", sold_at)])
        kb: list[list[dict]] = []
    else:
        html_text = f"<b>{e(title)}</b>\n" + "\n".join(body_html)
        plain = "\n".join([title, *body_plain])
        kb = [[{"text": lab, "url": url}] for lab, url in (info.get("links") or [])]
    return html_text[:4000], plain, kb
