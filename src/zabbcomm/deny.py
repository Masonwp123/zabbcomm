"""The deny list (DATA_DIR/deny.json) and the signed uninstall order sent to agents."""

import hashlib
import hmac
import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .ids import slugify

log = logging.getLogger("zabbcomm")


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
