"""Fixes from the second whole-repo review (2026-11-27)."""

import asyncio
import json
import logging
import threading
from pathlib import Path

import httpx
import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import zabbcomm as z

TOKEN = "test-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
OLD_SITE = "203.0.113.1"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def settings(tmp_path: Path) -> z.Settings:
    return z.Settings(api_token=TOKEN, zabbix_server="127.0.0.1", data_dir=tmp_path)


class FakeSender:
    async def send(self, host: str, values: list[tuple[str, str]], clock: int) -> z.SendResult:
        return z.SendResult(len(values), 0)


# ------------------------------------------------- moves between networks, not addresses


def test_address_changes_inside_one_network_keep_the_previous_network() -> None:
    r = z.Registry(10, 10, 1050, ipv6_prefix=64)
    d = r.check_in("A", "pc", OLD_SITE, None, 0)
    r.check_in("A", "pc", "2001:db8::1", None, 900)  # moved to an IPv6 network
    r.check_in("A", "pc", "2001:db8::99", None, 1800)  # privacy address changed
    r.check_in("A", "pc", "2001:db8::98", None, 2700)  # and again
    assert (d.prev_ip, d.prev_seen) == (OLD_SITE, 0)


def test_ipv4_mapped_form_is_the_same_network() -> None:
    r = z.Registry(10, 10, 1050)
    d = r.check_in("A", "pc", OLD_SITE, None, 0)
    r.check_in("A", "pc", f"::ffff:{OLD_SITE}", None, 900)
    assert d.prev_ip is None


def test_left_behind_server_is_not_a_site_after_privacy_addresses_rotate() -> None:
    r = z.Registry(10, 10, 1050, ipv6_prefix=64)
    r.check_in("A", "srv", OLD_SITE, None, 0, is_server=True)  # then powered off
    for t, ip_a, ip_b in [
        (0, OLD_SITE, OLD_SITE),
        (19100, OLD_SITE, OLD_SITE),  # still here long after the server went quiet
        (20000, "2001:db8::1", "2001:db8::2"),  # the ISP moved the site to IPv6
        (20900, "2001:db8::99", "2001:db8::98"),  # privacy addresses rotated
    ]:
        r.check_in("A", "a", ip_a, None, t)
        r.check_in("A", "b", ip_b, None, t)
    ev = z.evaluate(
        r.devices(),
        21000,
        device_timeout=2100,
        confirm_window=1050,
        site_min_devices=2,
        ipv6_prefix=64,
    )
    assert [s.site_name for s in ev.sites] == ["2001:db8::/64"]


# --------------------------------------------------------------------- shutdown order


class SlowStore(z.SnapshotStore):
    """The first save blocks until released, like a slow disk."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def save(self, devices: list[dict[str, object]]) -> None:
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            self.release.wait(5)
        super().save(devices)


@pytest.mark.anyio
async def test_final_snapshot_is_not_overwritten_by_an_older_save(tmp_path: Path) -> None:
    conf = settings(tmp_path)
    registry = z.Registry(10, 10, conf.confirm_window)
    registry.check_in("Acme", "old", None, None, 1_000)
    store = SlowStore(tmp_path / "registry.json")
    monitor = z.Monitor(conf, registry, FakeSender(), store, started_at=0)

    task = asyncio.create_task(monitor.run_forever(lambda: 1_000.0))
    await asyncio.to_thread(store.started.wait, 5)  # the cycle's save is in progress
    registry.check_in("Acme", "new", None, None, 1_010)  # arrives meanwhile

    stopping = asyncio.create_task(monitor.stop(task))
    await asyncio.sleep(0.1)
    store.release.set()  # the old save finishes only now
    await stopping

    saved = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    assert sorted(d["device_id"] for d in saved["devices"]) == ["new", "old"]


# ------------------------------------------------------------------ untrusted proxies


async def post(app: object, client_ip: str, headers: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, client=(client_ip, 50000))  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://zabbcomm") as c:
        return await c.post(
            "/api/v1/checkin", json={"org": "A", "device": "1"}, headers=AUTH | headers
        )


@pytest.mark.anyio
async def test_warns_once_an_hour_when_a_proxy_is_not_trusted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    now = [1_000.0]
    app = z.create_app(settings(tmp_path), sender=FakeSender(), clock=lambda: now[0])
    proxied = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1")
    forwarded = {"X-Forwarded-For": "198.51.100.20"}

    def warnings() -> int:
        return sum("FORWARDED_ALLOW_IPS" in r.message for r in caplog.records)

    with caplog.at_level(logging.WARNING, logger="zabbcomm"):
        await post(proxied, "172.18.0.1", forwarded)
        await post(proxied, "172.18.0.1", forwarded)
        assert warnings() == 1
        now[0] += 3600
        await post(proxied, "172.18.0.1", forwarded)
        assert warnings() == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("client_ip", "headers"),
    [
        ("127.0.0.1", {"X-Forwarded-For": "198.51.100.20"}),  # trusted proxy
        ("198.51.100.20", {}),  # no proxy at all
    ],
)
async def test_no_proxy_warning_when_addresses_are_real(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, client_ip: str, headers: dict
) -> None:
    app = z.create_app(settings(tmp_path), sender=FakeSender())
    proxied = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1")
    with caplog.at_level(logging.WARNING, logger="zabbcomm"):
        assert (await post(proxied, client_ip, headers)).status_code == 204
    assert "FORWARDED_ALLOW_IPS" not in caplog.text
    assert app.state.registry.devices()[0].last_ip == "198.51.100.20"
