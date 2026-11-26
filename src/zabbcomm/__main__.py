"""zabbcomm: turn Syncro endpoint check-ins into Zabbix org / site / device status.

Sections, top to bottom: settings, IDs and sites, registry, evaluator, Zabbix payloads,
senders, persistence, monitor loop, FastAPI app, entry point. Everything up to
"Zabbix payloads" is pure and takes `now` explicitly, so tests never sleep.

Importing this module has no side effects: settings are only read in `create_app()` /
`main()`.
"""

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import tempfile
import time
import unicodedata
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from zabbix_utils import AsyncSender, ItemValue

log = logging.getLogger("zabbcomm")


# --------------------------------------------------------------------------- settings

_HOSTNAME = re.compile(r"[A-Za-z0-9._-]+")


class Settings(BaseSettings):
    """Configuration from environment variables (see .claude/CLAUDE.md for meanings)."""

    model_config = SettingsConfigDict(extra="ignore")

    app_url: str | None = None
    host: str = "0.0.0.0"
    port: int = Field(8000, ge=1, le=65535)
    api_token: SecretStr = Field(min_length=16)
    zabbix_server: str
    zabbix_port: int = Field(10051, ge=1, le=65535)
    zabbix_host: str = "zabbcomm"
    zabbix_dry_run: bool = False
    checkin_interval: int = Field(900, gt=0)
    device_timeout: int = Field(2100, gt=0)
    eval_interval: int = Field(60, gt=0)
    discovery_interval: int = Field(3600, gt=0)
    device_forget_after: int = Field(604800, gt=0)
    site_min_devices: int = Field(2, ge=1)
    site_ipv6_prefix: int = Field(64, ge=0, le=128)
    max_orgs: int = Field(500, ge=1)
    max_devices_per_org: int = Field(1000, ge=1)
    data_dir: Path = Path("/data")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("zabbix_server")
    @classmethod
    def _bare_host(cls, value: str) -> str:
        # zabbix_utils splits "server:port" on ":", so any other colon crashes it.
        value = value.strip()
        if not _HOSTNAME.fullmatch(value):
            raise ValueError(
                "must be just a hostname or IPv4 address, without http://, a port "
                "(use ZABBIX_PORT) or a path"
            )
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _timings(self) -> Self:
        # Either mistake would silently stop offline alerts from ever being sent.
        if self.device_timeout <= 2 * self.checkin_interval:
            raise ValueError("DEVICE_TIMEOUT must be more than twice CHECKIN_INTERVAL")
        if self.device_forget_after < 2 * self.device_timeout:
            raise ValueError("DEVICE_FORGET_AFTER must be at least twice DEVICE_TIMEOUT")
        return self

    @property
    def confirm_window(self) -> int:
        """How recent a check-in must be, and how long a device must have been checking
        in without a gap, before it can vouch that its site or org is up (see
        `evaluate`).

        It has to be longer than the gap between healthy check-ins, and shorter than
        DEVICE_TIMEOUT minus that gap, so nothing in a dead group still qualifies when
        its first device looks stale. Half of DEVICE_TIMEOUT leaves the same room for
        late check-ins on both sides: DEVICE_TIMEOUT / 2 - CHECKIN_INTERVAL."""
        return self.device_timeout // 2


def load_settings() -> Settings:
    """Read Settings from the environment, failing with a message that never echoes input
    values (a too-short API_TOKEN would otherwise be printed in the validation error)."""
    try:
        return Settings()  # type: ignore[call-arg]  # required fields come from the env
    except ValidationError as exc:
        problems = "\n".join(
            "  " + ": ".join(filter(None, [".".join(map(str, e["loc"])).upper(), e["msg"]]))
            for e in exc.errors(include_input=False, include_url=False)
        )
        raise SystemExit(f"zabbcomm: invalid configuration:\n{problems}") from None


# ---------------------------------------------------------------------- IDs and sites

_NON_SLUG = re.compile(r"[^a-z0-9]+")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


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


# --------------------------------------------------------------------------- registry


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
    prev_ip: str | None = None  # the WAN IP used before last_ip ...
    prev_seen: float | None = None  # ... and the last check-in from it


class LimitReachedError(Exception):
    """A check-in would exceed MAX_ORGS or MAX_DEVICES_PER_ORG."""


class Registry:
    """The app's only mutable state: org_id -> device_id -> Device.

    Only touch it from code running on the event loop with no `await` in between
    reads and writes; that is what makes it safe without locks.
    """

    def __init__(self, max_orgs: int, max_devices_per_org: int, confirm_window: float) -> None:
        self.max_orgs = max_orgs
        self.max_devices_per_org = max_devices_per_org
        self.confirm_window = confirm_window
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
            if record.last_ip is not None and ip != record.last_ip:
                record.prev_ip, record.prev_seen = record.last_ip, record.last_seen
        record.org_name, record.device_name = org, device
        record.last_seen, record.last_ip, record.agent_version = now, ip, agent_version
        if is_server is not None:  # unknown keeps what we already knew
            record.is_server = is_server
        return record

    def forget(self, now: float, after: float) -> list[Device]:
        """Drop devices silent for longer than `after` seconds (and orgs left empty)."""
        forgotten: list[Device] = []
        for org_id, org_devices in list(self._orgs.items()):
            for device_id, record in list(org_devices.items()):
                if now - record.last_seen > after:
                    forgotten.append(org_devices.pop(device_id))
            if not org_devices:
                del self._orgs[org_id]
        return forgotten

    def devices(self) -> list[Device]:
        return [d for org_devices in self._orgs.values() for d in org_devices.values()]

    def remove_where(self, matches: Callable[[str, str], bool]) -> list[Device]:
        """Drop every device for which `matches(org_id, device_id)` is true."""
        removed: list[Device] = []
        for org_id, org_devices in list(self._orgs.items()):
            for device_id in list(org_devices):
                if matches(org_id, device_id):
                    removed.append(org_devices.pop(device_id))
            if not org_devices:
                del self._orgs[org_id]
        return removed

    def replace_all(self, devices: Iterable[Device]) -> None:
        """Load state (e.g. from a snapshot). Caps are not applied to loaded data.

        IDs are recomputed from the names, so a snapshot written under older ID rules
        can't leave a stale duplicate behind."""
        self._orgs = {}
        for d in sorted(devices, key=lambda d: d.last_seen):  # latest wins a clash
            d.org_id, d.device_id = slugify(d.org_name), slugify(d.device_name)
            self._orgs.setdefault(d.org_id, {})[d.device_id] = d


# -------------------------------------------------------------------------- evaluator

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
    level that is entirely down. Pure: no I/O, no globals.

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


# -------------------------------------------------------------------- Zabbix payloads
# Every Zabbix key string lives here. Keys only ever contain slug IDs ([a-z0-9-]), so
# they never need key-parameter quoting. Changing a key is a breaking change in Zabbix.

KEY_ALIVE = "zabbcomm.alive"
KEY_ORG_DISCOVERY = "zabbcomm.org.discovery"
KEY_SITE_DISCOVERY = "zabbcomm.site.discovery"
KEY_DEVICE_DISCOVERY = "zabbcomm.device.discovery"


def org_key(metric: str, org_id: str) -> str:
    return f"zabbcomm.org.{metric}[{org_id}]"


def site_key(metric: str, org_id: str, site_id: str) -> str:
    return f"zabbcomm.site.{metric}[{org_id},{site_id}]"


def device_key(metric: str, org_id: str, device_id: str) -> str:
    return f"zabbcomm.device.{metric}[{org_id},{device_id}]"


def _lld(rows: list[dict[str, str]]) -> str:
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


def build_discovery(ev: Evaluation) -> dict[str, str]:
    """LLD rule key -> discovery JSON for that rule."""
    return {
        KEY_ORG_DISCOVERY: _lld([{"{#ORGID}": o.org_id, "{#ORG}": o.org_name} for o in ev.orgs]),
        KEY_SITE_DISCOVERY: _lld(
            [
                {
                    "{#ORGID}": s.org_id,
                    "{#ORG}": s.org_name,
                    "{#SITEID}": s.site_id,
                    "{#SITE}": s.site_name,
                }
                for s in ev.sites
            ]
        ),
        KEY_DEVICE_DISCOVERY: _lld(
            [
                {
                    "{#ORGID}": d.org_id,
                    "{#ORG}": d.org_name,
                    "{#DEVICEID}": d.device_id,
                    "{#DEVICE}": d.device_name,
                }
                for d in ev.devices
            ]
        ),
    }


def build_values(ev: Evaluation, *, in_grace: bool) -> list[tuple[str, str]]:
    """(key, value) pairs for one cycle. During the startup grace period only ONLINE
    statuses are sent: stale-looking devices may just reflect the app having been down."""
    values = [(KEY_ALIVE, "1")]

    def add_status(key: str, value: int) -> None:
        if value != PENDING and (value == ONLINE or not in_grace):
            values.append((key, str(value)))

    for o in ev.orgs:
        add_status(org_key("status", o.org_id), o.status)
        values.append((org_key("devices.online", o.org_id), str(o.online)))
        values.append((org_key("devices.total", o.org_id), str(o.total)))
        values.append((org_key("last_checkin", o.org_id), str(int(o.last_checkin))))
    for s in ev.sites:
        add_status(site_key("status", s.org_id, s.site_id), s.status)
        values.append((site_key("devices.online", s.org_id, s.site_id), str(s.online)))
        values.append((site_key("devices.total", s.org_id, s.site_id), str(s.total)))
        values.append((site_key("last_checkin", s.org_id, s.site_id), str(int(s.last_checkin))))
    for d in ev.devices:
        add_status(device_key("status", d.org_id, d.device_id), d.status)
        last_checkin = str(int(d.last_checkin))
        values.append((device_key("last_checkin", d.org_id, d.device_id), last_checkin))
        if d.wan_ip:
            values.append((device_key("wan_ip", d.org_id, d.device_id), d.wan_ip))
    return values


# ---------------------------------------------------------------------------- senders


@dataclass(frozen=True)
class SendResult:
    processed: int
    failed: int


class Sender(Protocol):
    async def send(self, host: str, values: list[tuple[str, str]], clock: int) -> SendResult: ...


CHUNK_SIZE = 250
SEND_TIMEOUT = 30
"""Seconds allowed for each chunk. zabbix_utils has no timeout on reading the reply, so
without this a server that never answers would stall monitoring for good."""


class ZabbixSender:
    """Zabbix sender protocol (trapper, TCP 10051). Sends in chunks of 250, in order."""

    def __init__(self, server: str, port: int, timeout: float = SEND_TIMEOUT) -> None:
        self._sender = AsyncSender(server=server, port=port, chunk_size=CHUNK_SIZE)
        self.timeout = timeout

    async def send(self, host: str, values: list[tuple[str, str]], clock: int) -> SendResult:
        items = [ItemValue(host, key, value, clock) for key, value in values]
        processed = failed = 0
        for start in range(0, len(items), CHUNK_SIZE):
            async with asyncio.timeout(self.timeout):
                response = await self._sender.send(items[start : start + CHUNK_SIZE])
            processed += response.processed
            failed += response.failed
        return SendResult(processed=processed, failed=failed)


class DryRunSender:
    """ZABBIX_DRY_RUN: log what would be sent instead of sending it."""

    async def send(self, host: str, values: list[tuple[str, str]], clock: int) -> SendResult:
        for key, value in values:
            log.info("dry-run %s %s = %s", host, key, value)
        return SendResult(processed=len(values), failed=0)


# ------------------------------------------------------------------------ persistence

SNAPSHOT_VERSION = 1


class SnapshotStore:
    """Registry snapshot as JSON, written atomically (temp file + os.replace)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> list[Device]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return [Device(**d) for d in raw["devices"]]
        except FileNotFoundError:
            return []
        except OSError, ValueError, KeyError, TypeError:
            aside = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
            log.exception(
                "unreadable snapshot %s; moving it to %s, starting empty", self.path, aside
            )
            with contextlib.suppress(OSError):
                os.replace(self.path, aside)
            return []

    def save(self, devices: list[dict[str, object]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".registry-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": SNAPSHOT_VERSION, "devices": devices}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


# -------------------------------------------------------------------------- deny list


class DeniedDevice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org: str
    device: str


class DenyFile(BaseModel):
    # Unknown keys are errors, so a typo is reported instead of silently doing nothing.
    model_config = ConfigDict(extra="forbid")

    orgs: list[str] = []
    devices: list[DeniedDevice] = []


class DenyList:
    """Orgs and devices whose agents are told to uninstall: DATA_DIR/deny.json, re-read
    whenever it changes. A missing or invalid file denies nobody."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._stamp: tuple[int, int] | None = None
        self._orgs: frozenset[str] = frozenset()
        self._devices: frozenset[tuple[str, str]] = frozenset()

    def refresh(self) -> None:
        try:
            info = self.path.stat()
            stamp = (info.st_mtime_ns, info.st_size)
        except OSError:
            stamp = None
        if stamp == self._stamp:
            return
        self._stamp = stamp
        content = DenyFile()
        if stamp is not None:
            try:
                # utf-8-sig: Windows editors may add a byte order mark.
                content = DenyFile.model_validate_json(self.path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError) as exc:
                log.error("ignoring %s, so nobody is denied: %s", self.path, exc)
        self._orgs = frozenset(slugify(name) for name in content.orgs)
        self._devices = frozenset((slugify(d.org), slugify(d.device)) for d in content.devices)
        log.info("deny list: %d orgs, %d devices", len(self._orgs), len(self._devices))

    def denies(self, org_id: str, device_id: str) -> bool:
        return org_id in self._orgs or (org_id, device_id) in self._devices


def uninstall_signature(token: str, nonce: str) -> str:
    """Proof that an uninstall order came from this server and answers this request. The
    agent ignores any denial without it (captive portals, web filters, replays)."""
    return hmac.new(token.encode(), f"uninstall:{nonce}".encode(), hashlib.sha256).hexdigest()


# ----------------------------------------------------------------------- monitor loop

NEW_ITEM_GRACE = 300
"""Seconds after a discovery change during which rejected values are expected (Zabbix is
still creating the new items), so they're logged at DEBUG instead of WARNING."""


class Monitor:
    """One evaluate -> send -> persist cycle every EVAL_INTERVAL."""

    def __init__(
        self,
        settings: Settings,
        registry: Registry,
        sender: Sender,
        store: SnapshotStore | None,
        started_at: float,
        deny_list: DenyList | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.sender = sender
        self.store = store
        self.started_at = started_at
        self.deny_list = deny_list
        self._discovery: dict[str, str] = {}
        self._discovery_pending = False  # True until Zabbix has accepted the latest version
        self._discovery_sent_at = float("-inf")
        self._discovery_changed_at = float("-inf")

    async def run_cycle(self, now: float) -> None:
        s = self.settings
        if self.deny_list is not None:
            # Denied devices stop checking in on purpose, so they mustn't alert.
            self.deny_list.refresh()
            for d in self.registry.remove_where(self.deny_list.denies):
                log.info("removing denied device %s / %s", d.org_name, d.device_name)
        for d in self.registry.forget(now, s.device_forget_after):
            log.info("forgetting %s / %s (silent since %d)", d.org_name, d.device_name, d.last_seen)

        ev = evaluate(
            self.registry.devices(),
            now,
            device_timeout=s.device_timeout,
            confirm_window=s.confirm_window,
            site_min_devices=s.site_min_devices,
            ipv6_prefix=s.site_ipv6_prefix,
        )
        discovery = build_discovery(ev)
        if discovery != self._discovery:
            self._discovery, self._discovery_changed_at = discovery, now
            self._discovery_pending = True
        values = build_values(ev, in_grace=now - self.started_at < s.device_timeout)

        try:
            # Discovery goes first so Zabbix sees new entities before their values.
            if self._discovery_pending or now - self._discovery_sent_at >= s.discovery_interval:
                await self._send_discovery(now)
            await self._send_values(values, now)
        except Exception:
            log.exception("sending to Zabbix failed; retrying next cycle")

        if self.store is not None:
            snapshot = [asdict(d) for d in self.registry.devices()]
            try:
                await asyncio.to_thread(self.store.save, snapshot)
            except OSError:
                log.exception("saving snapshot to %s failed", self.store.path)

    async def _send_discovery(self, now: float) -> None:
        """Send discovery on its own: Zabbix only reports counts, so this is the only way
        to know whether it was accepted. Rejected discovery stays pending."""
        host = self.settings.zabbix_host
        rules = list(self._discovery.items())
        result = await self.sender.send(host, rules, int(now))
        self._discovery_pending = result.failed > 0
        if result.failed:
            log.warning(
                "Zabbix rejected %d of %d discovery rules; retrying next cycle. Check host %r "
                "exists, has the zabbcomm template linked, and allows this connector.",
                result.failed,
                len(rules),
                host,
            )
        else:
            self._discovery_sent_at = now

    async def _send_values(self, values: list[tuple[str, str]], now: float) -> None:
        result = await self.sender.send(self.settings.zabbix_host, values, int(now))
        if not result.failed:
            return
        recent = now - self._discovery_changed_at < NEW_ITEM_GRACE
        log.log(
            logging.DEBUG if recent else logging.WARNING,
            "Zabbix rejected %d of %d values. %s",
            result.failed,
            len(values),
            "Expected briefly after new discovery."
            if recent
            else "Check the discovered items exist on the Zabbix host (see the discovery "
            "rules' Info column) and trapper 'Allowed hosts' permits this connector.",
        )

    async def run_forever(self, clock: Callable[[], float]) -> None:
        while True:
            try:
                await self.run_cycle(clock())
            except Exception:
                log.exception("evaluation cycle failed")
            await asyncio.sleep(self.settings.eval_interval)


# ------------------------------------------------------------------------ FastAPI app


def _no_control_chars(value: str) -> str:
    if _CONTROL_CHARS.search(value):
        raise ValueError("must not contain control characters")
    return value


Name = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
    AfterValidator(_no_control_chars),
]


class CheckIn(BaseModel):
    org: Name
    device: Name
    agent_version: (
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, max_length=32),
            AfterValidator(_no_control_chars),
        ]
        | None
    ) = None
    is_server: bool | None = None


_NONCE = re.compile(r"[0-9a-f]{32}")

MAX_BODY_BYTES = 8192
"""A check-in with the longest allowed names, fully escaped, is well under this."""


class RequestGuard:
    """Protects the internet-facing side. Anything but GET / HEAD needs the bearer token,
    checked before any of the body is read, and bodies are capped at MAX_BODY_BYTES."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD"):
            await self.app(scope, receive, send)
            return

        authorization = dict(scope["headers"]).get(b"authorization", b"")
        scheme, _, credentials = authorization.partition(b" ")
        if scheme.lower() != b"bearer" or not secrets.compare_digest(
            credentials.strip(), self.token
        ):
            response = JSONResponse(
                {"detail": "invalid or missing token"},
                status.HTTP_401_UNAUTHORIZED,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        body = bytearray()
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            more_body = message.get("more_body", False)
            if len(body) > MAX_BODY_BYTES:
                response = JSONResponse({"detail": "request body too large"}, 413)
                await response(scope, receive, send)
                return

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)


def create_app(
    settings: Settings | None = None,
    *,
    sender: Sender | None = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    """App factory (`uvicorn zabbcomm:create_app --factory`). Tests pass their own
    settings, sender and clock."""
    settings = settings if settings is not None else load_settings()
    registry = Registry(settings.max_orgs, settings.max_devices_per_org, settings.confirm_window)
    store = SnapshotStore(settings.data_dir / "registry.json")
    deny_list = DenyList(settings.data_dir / "deny.json")
    token = settings.api_token.get_secret_value()
    if sender is None:
        sender = (
            DryRunSender()
            if settings.zabbix_dry_run
            else ZabbixSender(settings.zabbix_server, settings.zabbix_port)
        )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        registry.replace_all(store.load())
        log.info("loaded %d devices from %s", len(registry.devices()), store.path)
        monitor = Monitor(
            settings, registry, sender, store, started_at=clock(), deny_list=deny_list
        )
        task = asyncio.create_task(monitor.run_forever(clock))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            try:
                store.save([asdict(d) for d in registry.devices()])
            except OSError:
                log.exception("final snapshot save failed")

    # Internet-facing: no interactive docs or schema.
    app = FastAPI(
        title="zabbcomm", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.registry = registry
    app.add_middleware(RequestGuard, token=token)

    @app.post("/api/v1/checkin", status_code=status.HTTP_204_NO_CONTENT)
    async def checkin(body: CheckIn, request: Request) -> Response:
        # `async def` on purpose: it runs on the event loop (a plain `def` would run in a
        # threadpool), so registry updates can never interleave with a monitor cycle.
        ip = request.client.host if request.client else None
        deny_list.refresh()
        if deny_list.denies(slugify(body.org), slugify(body.device)):
            registry.remove_where(deny_list.denies)
            log.warning("denied check-in %s / %s from %s", body.org, body.device, ip)
            nonce = request.headers.get("x-zabbcomm-nonce", "")
            headers = {}
            if _NONCE.fullmatch(nonce):
                headers["X-Zabbcomm-Uninstall"] = uninstall_signature(token, nonce)
            return JSONResponse({"detail": "device denied"}, status.HTTP_410_GONE, headers)
        try:
            registry.check_in(
                body.org, body.device, ip, body.agent_version, clock(), body.is_server
            )
        except LimitReachedError as exc:
            log.warning("rejected check-in %s / %s from %s: %s", body.org, body.device, ip, exc)
            raise HTTPException(status.HTTP_403_FORBIDDEN, "registration limit reached") from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


# ------------------------------------------------------------------------ entry point


def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # Proxy headers are left at uvicorn's defaults (on, trusting FORWARDED_ALLOW_IPS,
    # default 127.0.0.1): site detection depends on seeing the real client IP.
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
