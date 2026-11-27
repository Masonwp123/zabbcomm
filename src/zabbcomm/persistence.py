"""The registry snapshot in DATA_DIR/registry.json."""

import contextlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path

from .registry import Device

log = logging.getLogger("zabbcomm")

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
