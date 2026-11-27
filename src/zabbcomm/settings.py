"""Configuration from environment variables (see .claude/CLAUDE.md for meanings)."""

import re
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_HOSTNAME = re.compile(r"[A-Za-z0-9._-]+")


class Settings(BaseSettings):
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
