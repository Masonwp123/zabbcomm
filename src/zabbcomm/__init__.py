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
from typing import Annotated, Literal, Protocol

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import (
    AfterValidator,
    BaseModel,
    Field,
    SecretStr,
    StringConstraints,
    ValidationError,
    field_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from zabbix_utils import AsyncSender, ItemValue

log = logging.getLogger("zabbcomm")


# --------------------------------------------------------------------------- settings


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
    data_dir: Path = Path("/app/data")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


def load_settings() -> Settings:
    """Read Settings from the environment, failing with a message that never echoes input
    values (a too-short API_TOKEN would otherwise be printed in the validation error)."""
    try:
        return Settings()  # type: ignore[call-arg]  # required fields come from the env
    except ValidationError as exc:
        problems = "\n".join(
            f"  {'.'.join(map(str, err['loc'])).upper()}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)
        )
        raise SystemExit(f"zabbcomm: invalid configuration:\n{problems}") from None


# ---------------------------------------------------------------------- IDs and sites

_NON_SLUG = re.compile(r"[^a-z0-9]+")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def slugify(name: str) -> str:
    """Stable ID for a name: ASCII-folded, casefolded, runs of other chars become '-'.

    Names with no ASCII letters or digits at all (e.g. non-Latin scripts) fall back to a
    short hash so they still get a distinct, key-safe ID.
    """
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = _NON_SLUG.sub("-", folded.casefold()).strip("-")
    if slug:
        return slug
    return "h-" + hashlib.sha256(name.casefold().encode()).hexdigest()[:12]


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


class LimitReachedError(Exception):
    """A check-in would exceed MAX_ORGS or MAX_DEVICES_PER_ORG."""


class Registry:
    """The app's only mutable state: org_id -> device_id -> Device.

    Only touch it from code running on the event loop with no `await` in between
    reads and writes; that is what makes it safe without locks.
    """

    def __init__(self, max_orgs: int, max_devices_per_org: int) -> None:
        self.max_orgs = max_orgs
        self.max_devices_per_org = max_devices_per_org
        self._orgs: dict[str, dict[str, Device]] = {}

    def check_in(
        self, org: str, device: str, ip: str | None, agent_version: str | None, now: float
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
            record = org_devices[device_id] = Device(org_id, device_id, org, device, now)
        record.org_name, record.device_name = org, device
        record.last_seen, record.last_ip, record.agent_version = now, ip, agent_version
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

    def replace_all(self, devices: Iterable[Device]) -> None:
        """Load state (e.g. from a snapshot). Caps are not applied to loaded data."""
        self._orgs = {}
        for d in devices:
            self._orgs.setdefault(d.org_id, {})[d.device_id] = d


# -------------------------------------------------------------------------- evaluator

ONLINE, OFFLINE, SUPPRESSED = 1, 0, 2
"""Status values. OFFLINE alerts in Zabbix; SUPPRESSED means a higher level (site or org)
is already reporting this outage."""


@dataclass(frozen=True)
class OrgStatus:
    org_id: str
    org_name: str
    status: int
    online: int
    total: int


@dataclass(frozen=True)
class SiteStatus:
    org_id: str
    org_name: str
    site_id: str
    site_name: str
    status: int
    online: int
    total: int


@dataclass(frozen=True)
class DeviceStatus:
    org_id: str
    org_name: str
    device_id: str
    device_name: str
    status: int
    wan_ip: str | None


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
    site_min_devices: int,
    ipv6_prefix: int,
) -> Evaluation:
    """Cascade org -> site -> device status so each outage alerts once, at the highest
    level that is entirely down. Pure: no I/O, no globals."""
    by_org: dict[str, list[Device]] = {}
    for d in devices:
        by_org.setdefault(d.org_id, []).append(d)

    orgs: list[OrgStatus] = []
    sites: list[SiteStatus] = []
    statuses: list[DeviceStatus] = []
    for org_id, members in sorted(by_org.items()):
        org_name = max(members, key=lambda d: d.last_seen).org_name
        online = {d.device_id for d in members if now - d.last_seen <= device_timeout}
        org_up = bool(online)
        orgs.append(
            OrgStatus(org_id, org_name, ONLINE if org_up else OFFLINE, len(online), len(members))
        )

        # A device belongs to the site of its latest WAN IP; small groups stay unsited.
        by_network: dict[str, list[Device]] = {}
        for d in members:
            network = site_network(d.last_ip, ipv6_prefix)
            if network is not None:
                by_network.setdefault(network, []).append(d)

        parent_up: dict[str, bool] = {}  # device_id -> is its site up (sited devices only)
        for network, site_members in sorted(by_network.items()):
            if len(site_members) < site_min_devices:
                continue
            site_online = sum(d.device_id in online for d in site_members)
            site_up = site_online > 0
            site_status = ONLINE if site_up else OFFLINE if org_up else SUPPRESSED
            sites.append(
                SiteStatus(
                    org_id,
                    org_name,
                    slugify(network),
                    network,
                    site_status,
                    site_online,
                    len(site_members),
                )
            )
            for d in site_members:
                parent_up[d.device_id] = site_up

        for d in sorted(members, key=lambda d: d.device_id):
            if d.device_id in online:
                device_status = ONLINE
            else:
                device_status = OFFLINE if parent_up.get(d.device_id, org_up) else SUPPRESSED
            statuses.append(
                DeviceStatus(org_id, org_name, d.device_id, d.device_name, device_status, d.last_ip)
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
        if value == ONLINE or not in_grace:
            values.append((key, str(value)))

    for o in ev.orgs:
        add_status(org_key("status", o.org_id), o.status)
        values.append((org_key("devices.online", o.org_id), str(o.online)))
        values.append((org_key("devices.total", o.org_id), str(o.total)))
    for s in ev.sites:
        add_status(site_key("status", s.org_id, s.site_id), s.status)
        values.append((site_key("devices.online", s.org_id, s.site_id), str(s.online)))
        values.append((site_key("devices.total", s.org_id, s.site_id), str(s.total)))
    for d in ev.devices:
        add_status(device_key("status", d.org_id, d.device_id), d.status)
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


class ZabbixSender:
    """Zabbix sender protocol (trapper, TCP 10051). Sends in chunks of 250, in order."""

    def __init__(self, server: str, port: int) -> None:
        self._sender = AsyncSender(server=server, port=port)

    async def send(self, host: str, values: list[tuple[str, str]], clock: int) -> SendResult:
        items = [ItemValue(host, key, value, clock) for key, value in values]
        response = await self._sender.send(items)
        return SendResult(processed=response.processed, failed=response.failed)


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
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.sender = sender
        self.store = store
        self.started_at = started_at
        self._sent_discovery: dict[str, str] = {}
        self._discovery_sent_at = float("-inf")
        self._discovery_changed_at = float("-inf")

    async def run_cycle(self, now: float) -> None:
        s = self.settings
        for d in self.registry.forget(now, s.device_forget_after):
            log.info("forgetting %s / %s (silent since %d)", d.org_name, d.device_name, d.last_seen)

        ev = evaluate(
            self.registry.devices(),
            now,
            device_timeout=s.device_timeout,
            site_min_devices=s.site_min_devices,
            ipv6_prefix=s.site_ipv6_prefix,
        )
        discovery = build_discovery(ev)
        changed = discovery != self._sent_discovery
        if changed:
            self._discovery_changed_at = now
        resend = changed or now - self._discovery_sent_at >= s.discovery_interval

        # Discovery goes first so Zabbix sees new entities before (or with) their values.
        values = list(discovery.items()) if resend else []
        values += build_values(ev, in_grace=now - self.started_at < s.device_timeout)

        try:
            result = await self.sender.send(s.zabbix_host, values, int(now))
        except Exception:
            log.exception("sending %d values to Zabbix failed; retrying next cycle", len(values))
        else:
            if resend:
                self._sent_discovery, self._discovery_sent_at = discovery, now
            if result.failed:
                recent = now - self._discovery_changed_at < NEW_ITEM_GRACE
                log.log(
                    logging.DEBUG if recent else logging.WARNING,
                    "Zabbix rejected %d of %d values. %s",
                    result.failed,
                    len(values),
                    "Expected briefly after new discovery."
                    if recent
                    else f"Check the zabbcomm template is linked to host {s.zabbix_host!r} "
                    "and trapper 'Allowed hosts' permits this connector.",
                )

        if self.store is not None:
            snapshot = [asdict(d) for d in self.registry.devices()]
            try:
                await asyncio.to_thread(self.store.save, snapshot)
            except OSError:
                log.exception("saving snapshot to %s failed", self.store.path)

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


def create_app(
    settings: Settings | None = None,
    *,
    sender: Sender | None = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    """App factory (`uvicorn zabbcomm:create_app --factory`). Tests pass their own
    settings, sender and clock."""
    settings = settings if settings is not None else load_settings()
    registry = Registry(settings.max_orgs, settings.max_devices_per_org)
    store = SnapshotStore(settings.data_dir / "registry.json")
    if sender is None:
        sender = (
            DryRunSender()
            if settings.zabbix_dry_run
            else ZabbixSender(settings.zabbix_server, settings.zabbix_port)
        )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if settings.device_timeout < 2 * settings.checkin_interval:
            log.warning(
                "DEVICE_TIMEOUT (%ds) is under 2x CHECKIN_INTERVAL (%ds): one late check-in "
                "will raise a false alert",
                settings.device_timeout,
                settings.checkin_interval,
            )
        registry.replace_all(store.load())
        log.info("loaded %d devices from %s", len(registry.devices()), store.path)
        monitor = Monitor(settings, registry, sender, store, started_at=clock())
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

    bearer = HTTPBearer(auto_error=False)
    expected_token = settings.api_token.get_secret_value().encode()

    async def require_token(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        if credentials is None or not secrets.compare_digest(
            credentials.credentials.encode(), expected_token
        ):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "invalid or missing token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.post(
        "/api/v1/checkin",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_token)],
    )
    async def checkin(body: CheckIn, request: Request) -> Response:
        # `async def` on purpose: it runs on the event loop (a plain `def` would run in a
        # threadpool), so registry updates can never interleave with a monitor cycle.
        ip = request.client.host if request.client else None
        try:
            registry.check_in(body.org, body.device, ip, body.agent_version, clock())
        except LimitReachedError as exc:
            log.warning("rejected check-in %s / %s from %s: %s", body.org, body.device, ip, exc)
            raise HTTPException(status.HTTP_403_FORBIDDEN, "registration limit reached") from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
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
