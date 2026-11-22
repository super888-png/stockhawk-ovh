"""SSRF guard for user-supplied URLs (webhooks): public http(s) only, resolved IP pinned for the request."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

import httpx
from .i18n import T, tr

METADATA = {ipaddress.ip_address(x) for x in ("169.254.169.254", "100.100.100.200", "192.0.0.192", "fd00:ec2::254")}


class Blocked(ValueError):
    pass


def _bad_ip(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (ip in METADATA or not ip.is_global or ip.is_multicast or ip.is_loopback or ip.is_link_local
            or ip.is_private or ip.is_reserved or ip.is_unspecified)


def resolve_public(url: str) -> tuple[str, str, int, str]:
    """-> (scheme, host, port, ip). Raises Blocked for anything not a public http(s) address."""
    try:
        u = urlsplit(url.strip())
    except Exception:
        raise Blocked(T("URL 无法解析"))
    if u.scheme not in ("http", "https") or not u.hostname:
        raise Blocked(T("只允许 http(s) 地址"))
    if u.username or u.password:
        raise Blocked(T("URL 中不能包含账号密码"))
    try:
        port = u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        raise Blocked(T("端口无效"))
    host = u.hostname.rstrip(".").lower()
    if host in ("localhost",) or host.endswith((".localhost", ".local", ".internal")):
        raise Blocked(T("不允许内网 / 本机地址"))
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise Blocked(T("域名无法解析"))
    ips = {ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos}
    if not ips or any(_bad_ip(ip) for ip in ips):
        raise Blocked(T("不允许内网、本机、保留或云元数据地址"))
    ip = sorted(ips, key=lambda x: x.version)[0]          # prefer IPv4
    return u.scheme, host, port, str(ip)


def check_url(url: str) -> str | None:
    try:
        resolve_public(url)
        return None
    except Blocked as e:
        return str(e)


def post_json(url: str, payload: dict, timeout: float = 10) -> httpx.Response:
    """POST to a vetted public URL, connecting to the IP that was checked (no DNS-rebinding window),
    with TLS verified against the original host name and redirects disabled."""
    scheme, host, port, ip = resolve_public(url)
    u = urlsplit(url.strip())
    netloc_ip = f"[{ip}]" if ":" in ip else ip
    pinned = urlunsplit((scheme, f"{netloc_ip}:{port}", u.path or "/", u.query, ""))
    headers = {"Host": host if port in (80, 443) else f"{host}:{port}", "User-Agent": "ovh-sniper-web/1.4"}
    ext = {"sni_hostname": host} if scheme == "https" else {}
    with httpx.Client(timeout=timeout, follow_redirects=False) as c:
        req = c.build_request("POST", pinned, json=payload, headers=headers, extensions=ext)
        return c.send(req)
