"""Human-readable names for OVH plan codes and addon codes.

ram-32g-ecc-1333-25skb01                      -> 32 GB ECC 1333
noraid-1x120ssd-25skb01                       -> 1×120 GB SSD（无 RAID）
hybridsoftraid-2x6000sa-2x960nvme-24rise06-v2 -> 2×6 TB HDD + 2×960 GB NVMe（混合软 RAID）
bandwidth-500-25sk                            -> 500 Mbps
traffic-unlimited-1000-adv-gen3-apac          -> 1 Gbps · 不限流量
25skb012 (catalog invoiceName "KS-B | Intel Xeon E5-1620v2") -> KS-B / Intel Xeon E5-1620v2

Parsing is pure string work (no catalog needed); plan names come from the
already-cached public catalog and never trigger a network request.
Unknown codes fall back to the raw code.
"""
from __future__ import annotations

import re
from .i18n import T, TDict, tr

RAID_CN = TDict({"noraid": "无 RAID", "softraid": "软 RAID", "hybridsoftraid": "混合软 RAID", "hardraid": "硬 RAID",
           "hwraid": "硬 RAID", "raid": "RAID"})
_DISK_RE = re.compile(r"(\d+)x(\d+)(?:-?(nvme|ssd|sas|sata|sa|hdd))?((?:-(?:hdd|ssd|sas|sata))*)", re.I)


def _cap(gb: int) -> str:
    if gb >= 1000:
        tb = gb / 1000
        return f"{tb:g} TB"
    return f"{gb} GB"


def _speed(mbps: int) -> str:
    return f"{mbps / 1000:g} Gbps" if mbps >= 1000 else f"{mbps} Mbps"


def ram_label(code: str) -> str:
    if not code:
        return ""
    s = code.lower()
    m = re.search(r"(?:^|-)ram-?(\d+)g\b|^(\d+)g(?:b)?\b|(?:^|-)(\d+)g-", s)
    if not m:
        return code
    size = next(g for g in m.groups() if g)
    parts = [f"{size} GB"]
    if "ddr5" in s:
        parts.append("DDR5")
    elif "ddr4" in s:
        parts.append("DDR4")
    if re.search(r"non-?ecc|noecc", s):
        parts.append(T("非 ECC"))
    elif "ecc" in s:
        parts.append("ECC")
    sp = re.search(r"-(\d{4})(?=-|$)", s[m.end():] if m.end() < len(s) else "")
    if sp and 800 <= int(sp.group(1)) <= 9999:
        parts.append(sp.group(1))
    return " ".join(parts)


def disk_label(code: str) -> str:
    if not code:
        return ""
    s = code.lower()
    raid = ""
    m = re.match(r"(hybridsoftraid|softraid|hardraid|hwraid|noraid|raid)-", s)
    if m:
        raid = RAID_CN[m.group(1)]
    if re.match(r"noraid-0(-|$)", s) or re.search(r"(^|-)0disk(-|$)", s):
        return T("无硬盘")
    parts = []
    for n, size, kind, extra in _DISK_RE.findall(s):
        k = (kind or "").lower()
        ex = extra.lower()
        if k == "nvme":
            typ = "NVMe"
        elif k == "ssd" or "-ssd" in ex:
            typ = "SSD"
        elif k == "sas" and "hdd" not in ex:
            typ = "SAS"
        elif k in ("sa", "sata", "hdd") or "hdd" in ex:
            typ = "HDD" + (" SAS" if "sas" in ex or k == "sas" else "")
        else:
            typ = ""
        parts.append(f"{n}×{_cap(int(size))}{(' ' + typ) if typ else ''}")
    if not parts:
        return code
    return " + ".join(parts) + (T("（{0}）", raid) if raid else "")


def bw_label(code: str) -> str:
    if not code:
        return ""
    s = code.lower()
    vrack = s.startswith("vrack")
    m = re.search(r"traffic-(unlimited|\d+tb)-(\d+)", s)
    if m:
        quota = T("不限流量") if m.group(1) == "unlimited" else T("{0} TB 流量", m.group(1)[:-2])
        out = f"{_speed(int(m.group(2)))} · {quota}"
    else:
        m = re.search(r"bandwidth-(\d+)(?:-upto-(\d+))?", s)
        if not m:
            return code
        out = _speed(int(m.group(1)))
        if m.group(2):
            out += T("（最高 {0}）", _speed(int(m.group(2))))
        if "unguaranteed" in s:
            out += T("（不保证）")
    return (T("内网 vRack ") + out) if vrack else out


def addon_label(family: str, code: str) -> str:
    """Label for an addon code; guesses the family from the prefix when family is empty."""
    if not code:
        return ""
    f = (family or "").lower()
    s = code.lower()
    if f == "memory" or (not f and s.startswith("ram-")):
        return ram_label(code)
    if f == "storage" or (not f and re.match(r"(hybridsoftraid|softraid|hardraid|noraid|raid)-", s)):
        return disk_label(code)
    if f in ("bandwidth", "vrack") or (not f and re.match(r"(bandwidth|traffic|vrack)-", s)):
        return bw_label(code)
    return code


# ---------------------------------------------------------------- plan names from the cached catalog
def split_invoice(name: str) -> tuple[str, str]:
    """'KS-B | Intel Xeon E5-1620v2' -> ('KS-B', 'Intel Xeon E5-1620v2')."""
    if not name:
        return "", ""
    head, _, tail = name.partition("|")
    return head.strip(), tail.strip()


def plan_info(endpoint: str, subsidiary: str, plan_code: str, ctype: str | None = None) -> dict:
    """{planCode, name, cpu, cores} using only cached catalogs (memory/disk). Raw code when unknown."""
    from . import catalog as C
    out = {"planCode": plan_code, "name": "", "cpu": "", "cores": "", "known": False}
    if not plan_code:
        return out
    types = [ctype] if ctype else []
    types += [x for x in ("eco", "baremetalServers") if x not in types]
    import glob
    import os
    subs = [subsidiary or "IE"]
    for f in sorted(glob.glob(os.path.join(C.CACHE_DIR, f"{endpoint or 'ovh-eu'}_*_*.json"))):
        sub = os.path.basename(f).split("_")[1]
        if sub not in subs:
            subs.append(sub)
    for sub, ty in ((s_, t_) for s_ in subs for t_ in types):
        cat = C.peek(endpoint or "ovh-eu", sub, ty)
        plans = (cat or {}).get("plans", {})
        p = plans.get(plan_code)
        if not p or (p.get("invoiceName") or plan_code) == plan_code:
            # retired code (e.g. 24rise01) -> newest successor 24rise01-vN with the same model
            succ = sorted((k for k in plans if re.fullmatch(re.escape(plan_code) + r"-v\d+", k)),
                          key=lambda k: int(k.rsplit("-v", 1)[1]))
            p = plans[succ[-1]] if succ else p
        if not p or (p.get("invoiceName") or plan_code) == plan_code:
            continue
        name, cpu = split_invoice(p.get("invoiceName") or "")
        srv = ((cat.get("products") or {}).get(p.get("product")) or {}).get("server") or {}
        c = srv.get("cpu") or {}
        if not cpu and c.get("model"):
            cpu = f"{c.get('brand', '')} {c['model']}".strip()
        cores = f"{c['cores']}C/{c.get('threads') or c['cores']}T" if c.get("cores") else ""
        out.update(name=name or plan_code, cpu=cpu, cores=cores, known=True)
        return out
    return out


def plan_title(info: dict) -> str:
    """'KS-B · Intel Xeon E5-1620v2' or the raw planCode."""
    if not info.get("known"):
        return info.get("planCode", "")
    return info["name"] + (f" · {info['cpu']}" if info.get("cpu") else "")
