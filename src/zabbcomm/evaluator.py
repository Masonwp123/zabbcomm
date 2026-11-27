"""Org / site / device status from the registry. Pure: no I/O, no globals, `now` passed in."""

from collections.abc import Iterable
from dataclasses import dataclass

from .ids import site_network, slugify
from .registry import Device

ONLINE, OFFLINE, SUPPRESSED = 1, 0, 2
"""Status values. OFFLINE alerts in Zabbix; SUPPRESSED means a higher level (site or org)
is already reporting this outage."""

PENDING = -1
"""Stale, but it's too early to say why: the site or org may be going down or coming back.
Never sent, so Zabbix keeps the previous value."""


@dataclass(frozen=True)
class OrgStatus:
    org_id: str
    org_name: str
    status: int
    online: int
    total: int
    last_checkin: float


@dataclass(frozen=True)
class SiteStatus:
    org_id: str
    org_name: str
    site_id: str
    site_name: str
    status: int
    online: int
    total: int
    last_checkin: float


@dataclass(frozen=True)
class DeviceStatus:
    org_id: str
    org_name: str
    device_id: str
    device_name: str
    status: int
    wan_ip: str | None
    last_checkin: float


@dataclass(frozen=True)
class Evaluation:
    orgs: tuple[OrgStatus, ...]
    sites: tuple[SiteStatus, ...]
    devices: tuple[DeviceStatus, ...]


def evaluate(
    devices: Iterable[Device],
    now: float,
    *,
    device_timeout: float,
    confirm_window: float,
    site_min_devices: int,
    ipv6_prefix: int,
) -> Evaluation:
    """Cascade org -> site -> device status so each outage alerts once, at the highest
    level that is entirely down.

    Devices check in at different times, so during an outage (and the recovery from it)
    some look stale while others still look online. Something stale is only reported
    OFFLINE if its parent (site, else org) is *confirmed* up: a member checked in within
    `confirm_window` and has been doing so without a gap for at least that long.
    Otherwise it's PENDING while the parent has anything online, and SUPPRESSED once the
    parent is entirely down."""

    def is_confirmed(group: list[Device]) -> bool:
        return any(now - d.last_seen < confirm_window <= now - d.online_since for d in group)

    def stale_status(parent_up: bool, parent_confirmed: bool) -> int:
        if not parent_up:
            return SUPPRESSED
        return OFFLINE if parent_confirmed else PENDING

    by_org: dict[str, list[Device]] = {}
    for d in devices:
        by_org.setdefault(d.org_id, []).append(d)

    orgs: list[OrgStatus] = []
    sites: list[SiteStatus] = []
    statuses: list[DeviceStatus] = []
    for org_id, members in sorted(by_org.items()):
        latest = max(members, key=lambda d: d.last_seen)
        org_name = latest.org_name
        online = {d.device_id for d in members if now - d.last_seen <= device_timeout}
        org_up = bool(online)
        org_confirmed = is_confirmed(members)
        orgs.append(
            OrgStatus(
                org_id,
                org_name,
                ONLINE if org_up else OFFLINE,
                len(online),
                len(members),
                latest.last_seen,
            )
        )

        # A device belongs to the site of its latest WAN IP. An IP counts as a site if a
        # server is on it (servers stay put and stay on) or enough devices share it.
        by_network: dict[str, list[Device]] = {}
        for d in members:
            network = site_network(d.last_ip, ipv6_prefix)
            if network is not None:
                by_network.setdefault(network, []).append(d)

        # Latest check-in from each network by a device that has since moved elsewhere.
        departed: dict[str, float] = {}
        for d in members:
            previous = site_network(d.prev_ip, ipv6_prefix)
            if previous is None or d.prev_seen is None:
                continue
            if previous != site_network(d.last_ip, ipv6_prefix):
                departed[previous] = max(d.prev_seen, departed.get(previous, d.prev_seen))

        # device_id -> (is its site up, is it confirmed up), for sited devices only
        parents: dict[str, tuple[bool, bool]] = {}
        for network, site_members in sorted(by_network.items()):
            has_server = any(d.is_server for d in site_members)
            if not has_server and len(site_members) < site_min_devices:
                continue
            site_online = sum(d.device_id in online for d in site_members)
            site_up = site_online > 0
            newest = max(d.last_seen for d in site_members)
            # Everyone left here went quiet long before others last used this IP and
            # moved on (e.g. an ISP address change): leftovers, not a site outage.
            if not site_up and departed.get(network, newest) - newest >= confirm_window:
                continue
            site_status = ONLINE if site_up else stale_status(org_up, org_confirmed)
            sites.append(
                SiteStatus(
                    org_id,
                    org_name,
                    slugify(network),
                    network,
                    site_status,
                    site_online,
                    len(site_members),
                    newest,
                )
            )
            site_confirmed = is_confirmed(site_members)
            for d in site_members:
                parents[d.device_id] = (site_up, site_confirmed)

        for d in sorted(members, key=lambda d: d.device_id):
            if d.device_id in online:
                device_status = ONLINE
            else:
                device_status = stale_status(*parents.get(d.device_id, (org_up, org_confirmed)))
            statuses.append(
                DeviceStatus(
                    org_id,
                    org_name,
                    d.device_id,
                    d.device_name,
                    device_status,
                    d.last_ip,
                    d.last_seen,
                )
            )

    return Evaluation(tuple(orgs), tuple(sites), tuple(statuses))
