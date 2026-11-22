"""Turn OVH /me/order/{id}/details lines into one clean summary per order.

OVH returns one line per cart item *and* per billing phase, unordered, e.g.
    "KS-LE-3 rental - datacenter bhs"        domain "*001.001"
    "Intel Xeon E-2274G"                     domain "*001.001"
    "2x SSD SATA 480GB Soft RAID"            domain "*001.001"
    "2x SSD SATA 480GB Soft RAID rental - 1 month"
    "Invoice IE1234567" / "debt2016v1.payment..." (invoice / debt orders)
We de-duplicate rental/non-rental pairs, drop the internal "*001.001" cart refs,
and classify lines into model / CPU / RAM / disks / bandwidth / extras.
"""
from __future__ import annotations

import re
from collections import Counter
from .i18n import T, TDict, tr

KIND_CN = TDict({"server": "服务器", "renew": "续费", "invoice": "账单", "debt": "欠款支付", "refund": "退款",
           "option": "附加服务", "other": "订单"})
_INTERNAL_REF = re.compile(r"^\*?\d{3}(\.\d{3})*$")
_PERIOD = re.compile(r"(?:rental|location)?\s*[-–:]?\s*(\d+)\s*(months?|mois|years?|ans?|an)\b", re.I)
_DC = re.compile(r"(?:datacenter|datacentre|data ?center)\s*[:\-]?\s*([a-z]{2,4}\d{0,2})\b", re.I)
_MODEL = re.compile(r"^\s*((?:KS|SYS|SO\s?YOU\s?START|RISE|ADVANCE|ADV|SCALE|HGR|HG|GAME|STOR|KIMSUFI|EG|SP|MG|BHG)"
                    r"(?:-[A-Z0-9]+)*(?:\s+Gen\s*\d+)?)\b", re.I)
_CPU = re.compile(r"\b((?:Intel|AMD)\b[^|,;]*)", re.I)
_RAM = re.compile(r"\b(\d+)\s*(?:G|GB|Go|GiB)\b.*\b(?:RAM|ECC|DDR\d?|memory|mémoire)\b|\bRAM\b|\bDDR\d\b", re.I)
_DISK = re.compile(r"\b(?:SSD|NVMe|HDD|SATA|SAS|disk|disque)\b|\bRAID\b", re.I)
_BW = re.compile(r"\b(?:bandwidth|bande passante|traffic|trafic|Mbps|Gbps|Mbit|Gbit)\b", re.I)
_RENTAL_TAIL = re.compile(r"\s*[-–(]*\s*(?:rental|location|renouvellement)?\s*[-–:]?\s*\d+\s*(?:months?|mois|years?|ans?)\)?\s*$"
                          r"|\s*[-–(]*\s*(?:rental|location)\)?\s*$", re.I)
_INSTALL = re.compile(r"\b(?:installation|setup|frais d'installation|install fee)\b", re.I)


def _clean(desc: str) -> str:
    s = re.sub(r"\s+", " ", (desc or "").strip())
    s = _DC.sub("", s)
    for _ in range(3):
        s = re.sub(r"\s*[-–:]\s*$", "", s).strip()
        s = _RENTAL_TAIL.sub("", s).strip(" -–|")
    s = re.sub(r"\b(\d+)x\s*", r"\1× ", s).replace("× ", "×")
    return re.sub(r"\s+", " ", s).strip()


def _kind(lines: list[dict]) -> str:
    text = " ".join((i.get("description") or "") for i in lines).lower()
    types = {str(i.get("detailType") or "").upper() for i in lines}
    if "REFUND" in types or re.search(r"\brefund|rembours", text):
        return "refund"
    if re.search(r"\bdebt\w*\.payment|\bdebt\b|\bdette\b|\bimpayé", text):
        return "debt"
    if re.search(r"^\s*(?:invoice|facture|bill)\b", text) or re.search(r"\b(?:invoice|facture)\s+[A-Z]{2}\d{4,}", text, re.I):
        return "invoice"
    if re.search(r"renouvellement|renewal|\brenew", text):
        return "renew"
    return "other"


def summarize(items: list[dict]) -> dict:
    """items: [{description, domain, quantity, total, detailType?}] -> summary dict (raw lines kept separately)."""
    items = [i for i in (items or []) if (i.get("description") or "").strip()]
    out = {"kind": "other", "kindCn": KIND_CN["other"], "model": "", "dc": "", "cpu": "", "ram": "", "disks": [],
           "bandwidth": "", "extras": [], "domain": "", "period": "", "ref": ""}
    if not items:
        return out
    kind = _kind(items)
    # domain: the server name/domain once; internal cart refs like "*001.001" dropped
    doms = Counter(d for i in items if (d := (i.get("domain") or "").strip()) and d != "*" and not _INTERNAL_REF.match(d))
    if doms:
        out["domain"] = doms.most_common(1)[0][0]
    for i in items:
        desc = i.get("description") or ""
        if not out["period"] and (m := _PERIOD.search(desc)):
            n, unit = int(m.group(1)), m.group(2).lower()
            out["period"] = T("{0} 个月", n) if unit.startswith(("month", "mois")) else T("{0} 年", n)
        if not out["dc"] and (m := _DC.search(desc)):
            out["dc"] = m.group(1).lower()
    if kind in ("invoice", "debt", "refund"):
        ref = re.search(r"\b([A-Z]{2}\d{5,})\b", " ".join(i.get("description") or "" for i in items))
        out.update(kind=kind, kindCn=KIND_CN[kind], ref=ref.group(1) if ref else "")
        return out
    seen = set()
    for i in items:
        line = _clean(i.get("description") or "")
        key = line.lower()
        if not line or key in seen:
            continue
        seen.add(key)
        if "|" in (i.get("description") or "") and not out["model"]:
            head, _, tail = (i.get("description") or "").partition("|")
            if _MODEL.match(head) or not _DISK.search(head):
                out["model"] = _clean(head)
                if (c := _CPU.search(tail)) and not out["cpu"]:
                    out["cpu"] = _clean(c.group(1))
                continue
        if out["model"] and line.lower() == out["model"].lower():
            continue
        if kind == "renew":
            rest = re.sub(r"^(?:renouvellement|renewal|renew)\b\s*", "", line, flags=re.I)
            if out["domain"]:
                rest = rest.replace(out["domain"], "")
            line = rest.strip(" -–:")
            if not line:
                continue
        if not out["model"] and (m := _MODEL.match(line)) and not _DISK.search(line) and not _RAM.search(line):
            out["model"] = m.group(1).strip()
            rest = line[m.end():].strip(" -–|")
            if (c := _CPU.search(rest)) and not out["cpu"]:
                out["cpu"] = _clean(c.group(1))
            continue
        if _CPU.match(line) and not out["cpu"]:
            out["cpu"] = line
        elif _RAM.search(line) and not _DISK.search(line) and not out["ram"]:
            out["ram"] = re.sub(r"\b(?:of\s+)?(?:RAM|memory)\b", "", line, flags=re.I).strip() or line
        elif _DISK.search(line):
            out["disks"].append(line)
        elif _BW.search(line) and not out["bandwidth"]:
            out["bandwidth"] = re.sub(r"^(?:bandwidth|bande passante)\s*[:\-]?\s*", "", line, flags=re.I) or line
        elif _INSTALL.search(line):
            out["extras"].append(T("安装费"))
        else:
            out["extras"].append(line)
    if kind == "renew":
        out.update(kind="renew", kindCn=KIND_CN["renew"])
    elif out["model"] or out["cpu"]:
        out.update(kind="server", kindCn=KIND_CN["server"])
    elif out["extras"]:
        out.update(kind="option", kindCn=KIND_CN["option"])
    out["extras"] = list(dict.fromkeys(out["extras"]))[:4]
    return out
