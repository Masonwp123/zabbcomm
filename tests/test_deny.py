import hashlib
import hmac
import json
import os
from pathlib import Path

import httpx
import pytest

import zabbcomm.__main__ as z

TOKEN = "test-token-0123456789"
NONCE = "0123456789abcdef0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}", "X-Zabbcomm-Nonce": NONCE}
NOW = 1_000_000.0


class FakeSender:
    def __init__(self) -> None:
        self.calls: list[list[tuple[str, str]]] = []

    async def send(self, host: str, values: list[tuple[str, str]], clock: int) -> z.SendResult:
        self.calls.append(values)
        return z.SendResult(len(values), 0)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def settings(tmp_path: Path) -> z.Settings:
    return z.Settings(api_token=TOKEN, zabbix_server="127.0.0.1", data_dir=tmp_path)


def deny(tmp_path: Path, content: object) -> None:
    path = tmp_path / "deny.json"
    previous = path.stat().st_mtime_ns if path.exists() else 0
    text = content if isinstance(content, str) else json.dumps(content)
    path.write_text(text, encoding="utf-8")
    # Make the change visible even on file systems with coarse timestamps.
    os.utime(path, ns=(previous + 10**9, previous + 10**9))


async def check_in(
    app: object, org: str, device: str, headers: dict[str, str] = AUTH
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://zabbcomm") as c:
        return await c.post("/api/v1/checkin", json={"org": org, "device": device}, headers=headers)


def test_signature_is_hmac_sha256_of_the_nonce() -> None:
    expected = hmac.new(TOKEN.encode(), f"uninstall:{NONCE}".encode(), hashlib.sha256)
    assert z.uninstall_signature(TOKEN, NONCE) == expected.hexdigest()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "content",
    [
        {"orgs": ["ACME, Inc."]},
        {"devices": [{"org": "acme inc", "device": "ws-1"}]},
    ],
)
async def test_denied_device_gets_a_signed_410_and_is_removed(
    tmp_path: Path, content: dict
) -> None:
    app = z.create_app(settings(tmp_path), sender=FakeSender())
    assert (await check_in(app, "Acme Inc", "WS-1")).status_code == 204
    deny(tmp_path, content)  # picked up without a restart
    r = await check_in(app, "Acme Inc", "WS-1")
    assert r.status_code == 410
    assert r.headers["X-Zabbcomm-Uninstall"] == z.uninstall_signature(TOKEN, NONCE)
    assert app.state.registry.devices() == []


@pytest.mark.anyio
async def test_only_the_listed_device_is_denied(tmp_path: Path) -> None:
    deny(tmp_path, {"devices": [{"org": "Acme", "device": "bad"}]})
    app = z.create_app(settings(tmp_path), sender=FakeSender())
    assert (await check_in(app, "Acme", "good")).status_code == 204
    assert (await check_in(app, "Other", "bad")).status_code == 204
    assert (await check_in(app, "Acme", "bad")).status_code == 410


@pytest.mark.anyio
@pytest.mark.parametrize("nonce", [None, "short", "Z" * 32])
async def test_no_signature_without_a_valid_nonce(tmp_path: Path, nonce: str | None) -> None:
    deny(tmp_path, {"orgs": ["Acme"]})
    app = z.create_app(settings(tmp_path), sender=FakeSender())
    headers = {"Authorization": AUTH["Authorization"]}
    if nonce:
        headers["X-Zabbcomm-Nonce"] = nonce
    r = await check_in(app, "Acme", "a", headers)
    assert r.status_code == 410
    assert "X-Zabbcomm-Uninstall" not in r.headers


@pytest.mark.anyio
async def test_a_bad_token_never_learns_about_the_deny_list(tmp_path: Path) -> None:
    deny(tmp_path, {"orgs": ["Acme"]})
    app = z.create_app(settings(tmp_path), sender=FakeSender())
    r = await check_in(app, "Acme", "a", AUTH | {"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert "X-Zabbcomm-Uninstall" not in r.headers


@pytest.mark.anyio
@pytest.mark.parametrize(
    "content",
    ["{not json", {"org": ["Acme"]}, {"orgs": "Acme"}, {"devices": [{"org": "Acme"}]}, []],
)
async def test_invalid_deny_file_denies_nobody(
    tmp_path: Path, content: object, caplog: pytest.LogCaptureFixture
) -> None:
    app = z.create_app(settings(tmp_path), sender=FakeSender())
    deny(tmp_path, {"orgs": ["Acme"]})
    assert (await check_in(app, "Acme", "a")).status_code == 410
    deny(tmp_path, content)
    assert (await check_in(app, "Acme", "a")).status_code == 204
    assert "nobody is denied" in caplog.text


@pytest.mark.anyio
async def test_removing_the_file_or_a_bom_is_handled(tmp_path: Path) -> None:
    app = z.create_app(settings(tmp_path), sender=FakeSender())
    deny(tmp_path, '﻿{"orgs": ["Acme"]}')
    assert (await check_in(app, "Acme", "a")).status_code == 410
    (tmp_path / "deny.json").unlink()
    assert (await check_in(app, "Acme", "a")).status_code == 204


@pytest.mark.anyio
async def test_monitor_drops_denied_devices_so_they_never_alert(tmp_path: Path) -> None:
    conf = settings(tmp_path)
    registry = z.Registry(10, 10, conf.confirm_window)
    registry.check_in("Gone", "a", None, None, NOW - 10_000)
    registry.check_in("Kept", "b", None, None, NOW)
    sender = FakeSender()
    deny(tmp_path, {"orgs": ["Gone"]})
    monitor = z.Monitor(
        conf,
        registry,
        sender,
        None,
        started_at=0,
        deny_list=z.DenyList(tmp_path / "deny.json"),
    )
    await monitor.run_cycle(NOW)
    assert [d.org_id for d in registry.devices()] == ["kept"]
    assert not any("gone" in key or "gone" in value for call in sender.calls for key, value in call)
