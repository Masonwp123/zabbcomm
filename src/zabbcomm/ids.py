"""Stable IDs for names, and the WAN network a check-in came from."""

import hashlib
import ipaddress
import re
import unicodedata

_NON_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Stable ID for a name: casefolded, accents removed, runs of other chars become '-'.

    If a letter or digit has no ASCII form (e.g. non-Latin scripts, or 'ø'), dropping it
    could merge two different names, so a short hash of the whole name is appended.
    """
    # Casefold again after NFKD, which can produce capitals (e.g. '№' -> 'No').
    folded = unicodedata.normalize("NFKD", name.casefold()).casefold()
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    slug = _NON_SLUG.sub("-", folded).strip("-")
    lossy = any(c.isalnum() and not c.isascii() for c in folded)
    if slug and not lossy:
        return slug
    canonical = unicodedata.normalize("NFC", name.casefold())
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:12]
    return f"{slug or 'h'}-{digest}"


def site_network(ip: str | None, ipv6_prefix: int) -> str | None:
    """The WAN network a check-in came from: the IPv4 address itself, or the IPv6 network
    of `ipv6_prefix` bits. None if `ip` isn't an IP address (e.g. a test client)."""
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if isinstance(addr, ipaddress.IPv4Address):
        return str(addr)
    return str(ipaddress.IPv6Network((addr, ipv6_prefix), strict=False))
