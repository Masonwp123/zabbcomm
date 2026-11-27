"""Everything Zabbix: item keys, discovery and value payloads, and the senders.

Every Zabbix key string lives here. Keys only ever contain slug IDs ([a-z0-9-]), so they
never need key-parameter quoting. Changing a key is a breaking change in Zabbix.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Protocol

from zabbix_utils import AsyncSender, ItemValue

from .evaluator import ONLINE, PENDING, Evaluation

log = logging.getLogger("zabbcomm")

# ---------------------------------------------------------------------------- payloads

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


# ----------------------------------------------------------------------------- senders


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
