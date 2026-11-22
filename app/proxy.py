"""Run behind Cloudflare (orange cloud, any SSL mode incl. Flexible) without trusting spoofable headers.

Only when the TCP peer is a Cloudflare edge (published ranges below, or CF_IPS in .env) do we take
  * the visitor IP from CF-Connecting-IP  -> rate limits, login throttling and the audit log see the real client
  * the scheme from X-Forwarded-Proto / CF-Visitor -> request.url is https, redirects stay on https
Anyone reaching the port directly keeps their own socket address and scheme, so direct http://IP:8000 still works.

Cookies get the Secure flag per request whenever the effective scheme is https (COOKIE_SECURE=1 forces it always).
Enable with TRUST_CLOUDFLARE=1.
"""
import ipaddress
import json
import os

# https://www.cloudflare.com/ips/ (fetched 2026-11-21)
CLOUDFLARE_IPS = """
173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22 141.101.64.0/18 108.162.192.0/18
190.93.240.0/20 188.114.96.0/20 197.234.240.0/22 198.41.128.0/17 162.158.0.0/15 104.16.0.0/13
104.24.0.0/14 172.64.0.0/13 131.0.72.0/22
2400:cb00::/32 2606:4700::/32 2803:f800::/32 2405:b500::/32 2405:8100::/32 2a06:98c0::/29 2c0f:f248::/32
"""


def _nets(spec: str):
    out = []
    for tok in spec.replace(",", " ").split():
        try:
            out.append(ipaddress.ip_network(tok, strict=False))
        except ValueError:
            pass
    return out


def _ip(s: str):
    try:
        ip = ipaddress.ip_address(s)
        return ip.ipv4_mapped or ip if ip.version == 6 else ip
    except ValueError:
        return None


class CloudflareProxy:
    def __init__(self, app, enabled: bool | None = None, nets: str | None = None, force_secure: bool = False):
        self.app = app
        self.enabled = os.environ.get("TRUST_CLOUDFLARE", "0") == "1" if enabled is None else enabled
        self.nets = _nets(nets if nets is not None else (os.environ.get("CF_IPS") or CLOUDFLARE_IPS))
        self.force_secure = force_secure

    def from_cloudflare(self, host: str) -> bool:
        ip = _ip(host or "")
        return bool(ip) and any(ip in n for n in self.nets if n.version == ip.version)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        if self.enabled and scope.get("client") and self.from_cloudflare(scope["client"][0]):
            h = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            real = _ip(h.get("cf-connecting-ip", "").strip())
            scope = dict(scope)
            scope["state"] = {**scope.get("state", {}), "via_cloudflare": True}
            if real:
                scope["client"] = (str(real), 0)
            proto = h.get("x-forwarded-proto", "").split(",")[0].strip().lower()
            try:
                proto = json.loads(h["cf-visitor"]).get("scheme", proto) if "cf-visitor" in h else proto
            except (ValueError, AttributeError):
                pass
            if proto in ("http", "https"):
                scope["scheme"] = proto if scope["type"] == "http" else ("wss" if proto == "https" else "ws")
        secure = self.force_secure or scope.get("scheme") in ("https", "wss")
        if not secure:
            return await self.app(scope, receive, send)

        async def send_secure(msg):
            if msg["type"] == "http.response.start":
                hdrs = []
                for k, v in msg.get("headers", []):
                    if k.lower() == b"set-cookie" and b"secure" not in [p.strip().lower() for p in v.split(b";")]:
                        v = v + b"; Secure"
                    hdrs.append((k, v))
                msg = {**msg, "headers": hdrs}
            await send(msg)
        return await self.app(scope, receive, send_secure)
