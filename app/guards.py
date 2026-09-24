import ipaddress
import socket
from typing import List
from urllib.parse import urlsplit

from fastapi import HTTPException

# Networks that must never be reachable from the metadata fetch. This is a
# basic SSRF guard: when asked to fetch an arbitrary URL, refuse targets whose
# resolved addresses live on private/reserved space (or the cloud metadata
# endpoint 169.254.169.254).
_BLOCKED_NETWORKS: List[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
]


def _is_blocked(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(address in net for net in _BLOCKED_NETWORKS)


def _resolve_host(hostname: str) -> List[str]:
    """Return every address the hostname resolves to (IPv4 + IPv6)."""
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise HTTPException(status_code=400, detail=f"Could not resolve host: {hostname}") from exc
    return [info[4][0] for info in infos]


def validate_public_url(raw: str) -> str:
    """Validate the URL is http(s) and its host resolves to a public address."""
    cleaned = raw.strip()
    parts = urlsplit(cleaned)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise HTTPException(status_code=400, detail="URL must be an absolute http(s) URL.")
    hostname = parts.hostname
    if not hostname:
        raise HTTPException(status_code=400, detail="URL is missing a host.")

    addresses = _resolve_host(hostname)
    if not addresses:
        raise HTTPException(status_code=400, detail=f"No addresses for host: {hostname}")

    public = [addr for addr in addresses if not _is_blocked(ipaddress.ip_address(addr))]
    if not public:
        raise HTTPException(
            status_code=400,
            detail=f"Host resolves only to private/reserved addresses: {hostname}",
        )

    return cleaned