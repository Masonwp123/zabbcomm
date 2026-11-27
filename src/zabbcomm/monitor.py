"""The background loop: evaluate, send to Zabbix, save a snapshot, every EVAL_INTERVAL."""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import asdict

from .deny import DenyList
from .evaluator import evaluate
from .persistence import SnapshotStore
from .registry import Registry
from .settings import Settings
from .zabbix import Sender, build_discovery, build_values

log = logging.getLogger("zabbcomm")

NEW_ITEM_GRACE = 300
"""Seconds after a discovery change during which rejected values are expected (Zabbix is
still creating the new items), so they're logged at DEBUG instead of WARNING."""


class Monitor:
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
        self._saving: asyncio.Future[None] | None = None

    async def run_cycle(self, now: float) -> None:
        s = self.settings
        if self.deny_list is not None:
            # Denied devices stop checking in on purpose, so they mustn't alert.
            self.deny_list.refresh()
            for d in self.registry.remove_denied(self.deny_list.denies):
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
            # Shielded and remembered: cancelling the loop doesn't stop the write
            # thread, so `stop` must wait for it before writing the final snapshot.
            self._saving = asyncio.ensure_future(
                asyncio.to_thread(self.store.save, self._snapshot())
            )
            try:
                await asyncio.shield(self._saving)
            except OSError:
                log.exception("saving snapshot to %s failed", self.store.path)

    def _snapshot(self) -> list[dict[str, object]]:
        return [asdict(d) for d in self.registry.devices()]

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

    async def stop(self, task: asyncio.Task[None]) -> None:
        """Stop the loop and write a final snapshot no earlier save can overwrite."""
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if self._saving is not None:
            with contextlib.suppress(Exception):  # a failed save was already logged
                await self._saving
        if self.store is not None:
            try:
                await asyncio.to_thread(self.store.save, self._snapshot())
            except OSError:
                log.exception("final snapshot save failed")
