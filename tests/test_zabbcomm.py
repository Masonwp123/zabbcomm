import json
import logging
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import zabbcomm as z

TOKEN = "test-token-0123456789"
NOW = 1_000_000.0
TIMEOUT = 2100
HQ, BRANCH, HOME = "203.0.113.10", "198.51.100.20", "192.0.2.50"


def make_settings(tmp_path: Path, **overrides: object) -> z.Settings:
    base: dict[str, object] = {
        "api_token": TOKEN,
        "zabbix_server": "127.0.0.1",
        "data_dir": tmp_path,
    }
    return z.Settings(**(base | overrides))  # type: ignore[arg-type]


def dev(org: str, name: str, seen_ago: float, ip: str | None = HQ) -> z.Device:
    return z.Device(z.slugify(org), z.slugify(name), org, name, NOW - seen_ago, ip)


def run_eval(devices: list[z.Device], **kw: int) -> z.Evaluation:
    params = {"device_timeout": TIMEOUT, "site_min_devices": 2, "ipv6_prefix": 64} | kw
    return z.evaluate(devices, NOW, **params)


def device_statuses(ev: z.Evaluation) -> dict[str, int]:
    return {d.device_id: d.status for d in ev.devices}


def site_statuses(ev: z.Evaluation) -> dict[str, int]:
    return {s.site_id: s.status for s in ev.sites}


class FakeSender:
    def __init__(self) -> None:
        self.calls: list[list[tuple[str, str]]] = []
        self.fail_with: Exception | None = None
        self.rejected = 0

    async def send(self, host: str, values: list[tuple[str, str]], clock: int) -> z.SendResult:
        if self.fail_with:
            raise self.fail_with
        self.calls.append(values)
        return z.SendResult(len(values) - self.rejected, self.rejected)

    def last(self) -> dict[str, str]:
        return dict(self.calls[-1])


# ------------------------------------------------------------------- IDs and sites


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Acme Inc", "acme-inc"),
        ("Acme, Inc.", "acme-inc"),
        ("  ACME--inc ", "acme-inc"),
        ("Café Zoë", "cafe-zoe"),
        ("ACME-WS01", "acme-ws01"),
    ],
)
def test_slugify(name: str, slug: str) -> None:
    assert z.slugify(name) == slug


def test_slugify_non_latin_names_get_distinct_hash_ids() -> None:
    a, b = z.slugify("東京"), z.slugify("大阪")
    assert a.startswith("h-") and b.startswith("h-") and a != b


@pytest.mark.parametrize(
    ("ip", "network"),
    [
        ("203.0.113.10", "203.0.113.10"),
        ("::ffff:203.0.113.10", "203.0.113.10"),
        ("2001:db8:1:2::aaaa", "2001:db8:1:2::/64"),
        ("2001:db8:1:2:ffff::1", "2001:db8:1:2::/64"),
        ("testclient", None),
        (None, None),
    ],
)
def test_site_network(ip: str | None, network: str | None) -> None:
    assert z.site_network(ip, 64) == network


# ------------------------------------------------------------------------ evaluator


def test_all_devices_stale_org_down_and_lower_levels_suppressed() -> None:
    ev = run_eval([dev("Acme", "a", 3000), dev("Acme", "b", 3000)])
    assert ev.orgs[0].status == z.OFFLINE
    assert site_statuses(ev) == {"203-0-113-10": z.SUPPRESSED}
    assert device_statuses(ev) == {"a": z.SUPPRESSED, "b": z.SUPPRESSED}


def test_single_stale_device_alerts_alone() -> None:
    ev = run_eval([dev("Acme", "a", 60), dev("Acme", "b", 3000)])
    assert ev.orgs[0].status == z.ONLINE
    assert site_statuses(ev) == {"203-0-113-10": z.ONLINE}
    assert device_statuses(ev) == {"a": z.ONLINE, "b": z.OFFLINE}


def test_whole_site_down_while_other_site_up() -> None:
    ev = run_eval(
        [
            dev("Acme", "hq1", 60, HQ),
            dev("Acme", "hq2", 60, HQ),
            dev("Acme", "br1", 3000, BRANCH),
            dev("Acme", "br2", 3000, BRANCH),
        ]
    )
    assert ev.orgs[0].status == z.ONLINE
    assert site_statuses(ev) == {"203-0-113-10": z.ONLINE, "198-51-100-20": z.OFFLINE}
    assert device_statuses(ev)["br1"] == z.SUPPRESSED
    assert device_statuses(ev)["br2"] == z.SUPPRESSED


def test_site_needs_min_devices() -> None:
    one = run_eval([dev("Acme", "a", 60, HQ), dev("Acme", "lt", 60, HOME)])
    assert {s.site_name for s in one.sites} == set()
    two = run_eval([dev("Acme", "a", 60, HQ), dev("Acme", "b", 60, HQ)])
    assert {s.site_name for s in two.sites} == {HQ}


def test_wan_ip_change_moves_devices_and_retires_old_site_without_alert() -> None:
    ev = run_eval([dev("Acme", "a", 60, "203.0.113.11"), dev("Acme", "b", 60, "203.0.113.11")])
    assert site_statuses(ev) == {"203-0-113-11": z.ONLINE}  # old 203-0-113-10 simply absent


def test_unsited_device_stale_alerts_against_org() -> None:
    ev = run_eval(
        [dev("Acme", "a", 60, HQ), dev("Acme", "b", 60, HQ), dev("Acme", "lt", 3000, HOME)]
    )
    assert device_statuses(ev)["lt"] == z.OFFLINE
    assert all(s.site_name != HOME for s in ev.sites)


def test_single_device_org_down_alerts_at_org_only() -> None:
    ev = run_eval([dev("Globex", "ws1", 3000)])
    assert ev.orgs[0].status == z.OFFLINE
    assert device_statuses(ev) == {"ws1": z.SUPPRESSED}


def test_ipv6_devices_in_same_prefix_form_one_site() -> None:
    ev = run_eval(
        [dev("Acme", "a", 60, "2001:db8:1:2::10"), dev("Acme", "b", 60, "2001:db8:1:2::20")]
    )
    assert [s.site_name for s in ev.sites] == ["2001:db8:1:2::/64"]


def test_org_display_name_is_latest_seen() -> None:
    ev = run_eval([dev("Acme Inc", "a", 600), dev("ACME, Inc.", "b", 60)])
    assert ev.orgs[0].org_name == "ACME, Inc."


# ----------------------------------------------------------------- Zabbix payloads


def test_grace_period_withholds_offline_and_suppressed_values() -> None:
    ev = run_eval([dev("Acme", "a", 60), dev("Acme", "b", 3000), dev("Globex", "g", 3000)])
    graced = dict(z.build_values(ev, in_grace=True))
    statuses = {k: v for k, v in graced.items() if ".status[" in k}
    assert set(statuses.values()) == {"1"}
    normal = dict(z.build_values(ev, in_grace=False))
    assert normal[z.device_key("status", "acme", "b")] == "0"
    assert normal[z.org_key("status", "globex")] == "0"
    assert normal[z.KEY_ALIVE] == "1"


def test_discovery_payload_shape() -> None:
    ev = run_eval([dev("Acme Inc", "WS-1", 60), dev("Acme Inc", "WS-2", 60)])
    d = z.build_discovery(ev)
    assert json.loads(d[z.KEY_ORG_DISCOVERY]) == [{"{#ORGID}": "acme-inc", "{#ORG}": "Acme Inc"}]
    assert json.loads(d[z.KEY_SITE_DISCOVERY])[0]["{#SITEID}"] == "203-0-113-10"
    assert {r["{#DEVICE}"] for r in json.loads(d[z.KEY_DEVICE_DISCOVERY])} == {"WS-1", "WS-2"}


# -------------------------------------------------------------------------- monitor


def make_monitor(tmp_path: Path, **overrides: object) -> tuple[z.Monitor, FakeSender]:
    settings = make_settings(tmp_path, **overrides)
    registry = z.Registry(settings.max_orgs, settings.max_devices_per_org)
    sender = FakeSender()
    store = z.SnapshotStore(tmp_path / "registry.json")
    return z.Monitor(settings, registry, sender, store, started_at=NOW - 10 * TIMEOUT), sender


@pytest.mark.anyio
async def test_discovery_sent_first_then_only_on_change_or_interval(tmp_path: Path) -> None:
    monitor, sender = make_monitor(tmp_path)
    monitor.registry.check_in("Acme", "a", HQ, None, NOW)

    await monitor.run_cycle(NOW)
    keys = [k for k, _ in sender.calls[-1]]
    assert keys[:3] == [z.KEY_ORG_DISCOVERY, z.KEY_SITE_DISCOVERY, z.KEY_DEVICE_DISCOVERY]

    await monitor.run_cycle(NOW + 60)
    assert z.KEY_ORG_DISCOVERY not in sender.last()

    monitor.registry.check_in("Acme", "b", HQ, None, NOW + 100)
    await monitor.run_cycle(NOW + 120)
    assert z.KEY_DEVICE_DISCOVERY in sender.last()

    await monitor.run_cycle(NOW + 120 + 3600)
    assert z.KEY_ORG_DISCOVERY in sender.last()


@pytest.mark.anyio
async def test_sender_failure_is_logged_and_discovery_retried(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monitor, sender = make_monitor(tmp_path)
    monitor.registry.check_in("Acme", "a", HQ, None, NOW)
    sender.fail_with = ConnectionRefusedError("zabbix down")
    await monitor.run_cycle(NOW)
    assert "retrying next cycle" in caplog.text

    sender.fail_with = None
    await monitor.run_cycle(NOW + 60)
    assert z.KEY_ORG_DISCOVERY in sender.last()


@pytest.mark.anyio
async def test_rejected_values_warn_only_once_discovery_is_old(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monitor, sender = make_monitor(tmp_path)
    monitor.registry.check_in("Acme", "a", HQ, None, NOW)
    sender.rejected = 3
    caplog.set_level(logging.DEBUG, logger="zabbcomm")
    await monitor.run_cycle(NOW)
    assert [r.levelno for r in caplog.records if "rejected" in r.message] == [logging.DEBUG]
    caplog.clear()
    monitor.registry.check_in("Acme", "a", HQ, None, NOW + 600)
    await monitor.run_cycle(NOW + 600)
    assert [r.levelno for r in caplog.records if "rejected" in r.message] == [logging.WARNING]


@pytest.mark.anyio
async def test_startup_grace_period(tmp_path: Path) -> None:
    monitor, sender = make_monitor(tmp_path)
    monitor.started_at = NOW
    monitor.registry.replace_all([dev("Acme", "a", 3000), dev("Acme", "b", 3000)])
    await monitor.run_cycle(NOW + 60)
    assert z.org_key("status", "acme") not in sender.last()
    await monitor.run_cycle(NOW + TIMEOUT)
    assert sender.last()[z.org_key("status", "acme")] == "0"


@pytest.mark.anyio
async def test_recovery_after_check_in(tmp_path: Path) -> None:
    monitor, sender = make_monitor(tmp_path)
    monitor.registry.replace_all([dev("Acme", "a", 3000)])
    await monitor.run_cycle(NOW)
    assert sender.last()[z.org_key("status", "acme")] == "0"
    monitor.registry.check_in("Acme", "a", HQ, None, NOW + 30)
    await monitor.run_cycle(NOW + 60)
    assert sender.last()[z.org_key("status", "acme")] == "1"
    assert sender.last()[z.device_key("status", "acme", "a")] == "1"


@pytest.mark.anyio
async def test_forgotten_devices_leave_registry_and_discovery(tmp_path: Path) -> None:
    monitor, sender = make_monitor(tmp_path, device_forget_after=86400)
    monitor.registry.replace_all([dev("Acme", "old", 90000), dev("Acme", "new", 60)])
    await monitor.run_cycle(NOW)
    assert [d.device_id for d in monitor.registry.devices()] == ["new"]
    devices = json.loads(sender.last()[z.KEY_DEVICE_DISCOVERY])
    assert [r["{#DEVICEID}"] for r in devices] == ["new"]


@pytest.mark.anyio
async def test_snapshot_round_trip(tmp_path: Path) -> None:
    monitor, _ = make_monitor(tmp_path)
    monitor.registry.check_in("Acme Inc", "WS-1", HQ, "1.0.0", NOW)
    await monitor.run_cycle(NOW)
    loaded = z.SnapshotStore(tmp_path / "registry.json").load()
    assert [asdict(d) for d in loaded] == [asdict(d) for d in monitor.registry.devices()]


def test_corrupt_snapshot_is_moved_aside(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    path.write_text("{not json", encoding="utf-8")
    assert z.SnapshotStore(path).load() == []
    assert not path.exists()
    assert list(tmp_path.glob("registry.json.corrupt-*"))


# ------------------------------------------------------------------------------ API


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def client_for(app: object, ip: str = HQ) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=(ip, 50000))  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=transport, base_url="http://zabbcomm")


AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.mark.anyio
async def test_checkin_records_device_and_client_ip(tmp_path: Path) -> None:
    app = z.create_app(make_settings(tmp_path), sender=FakeSender(), clock=lambda: NOW)
    async with client_for(app) as c:
        r = await c.post(
            "/api/v1/checkin",
            json={"org": " Acme Inc ", "device": "WS-1", "agent_version": "1.0.0", "extra": 1},
            headers=AUTH,
        )
    assert r.status_code == 204
    (d,) = app.state.registry.devices()
    assert (d.org_id, d.device_id, d.org_name, d.last_ip, d.last_seen) == (
        "acme-inc",
        "ws-1",
        "Acme Inc",
        HQ,
        NOW,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-token-0000000"}])
async def test_bad_token_is_401_even_with_invalid_body(tmp_path: Path, headers: dict) -> None:
    app = z.create_app(make_settings(tmp_path), sender=FakeSender())
    async with client_for(app) as c:
        r = await c.post("/api/v1/checkin", json={"org": ""}, headers=headers)
    assert r.status_code == 401
    assert app.state.registry.devices() == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        {"org": "", "device": "x"},
        {"org": "   ", "device": "x"},
        {"org": "Acme", "device": "x" * 129},
        {"org": "Acme\n", "device": "x\x00y"},
        {"org": "Acme"},
    ],
)
async def test_invalid_body_is_422(tmp_path: Path, body: dict) -> None:
    app = z.create_app(make_settings(tmp_path), sender=FakeSender())
    async with client_for(app) as c:
        r = await c.post("/api/v1/checkin", json=body, headers=AUTH)
    assert r.status_code == 422


@pytest.mark.anyio
async def test_caps_are_enforced(tmp_path: Path) -> None:
    app = z.create_app(
        make_settings(tmp_path, max_orgs=1, max_devices_per_org=1), sender=FakeSender()
    )
    async with client_for(app) as c:
        ok = await c.post("/api/v1/checkin", json={"org": "A", "device": "1"}, headers=AUTH)
        again = await c.post("/api/v1/checkin", json={"org": "A", "device": "1"}, headers=AUTH)
        new_org = await c.post("/api/v1/checkin", json={"org": "B", "device": "1"}, headers=AUTH)
        new_dev = await c.post("/api/v1/checkin", json={"org": "A", "device": "2"}, headers=AUTH)
    assert [ok.status_code, again.status_code, new_org.status_code, new_dev.status_code] == [
        204,
        204,
        403,
        403,
    ]


@pytest.mark.anyio
async def test_token_never_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    app = z.create_app(make_settings(tmp_path, max_orgs=1), sender=FakeSender())
    async with client_for(app) as c:
        await c.post("/api/v1/checkin", json={"org": "A", "device": "1"}, headers=AUTH)
        await c.post("/api/v1/checkin", json={"org": "B", "device": "1"}, headers=AUTH)
    assert TOKEN not in caplog.text


@pytest.mark.anyio
async def test_healthz_and_no_public_docs(tmp_path: Path) -> None:
    app = z.create_app(make_settings(tmp_path), sender=FakeSender())
    async with client_for(app) as c:
        assert (await c.get("/healthz")).json() == {"status": "ok"}
        assert (await c.get("/docs")).status_code == 404
        assert (await c.get("/openapi.json")).status_code == 404


@pytest.mark.anyio
async def test_forwarded_ip_from_trusted_proxy_is_recorded(tmp_path: Path) -> None:
    app = z.create_app(make_settings(tmp_path), sender=FakeSender())
    proxied = ProxyHeadersMiddleware(app, trusted_hosts="10.0.0.5")
    headers = AUTH | {"X-Forwarded-For": BRANCH}
    async with client_for(proxied, ip="10.0.0.5") as c:
        await c.post("/api/v1/checkin", json={"org": "A", "device": "1"}, headers=headers)
    async with client_for(proxied, ip="192.0.2.99") as c:  # untrusted: header ignored
        await c.post("/api/v1/checkin", json={"org": "A", "device": "2"}, headers=headers)
    ips = {d.device_id: d.last_ip for d in app.state.registry.devices()}
    assert ips == {"1": BRANCH, "2": "192.0.2.99"}


# --------------------------------------------------------------------------- config


def test_load_settings_error_does_not_echo_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("API_TOKEN", "short-secret")
    monkeypatch.delenv("ZABBIX_SERVER", raising=False)
    with pytest.raises(SystemExit) as exc:
        z.load_settings()
    message = str(exc.value)
    assert "API_TOKEN" in message and "ZABBIX_SERVER" in message
    assert "short-secret" not in message
