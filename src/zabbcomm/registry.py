"""The app's only mutable state: which devices have checked in, when and from where."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .ids import site_network, slugify


@dataclass
class Device:
    org_id: str
    device_id: str
    org_name: str
    device_name: str
    last_seen: float
    last_ip: str | None = None
    agent_version: str | None = None
    is_server: bool = False
    online_since: float = 0.0  # start of the current run of check-ins without a gap
    prev_ip: str | None = None  # an IP on the WAN network used before last_ip's ...
    prev_seen: float | None = None  # ... and the last check-in from that network


class LimitReachedError(Exception):
    """A check-in would exceed MAX_ORGS or MAX_DEVICES_PER_ORG."""


class Registry:
    """org_id -> device_id -> Device.

    Only touch it from code running on the event loop with no `await` in between
    reads and writes; that is what makes it safe without locks.
    """

    def __init__(
        self,
        max_orgs: int,
        max_devices_per_org: int,
        confirm_window: float,
        ipv6_prefix: int = 64,
    ) -> None:
        self.max_orgs = max_orgs
        self.max_devices_per_org = max_devices_per_org
        self.confirm_window = confirm_window
        self.ipv6_prefix = ipv6_prefix
        self._orgs: dict[str, dict[str, Device]] = {}

    def check_in(
        self,
        org: str,
        device: str,
        ip: str | None,
        agent_version: str | None,
        now: float,
        is_server: bool | None = None,
    ) -> Device:
        org_id, device_id = slugify(org), slugify(device)
        org_devices = self._orgs.get(org_id)
        if org_devices is None:
            if len(self._orgs) >= self.max_orgs:
                raise LimitReachedError(f"MAX_ORGS ({self.max_orgs}) reached")
            org_devices = self._orgs[org_id] = {}
        record = org_devices.get(device_id)
        if record is None:
            if len(org_devices) >= self.max_devices_per_org:
                raise LimitReachedError(
                    f"MAX_DEVICES_PER_ORG ({self.max_devices_per_org}) reached for {org_id}"
                )
            record = org_devices[device_id] = Device(
                org_id, device_id, org, device, now, online_since=now
            )
        else:
            if now - record.last_seen >= self.confirm_window:
                record.online_since = now
            # Only a move to another network counts. IPv6 privacy addresses change
            # within a network all the time, and would erase where the device came from.
            old = site_network(record.last_ip, self.ipv6_prefix)
            if old is not None and old != site_network(ip, self.ipv6_prefix):
                record.prev_ip, record.prev_seen = record.last_ip, record.last_seen
        record.org_name, record.device_name = org, device
        record.last_seen, record.last_ip, record.agent_version = now, ip, agent_version
        if is_server is not None:  # unknown keeps what we already knew
            record.is_server = is_server
        return record

    def forget(self, now: float, after: float) -> list[Device]:
        """Drop devices silent for longer than `after` seconds (and orgs left empty)."""
        return self.remove_where(lambda d: now - d.last_seen > after)

    def remove_denied(self, denies: Callable[[str, str], bool]) -> list[Device]:
        """Drop every device for which `denies(org_id, device_id)` is true."""
        return self.remove_where(lambda d: denies(d.org_id, d.device_id))

    def remove_where(self, matches: Callable[[Device], bool]) -> list[Device]:
        removed: list[Device] = []
        for org_id, org_devices in list(self._orgs.items()):
            for device_id, record in list(org_devices.items()):
                if matches(record):
                    removed.append(org_devices.pop(device_id))
            if not org_devices:
                del self._orgs[org_id]
        return removed

    def devices(self) -> list[Device]:
        return [d for org_devices in self._orgs.values() for d in org_devices.values()]

    def replace_all(self, devices: Iterable[Device]) -> None:
        """Load state (e.g. from a snapshot). Caps are not applied to loaded data.

        IDs are recomputed from the names, so a snapshot written under older ID rules
        can't leave a stale duplicate behind."""
        self._orgs = {}
        for d in sorted(devices, key=lambda d: d.last_seen):  # latest wins a clash
            d.org_id, d.device_id = slugify(d.org_name), slugify(d.device_name)
            self._orgs.setdefault(d.org_id, {})[d.device_id] = d
