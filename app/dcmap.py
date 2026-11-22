"""Datacenter map for the dashboard: where OVH's dedicated-server datacenters are and how much stock each has.

* SITES        - datacenter code -> city / country / coordinates (zone codes such as eu-west-par-a fold into "par").
* LEVELS/level - the ONE mapping from OVH availability values to the map's colour levels (JS reads it from the API).
* aggregate()  - per-site summary of the live stock rows produced by main._stock_rows (shared poller data).
"""
from .i18n import TDict, T, tr
import re

# code: (city, country, lat, lon, region). Coordinates are the datacenter towns, not the capital cities.
SITES: dict[str, tuple[str, str, float, float, str]] = TDict({
    "rbx": ("鲁贝 Roubaix", "法国", 50.69, 3.17, "eu"),
    "gra": ("格拉沃利讷 Gravelines", "法国", 50.99, 2.13, "eu"),
    "sbg": ("斯特拉斯堡 Strasbourg", "法国", 48.58, 7.75, "eu"),
    "par": ("巴黎 Paris（3-AZ）", "法国", 48.86, 2.35, "eu"),
    "fra": ("法兰克福 Frankfurt", "德国", 50.11, 8.68, "eu"),
    "lim": ("林堡 Limburg", "德国", 50.39, 8.06, "eu"),
    "lon": ("伦敦 London（Erith）", "英国", 51.48, 0.18, "eu"),
    "eri": ("伦敦 Erith", "英国", 51.48, 0.18, "eu"),
    "waw": ("华沙 Warsaw（Ożarów）", "波兰", 52.21, 20.80, "eu"),
    "bhs": ("博阿努瓦 Beauharnois", "加拿大", 45.32, -73.87, "na"),
    "tor": ("多伦多 Toronto", "加拿大", 43.65, -79.38, "na"),
    "vin": ("弗吉尼亚 Vint Hill", "美国", 38.75, -77.67, "na"),
    "hil": ("俄勒冈 Hillsboro", "美国", 45.52, -122.99, "na"),
    "sgp": ("新加坡 Singapore", "新加坡", 1.35, 103.82, "apac"),
    "syd": ("悉尼 Sydney", "澳大利亚", -33.87, 151.21, "apac"),
    "ynm": ("孟买 Mumbai", "印度", 19.08, 72.88, "apac"),
})
ALIASES = {"yyz": "tor", "mum": "ynm", "bom": "ynm"}
# Sites drawn even when nothing monitored lists them (the ones OVH's public availability API currently returns).
CORE = ["bhs", "tor", "vin", "hil", "gra", "rbx", "sbg", "par", "lon", "fra", "waw", "sgp", "syd", "ynm"]
ZONE_RE = re.compile(r"^[a-z]{2}-[a-z]+-([a-z]{3})-[a-z0-9]+$")   # eu-west-par-a -> par, ca-east-tor-a -> tor

# Colour levels, best first. key, label, CSS colour token.
LEVELS = [
    ("high", "现货充足", "green", "1H-high"),
    ("low", "少量现货", "amber", "1H-low / 24H"),
    ("wait", "可订·等交付", "blue", "72H / 240H / 480H …"),
    ("none", "无货", "red", "unavailable"),
    ("unknown", "未知 / 未监控", "gray", "unknown / comingSoon / 无数据"),
]
ORDER = {k: i for i, (k, *_r) in enumerate(LEVELS)}


def level(av: str | None) -> str:
    """OVH availability value -> map level (single source of truth)."""
    if av == "1H-high":
        return "high"
    if av in ("1H-low", "24H"):
        return "low"
    if av and re.fullmatch(r"\d+H", av):
        return "wait"
    if av == "unavailable":
        return "none"
    return "unknown"


def site_of(dc: str) -> str:
    dc = (dc or "").lower()
    m = ZONE_RE.match(dc)
    if m:
        dc = m.group(1)
    return ALIASES.get(dc, dc)


def legend() -> list[dict]:
    return [{"key": k, "label": tr(lb), "color": c, "values": tr(v)} for k, lb, c, v in LEVELS]


def aggregate(rows: list[dict], av_rank: dict) -> list[dict]:
    """rows: dicts with endpoint, plan, name, cpu, dc, availability. Returns one entry per site (CORE + any seen)."""
    sites: dict[str, dict] = {}

    def site(code: str) -> dict:
        s = sites.get(code)
        if s is None:
            city, country, lat, lon, region = SITES.get(code, (code.upper(), "", None, None, ""))
            s = sites[code] = {"code": code, "label": code.upper(), "city": city, "country": country, "lat": lat,
                               "lon": lon, "region": region, "zones": [], "monitored": False, "level": "unknown",
                               "counts": {k: 0 for k in ORDER}, "configs": 0, "_plans": {}}
        return s

    for code in CORE:
        site(code)
    for r in rows:
        dc = r.get("dc") or ""
        s = site(site_of(dc))
        if dc and dc not in s["zones"]:
            s["zones"].append(dc)
        av = r.get("availability") or "unknown"
        lv = level(av)
        s["monitored"] = True
        s["configs"] += 1
        s["counts"][lv] += 1
        if ORDER[lv] < ORDER[s["level"]]:
            s["level"] = lv
        key = (r.get("endpoint") or "", r.get("plan") or "")
        p = s["_plans"].get(key)
        if p is None:
            p = s["_plans"][key] = {"plan": key[1], "endpoint": key[0], "name": r.get("name") or key[1],
                                    "cpu": r.get("cpu") or "", "best": av, "level": lv, "inStock": 0, "total": 0}
        p["total"] += 1
        if lv in ("high", "low"):
            p["inStock"] += 1
        if av_rank.get(av, 6) < av_rank.get(p["best"], 6):
            p["best"], p["level"] = av, lv
    out = []
    for s in sites.values():
        plans = sorted(s.pop("_plans").values(), key=lambda p: (ORDER[p["level"]], -p["inStock"], p["name"]))
        s["plans"] = plans[:12]
        s["moreplans"] = max(0, len(plans) - 12)
        s["inStock"] = s["counts"]["high"] + s["counts"]["low"]
        if s["lat"] is not None:
            out.append(s)
    out.sort(key=lambda s: (ORDER[s["level"]], -s["inStock"], s["code"]))
    return out
