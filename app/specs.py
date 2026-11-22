"""Official (catalog) vs actual (delivered) server configuration, and the OVH 'lottery' check."""
from __future__ import annotations

import re

from . import ovh_engine as E
from .i18n import T, tr

DISK_RANK = {"nvme": 3, "ssd": 2, "sas": 1, "sata": 0, "hdd": 0}
DISK_CN = {3: "NVMe", 2: "SSD", 1: "SAS", 0: "HDD"}


def _norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"\(r\)|\(tm\)|cpu|processor|@.*$", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


def _rank(kind: str) -> int:
    k = (kind or "").lower()
    for name, r in DISK_RANK.items():
        if name in k:
            return r
    return 0


def _gb(v) -> float | None:
    if isinstance(v, dict):
        if v.get("value") is None:
            return None
        val, unit = float(v["value"]), (v.get("unit") or "GB").upper()
        return {"MB": val / 1024, "GB": val, "TB": val * 1000, "B": val / 1024 ** 3}.get(unit, val)
    return float(v) if v is not None else None


def _disk_text(disks: list[dict]) -> str:
    out = []
    for d in disks:
        gb = d.get("gb") or 0
        size = (f"{gb / 1000:g} TB" if gb >= 1000 else f"{gb:g} GB")
        out.append(f"{d.get('n') or 1}×{size} {DISK_CN.get(d.get('rank', 0), '')}".strip())
    return " + ".join(out) or "-"


def _text(cpu: dict, ram: float | None, disks: list[dict]) -> str:
    c = cpu.get("model") or "-"
    if cpu.get("cores"):
        c += f" {cpu['cores']}C" + (f"/{cpu['threads']}T" if cpu.get("threads") else "")
    return f"{c} · {f'{ram:g} GB' if ram else '-'} · {_disk_text(disks)}"


def cpu_scores(cats: list[dict]) -> dict[str, float]:
    out = {}
    for cat in cats:
        for prod in (cat or {}).get("products", {}).values():
            cpu = (prod.get("server") or {}).get("cpu") or {}
            if cpu.get("model") and cpu.get("score"):
                out[_norm(f"{cpu.get('brand', '')} {cpu['model']}")] = float(cpu["score"])
                out[_norm(cpu["model"])] = float(cpu["score"])
    return out


def official(cat: dict, plan_code: str, memory: str = "", storage: str = "") -> dict | None:
    plan, note = cat["plans"].get(plan_code), ""
    if not plan:
        # retired plan (e.g. 24rise01 -> 24rise01-v2): use the newest successor's catalog spec
        succ = sorted(c for c in cat["plans"] if re.fullmatch(re.escape(plan_code) + r"-v\d+", c))
        if not succ:
            return None
        old, plan_code = plan_code, succ[-1]
        plan, note = cat["plans"][plan_code], T("{0} 已下架，按后继型号 {1} 的目录规格对比", old, plan_code)
        memory = re.sub(r"-" + re.escape(old) + r"$", "", memory or "")
        storage = re.sub(r"-" + re.escape(old) + r"$", "", storage or "")
    prods = cat.get("products") or {}
    srv = (prods.get(plan.get("product")) or {}).get("server") or {}
    cpu_b = srv.get("cpu") or {}
    cpu = {"model": " ".join(x for x in (cpu_b.get("brand"), cpu_b.get("model")) if x),
           "cores": (cpu_b.get("cores") or 0) * (cpu_b.get("number") or 1) or None,
           "threads": (cpu_b.get("threads") or 0) * (cpu_b.get("number") or 1) or None,
           "frequency": cpu_b.get("frequency"), "boost": cpu_b.get("boost"), "score": cpu_b.get("score")}
    defaults = E.default_addons(plan)

    def addon(fam, want):
        code = (E.addon_code(plan, fam, want, synthesize=False) if want else None) or defaults.get(fam)
        a = cat["addons"].get(code or "") or {}
        return code, prods.get(a.get("product")) or {}

    mcode, mprod = addon("memory", memory)
    ram = (mprod.get("memory") or {}).get("size")
    if ram is None and mcode:
        m = re.search(r"ram-(\d+)g", mcode)
        ram = float(m.group(1)) if m else None
    scode, sprod = addon("storage", storage)
    st = sprod.get("storage") or {}
    disks = [{"n": d.get("number") or 1, "gb": float(d.get("capacity") or 0),
              "rank": 3 if (d.get("interface") or "").lower() == "nvme" else (2 if (d.get("technology") or "").upper() == "SSD" else 0)}
             for d in st.get("disks") or []]
    out = {"cpu": cpu, "ramGb": float(ram) if ram else None, "disks": disks,
           "diskTotalGb": sum(d["n"] * d["gb"] for d in disks), "raid": st.get("raid") or "",
           "memoryCode": mcode, "storageCode": scode, "note": note}
    out["text"] = _text(cpu, out["ramGb"], disks)
    return out


def actual(hw: dict, server: dict | None = None) -> dict:
    n = hw.get("numberOfProcessors") or 1
    cpu = {"model": hw.get("processorName") or "", "cores": (hw.get("coresPerProcessor") or 0) * n or None,
           "threads": (hw.get("threadsPerProcessor") or 0) * n or None}
    disks = [{"n": g.get("numberOfDisks") or 1, "gb": _gb(g.get("diskSize")) or 0.0, "rank": _rank(g.get("diskType")),
              "type": g.get("diskType") or ""} for g in hw.get("diskGroups") or []]
    ram = _gb(hw.get("memorySize"))
    out = {"cpu": cpu, "ramGb": round(ram) if ram else None, "disks": disks,
           "diskTotalGb": sum(d["n"] * d["gb"] for d in disks), "description": hw.get("description") or "",
           "datacenter": (server or {}).get("datacenter") or "", "os": (server or {}).get("os") or ""}
    out["text"] = _text(cpu, out["ramGb"], disks)
    return out


def compare(off: dict | None, act: dict | None, scores: dict[str, float] | None = None) -> dict:
    """-> {"upgrades": [str], "downgrades": [str], "lottery": bool}"""
    up, down = [], []
    if not off or not act:
        return {"upgrades": up, "downgrades": down, "lottery": False}
    scores = scores or {}
    # RAM
    if off.get("ramGb") and act.get("ramGb"):
        if act["ramGb"] > off["ramGb"] + 1:
            up.append(T("内存 {0:g} GB → {1:g} GB", off['ramGb'], act['ramGb']))
        elif act["ramGb"] < off["ramGb"] - 1:
            down.append(T("内存 {0:g} GB → {1:g} GB", off['ramGb'], act['ramGb']))
    # disks: capacity and medium
    if off.get("disks") and act.get("disks"):
        ot, at = off["diskTotalGb"], act["diskTotalGb"]
        orank, arank = max(d["rank"] for d in off["disks"]), max(d["rank"] for d in act["disks"])
        if arank > orank and at >= ot * 0.95:
            up.append(T("硬盘 {0} → {1}（介质升级）", _disk_text(off['disks']), _disk_text(act['disks'])))
        elif at > ot * 1.05 and arank >= orank:
            up.append(T("硬盘 {0} → {1}（容量更大）", _disk_text(off['disks']), _disk_text(act['disks'])))
        elif at < ot * 0.95 or arank < orank:
            down.append(T("硬盘 {0} → {1}", _disk_text(off['disks']), _disk_text(act['disks'])))
    # CPU: different model with a higher catalog benchmark score, or more cores
    oc, ac = off.get("cpu") or {}, act.get("cpu") or {}
    if oc.get("model") and ac.get("model") and _norm(oc["model"]) not in _norm(ac["model"]) and _norm(ac["model"]) not in _norm(oc["model"]):
        os_, as_ = oc.get("score") or scores.get(_norm(oc["model"])), scores.get(_norm(ac["model"]))
        if not as_:  # try partial match against known models
            as_ = next((v for k, v in scores.items() if k and (k in _norm(ac["model"]) or _norm(ac["model"]) in k)), None)
        if os_ and as_ and as_ > os_ * 1.03:
            up.append(T("CPU {0} → {1}（跑分 {2:g} → {3:g}）", oc['model'], ac['model'], os_, as_))
        elif (ac.get("cores") or 0) > (oc.get("cores") or 0) > 0:
            up.append(T("CPU {0} {1} 核 → {2} {3} 核", oc['model'], oc['cores'], ac['model'], ac['cores']))
        elif os_ and as_ and as_ < os_ * 0.97:
            down.append(f"CPU {oc['model']} → {ac['model']}")
    elif (ac.get("cores") or 0) > (oc.get("cores") or 0) > 0:
        up.append(T("CPU 核心 {0} → {1}", oc['cores'], ac['cores']))
    return {"upgrades": up, "downgrades": down, "lottery": bool(up)}
