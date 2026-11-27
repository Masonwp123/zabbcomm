"""Whole outages over time: devices check in on staggered, slightly late schedules, the
monitor evaluates every minute, and we record what would raise an alert (status 0)."""

import random
from collections.abc import Callable

import pytest

import zabbcomm as z

INTERVAL, TIMEOUT, EVAL = 900, 2100, 60
LATE = 120  # seconds a scheduled run may start late
WARMUP = 4 * INTERVAL  # long enough for every device to be established
HQ, BRANCH, HOME, NEW_IP = "203.0.113.10", "198.51.100.20", "192.0.2.50", "203.0.113.99"

FLEET = {f"hq{i}": HQ for i in range(5)} | {"br0": BRANCH, "br1": BRANCH}

Route = Callable[[str, float], str | None]
"""(device, time) -> the IP its check-in arrives from, or None if it can't get through."""


def normal(device: str, t: float) -> str | None:
    return FLEET[device]


def simulate(
    route: Route,
    *,
    seed: int,
    until: float,
    fleet: dict[str, str] = FLEET,
    servers: frozenset[str] = frozenset(),
) -> dict[str, float]:
    """Return {status key: first time it reported 0}."""
    rng = random.Random(seed)
    registry = z.Registry(10, 100, TIMEOUT // 2, 64)
    offsets = {name: rng.uniform(0, INTERVAL) for name in fleet}
    runs = sorted(
        (offset + k * INTERVAL + rng.uniform(0, LATE), name)
        for name, offset in offsets.items()
        for k in range(int(until // INTERVAL) + 1)
    )
    alerts: dict[str, float] = {}
    next_run = 0
    for now in range(0, int(until), EVAL):
        while next_run < len(runs) and runs[next_run][0] <= now:
            t, name = runs[next_run]
            next_run += 1
            if ip := route(name, t):
                registry.check_in("Acme", name, ip, None, t, name in servers)
        ev = z.evaluate(
            registry.devices(),
            now,
            device_timeout=TIMEOUT,
            confirm_window=TIMEOUT // 2,
            site_min_devices=2,
            ipv6_prefix=64,
        )
        for key, value in z.build_values(ev, in_grace=False):
            if ".status[" in key and value == "0":
                alerts.setdefault(key, now)
    return alerts


ORG_KEY = z.org_key("status", "acme")
HQ_KEY = z.site_key("status", "acme", z.slugify(HQ))
BRANCH_KEY = z.site_key("status", "acme", z.slugify(BRANCH))
SEEDS = range(40)


@pytest.mark.parametrize("seed", SEEDS)
def test_org_outage_and_recovery_alert_only_at_org_level(seed: int) -> None:
    start, end = WARMUP, WARMUP + 7200

    def route(device: str, t: float) -> str | None:
        return None if start <= t < end else normal(device, t)

    alerts = simulate(route, seed=seed, until=end + 2 * TIMEOUT)
    assert set(alerts) == {ORG_KEY}
    assert alerts[ORG_KEY] <= start + TIMEOUT + EVAL


@pytest.mark.parametrize("minutes", [10, 18, 21, 25, 30, 35, 40, 50])
@pytest.mark.parametrize("seed", range(10))
def test_short_org_outage_never_alerts_below_org_level(seed: int, minutes: int) -> None:
    start, end = WARMUP, WARMUP + minutes * 60

    def route(device: str, t: float) -> str | None:
        return None if start <= t < end else normal(device, t)

    assert set(simulate(route, seed=seed, until=end + 2 * TIMEOUT)) <= {ORG_KEY}


@pytest.mark.parametrize("seed", SEEDS)
def test_site_outage_and_recovery_alert_only_at_site_level(seed: int) -> None:
    start, end = WARMUP, WARMUP + 7200

    def route(device: str, t: float) -> str | None:
        return None if FLEET[device] == BRANCH and start <= t < end else normal(device, t)

    alerts = simulate(route, seed=seed, until=end + 2 * TIMEOUT)
    assert set(alerts) == {BRANCH_KEY}
    assert alerts[BRANCH_KEY] <= start + TIMEOUT + EVAL


@pytest.mark.parametrize("seed", SEEDS)
def test_single_device_failure_is_reported_without_extra_delay(seed: int) -> None:
    start = WARMUP

    def route(device: str, t: float) -> str | None:
        return None if device == "br1" and t >= start else normal(device, t)

    alerts = simulate(route, seed=seed, until=start + 2 * TIMEOUT)
    key = z.device_key("status", "acme", "br1")
    assert set(alerts) == {key}
    assert alerts[key] <= start + TIMEOUT + EVAL


@pytest.mark.parametrize("seed", SEEDS)
def test_site_outage_still_alerts_when_a_laptop_leaves_for_home(seed: int) -> None:
    start = WARMUP

    def route(device: str, t: float) -> str | None:
        if FLEET[device] == HQ and t >= start:
            return HOME if device == "hq0" and t >= start + 600 else None
        return normal(device, t)

    alerts = simulate(route, seed=seed, until=start + 2 * TIMEOUT)
    assert set(alerts) == {HQ_KEY}


@pytest.mark.parametrize("seed", SEEDS)
def test_ip_change_with_a_powered_off_server_raises_no_site_alert(seed: int) -> None:
    off, change = WARMUP, WARMUP + 3 * TIMEOUT

    def route(device: str, t: float) -> str | None:
        if device == "hq0" and t >= off:
            return None
        if FLEET[device] == HQ and t >= change:
            return NEW_IP
        return normal(device, t)

    alerts = simulate(route, seed=seed, until=change + 2 * TIMEOUT, servers=frozenset({"hq0"}))
    # The server was reported when it went off; the abandoned address never is.
    assert set(alerts) == {z.device_key("status", "acme", "hq0")}
