"""Fake OVH API for the read-only account view (orders + dedicated servers). mode: ok | denied | invalid | empty."""
import datetime as dt
import threading

import ovh.exceptions as oe


class _Resp:
    def __init__(self, code): self.status_code, self.headers = code, {"X-OVH-QueryID": "EU.fake-1"}


def _iso(days_ago, h=10):
    t = dt.datetime.now(dt.timezone(dt.timedelta(hours=1))) - dt.timedelta(days=days_ago)
    return t.replace(hour=h, minute=12, second=5, microsecond=0).isoformat()


def _day(days):
    return (dt.date.today() + dt.timedelta(days=days)).isoformat()


ORDERS = {
    231890455: dict(days=2, status="delivered", eur=140.20, desc=["Rise-1 | Intel Xeon-E 2386G", "Installation"], pay=("creditCard", 2)),
    231874012: dict(days=6, status="notPaid", eur=57.48, desc=["KS-LE-B | Intel Xeon-D 1521"], pay=None),
    231702334: dict(days=19, status="delivering", eur=96.59, desc=["Advance-1 Gen 2", "Bandwidth 1 Gbps"], pay=("paypal", 19)),
    231500118: dict(days=41, status="cancelled", eur=33.59, desc=["KS-A | Intel i7-6700k"], pay=None),
    231355790: dict(days=73, status="delivered", eur=26.39, desc=["Renouvellement ns31337.ip-203-0-113.eu"], pay=("creditCard", 73)),
    # real OVH style: every item twice (setup + rental), unordered, internal *001.001 cart refs
    231960777: dict(days=1, status="delivering", eur=21.24, desc=[
        ("2x SSD SATA 480GB Soft RAID rental - 1 month", "*001.001"), ("KS-LE-3 rental - datacenter bhs", "*001.001"),
        ("Intel Xeon E-2274G", "*001.001"), ("32GB DDR4 ECC 2666MHz", "*001.001"),
        ("2x SSD SATA 480GB Soft RAID", "*001.001"), ("KS-LE-3 rental - 1 month", "ns1234567.ip-203-0-113.net"),
        ("32GB DDR4 ECC 2666MHz rental - 1 month", "*001.001"), ("Bandwidth 500 Mbps", "*001.001")],
        pay=("creditCard", 1)),
    231960100: dict(days=9, status="delivered", eur=12.30, desc=["Invoice IE1234567"], pay=("creditCard", 9)),
    231950042: dict(days=12, status="delivered", eur=8.61, desc=["debt2016v1.payment.4242"], pay=("creditCard", 12)),
    231912345: dict(days=4, status="checking", eur=64.04, desc=["KS-5 | Intel Xeon-E3 1270 v6"], pay=("debtAccount", 4)),
}
SERVERS = {
    "ns3141592.ip-203-0-113.eu": dict(
        info=dict(name="ns3141592.ip-203-0-113.eu", state="ok", datacenter="gra3", os="debian12_64", ip="203.0.113.18",
                  reverse="ns3141592.ip-203-0-113.eu", commercialRange="rise-1", monitoring=True, rack="G317B07", linkSpeed=1000,
                  iam={"displayName": "rise-paris-01"}),
        si=dict(creation=_day(-2), expiration=_day(28), status="ok", renew={"automatic": True, "period": 1, "deleteAtExpiration": False}),
        hw=dict(processorName="Intel Xeon-E 2386G", numberOfProcessors=1, coresPerProcessor=6, threadsPerProcessor=12,
                memorySize={"unit": "MB", "value": 32768}, description="RISE-1 - Intel Xeon-E 2386G",
                diskGroups=[{"diskSize": {"unit": "GB", "value": 512}, "numberOfDisks": 2, "diskType": "NVME", "defaultHardwareRaidType": "N/A"}]),
        net=dict(bandwidth={"OvhToInternet": {"unit": "Mbps", "value": 1000}, "InternetToOvh": {"unit": "Mbps", "value": 1000}, "type": "standard"}),
        ips=["203.0.113.18/32", "2001:db8:403:3c76::/64"]),
    "ns5012345.ip-198-51-100.net": dict(
        info=dict(name="ns5012345.ip-198-51-100.net", state="error", datacenter="bhs6", os="ubuntu2204-server_64", ip="198.51.100.77",
                  reverse="backup.example.org", commercialRange="ks-5", monitoring=False, rack="B106A12", linkSpeed=100),
        si=dict(creation=_day(-340), expiration=_day(9), status="ok", renew={"automatic": False, "manualPayment": True}),
        hw=dict(processorName="Intel Xeon-D 1520", numberOfProcessors=1, coresPerProcessor=4, threadsPerProcessor=8,
                memorySize={"unit": "MB", "value": 16384},
                diskGroups=[{"diskSize": {"unit": "GB", "value": 2000}, "numberOfDisks": 4, "diskType": "SATA", "defaultHardwareRaidType": "raid5"}]),
        net=dict(bandwidth={"OvhToInternet": {"unit": "Mbps", "value": 250}, "InternetToOvh": {"unit": "Mbps", "value": 250}, "type": "standard"}),
        ips=["198.51.100.77/32"]),
}


class FakeAccountOVH:
    mode = "ok"
    calls: list = []
    lock = threading.Lock()
    inflight = 0
    max_inflight = 0
    delay = 0.0

    def __init__(self, *a, **k):
        self._timeout = (5, 20)

    # ---- minimal cart flow so DRY-RUN tasks on the demo server can preview a price (never checks out)
    carts: dict = {}
    _ids = iter(range(500, 10 ** 9))

    def post(self, path, **kw):
        parts = path.strip("/").split("/")
        if path == "/order/cart":
            cid = f"cart{next(self._ids)}"; self.carts[cid] = {"items": {}}
            return {"cartId": cid, "expire": "2099-01-01T00:00:00+00:00"}
        if parts[:2] != ["order", "cart"] or parts[2] not in self.carts:
            raise oe.ResourceNotFoundError(path)
        c = self.carts[parts[2]]
        if parts[3] == "assign":
            return None
        if parts[3] in ("eco", "baremetalServers"):
            iid = next(self._ids); c["items"][iid] = {"planCode": kw.get("planCode"), "cfg": {}}
            return {"itemId": iid}
        if parts[3] == "item" and parts[5] == "configuration":
            c["items"][int(parts[4])]["cfg"][kw["label"]] = kw["value"]; return {}
        if parts[3] == "checkout":
            raise oe.BadParametersError("demo server never checks out")
        raise oe.ResourceNotFoundError(path)

    def delete(self, path, **kw):
        self.carts.pop(path.strip("/").split("/")[2], None)

    def _cart_get(self, parts, path):
        c = self.carts.get(parts[2])
        if c is None:
            raise oe.ResourceNotFoundError(path)
        if len(parts) == 3:
            return {"items": list(c["items"]), "expire": "2099-01-01T00:00:00+00:00"}
        if parts[3] == "checkout":
            return {"prices": {"withTax": {"value": 140.20}, "withoutTax": {"value": 113.98}}}
        if parts[3] == "item":
            iid = int(parts[4]); it = c["items"][iid]
            if len(parts) == 5:
                return {"settings": {"planCode": it["planCode"]}, "prices": [{"label": "TOTAL", "price": {"value": 113.98}}]}
            if len(parts) == 6:
                return list(range(len(it["cfg"])))
            lab = list(it["cfg"])[int(parts[6])]
            return {"label": lab, "value": it["cfg"][lab]}
        raise oe.ResourceNotFoundError(path)

    def get(self, path, **kw):
        import time
        cls = type(self)
        parts = path.split("?")[0].strip("/").split("/")
        if parts[:2] == ["order", "cart"] and len(parts) > 2:
            return self._cart_get(parts, path)
        with cls.lock:
            cls.calls.append(path); cls.inflight += 1; cls.max_inflight = max(cls.max_inflight, cls.inflight)
        try:
            if cls.delay:
                time.sleep(cls.delay)
            return self._get(path, **kw)
        finally:
            with cls.lock:
                cls.inflight -= 1

    def _get(self, path, **kw):
        m = type(self).mode
        p = path.strip("/").split("/")
        if m == "invalid":
            raise oe.InvalidCredential("This credential is not valid", response=_Resp(403))
        if path == "/me":
            return {"nichandle": "ab12345-ovh", "currency": {"code": "EUR"}}
        if p[0] == "dedicated":
            if m == "denied":
                raise oe.NotGrantedCall("This call has not been granted", response=_Resp(403))
            if m == "empty" and len(p) == 2:
                return []
            if len(p) == 2:
                return list(SERVERS)
            s = SERVERS.get(p[2]) or self._404(path)
            rest = "/".join(p[3:])
            return {"": s["info"], "serviceInfos": s["si"], "specifications/hardware": s["hw"],
                    "specifications/network": s["net"], "ips": s["ips"]}.get(rest) or self._404(path)
        if p[:2] == ["me", "order"]:
            if len(p) == 2:
                assert "date.from" in kw
                return [] if m == "empty" else sorted(ORDERS)
            oid = int(p[2]); o = ORDERS.get(oid) or self._404(path)
            rest = "/".join(p[3:])
            if rest == "":
                return {"orderId": oid, "date": _iso(o["days"]), "expirationDate": _iso(o["days"] - 14),
                        "priceWithTax": {"value": o["eur"], "text": f"{o['eur']:.2f} €", "currencyCode": "EUR"},
                        "priceWithoutTax": {"value": round(o["eur"] / 1.23, 2), "text": f"{o['eur'] / 1.23:.2f} €"},
                        "url": f"https://www.ovh.com/cgi-bin/order/displayOrder.cgi?orderId={oid}&orderPassword=XXXX"}
            if rest == "status":
                return o["status"]
            if rest == "details":
                return [oid * 10 + i for i in range(len(o["desc"]))]
            if p[3] == "details" and len(p) == 5:
                i = int(p[4]) - oid * 10
                d = o["desc"][i]
                desc, dom = d if isinstance(d, tuple) else (d, "*")
                return {"description": desc, "domain": dom, "quantity": 1,
                        "totalPrice": {"text": f"{o['eur'] / len(o['desc']) / 1.23:.2f} €"}}
            if rest == "payment":
                if not o["pay"]:
                    self._404(path)
                return {"paymentType": o["pay"][0], "paymentDate": _iso(o["pay"][1], 11)}
        self._404(path)

    def _404(self, path):
        raise oe.ResourceNotFoundError(f"Got an invalid (or empty) URL: {path}")
