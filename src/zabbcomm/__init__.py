"""zabbcomm: turn Syncro endpoint check-ins into Zabbix org / site / device status.

Importing this package has no side effects: settings are only read in `create_app()` and
`main()`.
"""

from .app import CheckIn, RequestGuard, create_app
from .deny import DenyList, uninstall_signature
from .evaluator import (
    OFFLINE,
    ONLINE,
    PENDING,
    SUPPRESSED,
    DeviceStatus,
    Evaluation,
    OrgStatus,
    SiteStatus,
    evaluate,
)
from .ids import site_network, slugify
from .monitor import Monitor
from .persistence import SnapshotStore
from .registry import Device, LimitReachedError, Registry
from .settings import Settings, load_settings
from .zabbix import (
    KEY_ALIVE,
    KEY_DEVICE_DISCOVERY,
    KEY_ORG_DISCOVERY,
    KEY_SITE_DISCOVERY,
    DryRunSender,
    Sender,
    SendResult,
    ZabbixSender,
    build_discovery,
    build_values,
    device_key,
    org_key,
    site_key,
)

__version__ = "0.1.0"

__all__ = [
    "KEY_ALIVE",
    "KEY_DEVICE_DISCOVERY",
    "KEY_ORG_DISCOVERY",
    "KEY_SITE_DISCOVERY",
    "OFFLINE",
    "ONLINE",
    "PENDING",
    "SUPPRESSED",
    "CheckIn",
    "DenyList",
    "Device",
    "DeviceStatus",
    "DryRunSender",
    "Evaluation",
    "LimitReachedError",
    "Monitor",
    "OrgStatus",
    "Registry",
    "RequestGuard",
    "SendResult",
    "Sender",
    "Settings",
    "SiteStatus",
    "SnapshotStore",
    "ZabbixSender",
    "build_discovery",
    "build_values",
    "create_app",
    "device_key",
    "evaluate",
    "load_settings",
    "org_key",
    "site_key",
    "site_network",
    "slugify",
    "uninstall_signature",
]
