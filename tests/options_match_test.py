"""Hotfix v1.6: availability RAM/disk codes vs catalog addon codes, with the real OVH data that broke a task.
KS-LE-3 (27skle03-v1, eco, ovh-eu/IE, checked 2026-11-21): the availability API lists ram-64g-ddr4-ecc-2666 in stock
(FRA/GRA 1H-low) but the IE/FR/GB/DE/ES/PL/NL/IT catalogs AND /order/cart/{id}/eco/options only offer
ram-32g-ddr4-ecc-2666-27skle03-v1. Offline.   python tests/options_match_test.py"""
import os, sys
os.environ.setdefault("SECRET_KEY", "x" * 48)
os.environ.setdefault("DATA_DIR", "/tmp/ovh-opt-test")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import ovh_engine as E

FAILS = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond: FAILS.append(name)

KSLE3 = {"planCode": "27skle03-v1", "invoiceName": "KS-LE-3", "addonFamilies": [
    {"name": "bandwidth", "mandatory": True, "default": "bandwidth-500-ks-gen0", "addons": ["bandwidth-500-ks-gen0"]},
    {"name": "memory", "mandatory": True, "default": "ram-32g-ddr4-ecc-2666-27skle03-v1", "addons": ["ram-32g-ddr4-ecc-2666-27skle03-v1"]},
    {"name": "storage", "mandatory": True, "default": "softraid-2x4000sa-27skle03-v1", "addons": [
        "softraid-2x4000sa-27skle03-v1", "hybridsoftraid-2x8000sa-2x480ssd-27skle03-v1", "softraid-3x4000sa-27skle03-v1"]}],
    "configurations": [{"name": "dedicated_datacenter", "values": ["bhs", "fra", "gra", "lon", "rbx", "sbg", "waw"]},
                       {"name": "region", "values": ["europe", "canada"]}]}
AVAIL = [  # real /dedicated/server/datacenter/availabilities?planCode=27skle03-v1 rows (trimmed)
    {"planCode": "27skle03-v1", "fqn": "27skle03-v1.ram-32g-ddr4-ecc-2666.softraid-3x4000sa", "memory": "ram-32g-ddr4-ecc-2666",
     "storage": "softraid-3x4000sa", "datacenters": [{"datacenter": "gra", "availability": "unavailable"}, {"datacenter": "fra", "availability": "unavailable"}]},
    {"planCode": "27skle03-v1", "fqn": "27skle03-v1.ram-64g-ddr4-ecc-2666.softraid-3x4000sa", "memory": "ram-64g-ddr4-ecc-2666",
     "storage": "softraid-3x4000sa", "datacenters": [{"datacenter": "fra", "availability": "1H-low"}, {"datacenter": "gra", "availability": "1H-low"}]},
]

# matching
check("32G maps to the suffixed catalog addon", E.addon_code(KSLE3, "memory", "ram-32g-ddr4-ecc-2666", synthesize=False) == "ram-32g-ddr4-ecc-2666-27skle03-v1")
check("3x4TB maps", E.addon_code(KSLE3, "storage", "softraid-3x4000sa", synthesize=False) == "softraid-3x4000sa-27skle03-v1")
check("hybrid maps", E.addon_code(KSLE3, "storage", "hybridsoftraid-2x8000sa-2x480ssd", synthesize=False) == "hybridsoftraid-2x8000sa-2x480ssd-27skle03-v1")
check("64G has no addon (never falls back to 32G)", E.addon_code(KSLE3, "memory", "ram-64g-ddr4-ecc-2666", synthesize=False) is None)
check("ddr token difference tolerated", E.addon_code(KSLE3, "memory", "ram-32g-ecc-2666", synthesize=False) == "ram-32g-ddr4-ecc-2666-27skle03-v1")
RISE = {"planCode": "24rise01-v2", "addonFamilies": [{"name": "memory", "mandatory": True, "default": "ram-32g-ecc-3200-24rise01-v2",
        "addons": ["ram-32g-ecc-3200-24rise01-v2", "ram-64g-ecc-3200-24rise01-v2"]}]}
check("reverse ddr difference tolerated", E.addon_code(RISE, "memory", "ram-64g-ddr4-ecc-3200", synthesize=False) == "ram-64g-ecc-3200-24rise01-v2")
check("different speed not matched", E.addon_code(RISE, "memory", "ram-64g-ecc-2400", synthesize=False) is None)
check("2x4TB is not 3x4TB", E.addon_code(KSLE3, "storage", "softraid-2x4000sa", synthesize=False) == "softraid-2x4000sa-27skle03-v1")

# orderable + stock rows
ok, why = E.orderable(KSLE3, "ram-64g-ddr4-ecc-2666", "softraid-3x4000sa")
check("64G variant flagged not orderable with reason", not ok and "64 GB" in why and "32 GB" in why, why)
check("32G variant orderable", E.orderable(KSLE3, "ram-32g-ddr4-ecc-2666", "softraid-3x4000sa")[0])
rows = E.live_stock("27skle03-v1", KSLE3, AVAIL)
check("live_stock: one in-stock row, not orderable", len(rows) == 1 and rows[0]["orderable"] is False, rows)
check("pick_stock never picks it (any config)", E.pick_stock(rows, ["gra", "fra"], True, None, None) is None)
check("pick_stock never picks it (pinned 64G)", E.pick_stock(rows, ["gra"], False, "ram-64g-ddr4-ecc-2666", "softraid-3x4000sa") is None)
msg = E.explain_no_match(rows, ["gra"], False, "ram-32g-ddr4-ecc-2666", "softraid-3x4000sa")
check("heartbeat explains: in stock only as non-orderable 64G", "64 GB" in msg and "无法下单" in msg and "GRA" in msg, msg)
check("STOCK line marks it", "不可下单" in E.format_row(KSLE3, rows[0]))
# orderable non-selected config explanation
rows2 = E.live_stock("27skle03-v1", KSLE3, [dict(AVAIL[0], datacenters=[{"datacenter": "gra", "availability": "1H-low"}])])
check("orderable non-default row: explains 'not selected disk'", "不是所选硬盘" in E.explain_no_match(rows2, [], False, None, "softraid-2x4000sa"))
check("orderable non-default row: explains any_config off", "允许非基础配置" in E.explain_no_match(rows2, [], False, None, None))
check("orderable non-default row picked with any_config", (E.pick_stock(rows2, [], True, None, None) or {}).get("dc") == "gra")
# cart addon resolution: pinned 32G works, pinned 64G raises a clear error (no silent 32G substitution)
spec = E.OrderSpec(subsidiary="IE", catalog_type="eco", plan_obj=KSLE3, os_code="none_64.en", duration="P1M", pricing_mode="default",
                   quantity=1, bandwidth="", extra_addons=[], price_cap=0, auto_pay=False, vat_rate=1.23, addon_pricing={})
check("cart codes for 32G/3x4TB", spec.addon_codes("ram-32g-ddr4-ecc-2666", "softraid-3x4000sa") ==
      ["bandwidth-500-ks-gen0", "ram-32g-ddr4-ecc-2666-27skle03-v1", "softraid-3x4000sa-27skle03-v1"], spec.addon_codes("ram-32g-ddr4-ecc-2666", "softraid-3x4000sa"))
try:
    spec.addon_codes("ram-64g-ddr4-ecc-2666", "softraid-3x4000sa"); check("64G cart refused", False)
except RuntimeError as e:
    check("64G cart refused with clear error", "memory" in str(e), e)

print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
