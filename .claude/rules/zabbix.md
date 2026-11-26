---
paths:
  - "src/zabbcomm/zabbix*.py"
  - "src/zabbcomm/evaluator.py"
  - "zabbix/**"
  - "tests/**/test_zabbix*.py"
---

# Zabbix integration rules

## Transport

- Send values with the **Zabbix sender protocol** to `ZABBIX_SERVER:ZABBIX_PORT`. Prefer
  the official `zabbix_utils` package (`AsyncSender`, `ItemValue`), and check its API
  against the installed version before using it.
- Wrap the sender behind a small interface (`async send(values) -> SendResult`) so tests
  can use a fake and `ZABBIX_DRY_RUN` can log instead.
- Batch every value of a cycle into one request. Set each value's `clock` to the
  evaluation timestamp.
- Read the response counts (`processed` / `failed`). Failures for items that were *just
  discovered* are expected for a short while (Zabbix has to update its configuration
  cache), so log those at DEBUG. Log a WARNING only when an item has been known for more
  than a few cycles and still fails. That usually means a key mismatch with the template
  or the trapper item's "Allowed hosts" rejecting the connector's IP.
- **No Zabbix JSON-RPC API.** Items come from trapper LLD, so the app needs no API user.
  Ask before changing that.

## Item key contract (host = `ZABBIX_HOST`)

| Key | Type | Value |
|---|---|---|
| `zabbcomm.alive` | trapper, int | `1` every cycle. The connector's own heartbeat |
| `zabbcomm.org.discovery` | trapper LLD rule | `[{"{#ORGID}": "...", "{#ORG}": "..."}]` |
| `zabbcomm.org.status[{#ORGID}]` | trapper, int | `1` = some device online ("org OK" heartbeat), `0` = all devices stale |
| `zabbcomm.org.devices.online[{#ORGID}]` | trapper, int | count |
| `zabbcomm.org.devices.total[{#ORGID}]` | trapper, int | count |
| `zabbcomm.site.discovery` | trapper LLD rule | `[{"{#ORGID}", "{#ORG}", "{#SITEID}", "{#SITE}"}]`. Only WAN IPs with at least `SITE_MIN_DEVICES` members |
| `zabbcomm.site.status[{#ORGID},{#SITEID}]` | trapper, int | `1` = some member online, `0` = all members stale while the org is up, `2` = all stale because the org is down (suppressed) |
| `zabbcomm.site.devices.online[{#ORGID},{#SITEID}]` | trapper, int | count |
| `zabbcomm.site.devices.total[{#ORGID},{#SITEID}]` | trapper, int | count |
| `zabbcomm.device.discovery` | trapper LLD rule | `[{"{#ORGID}", "{#ORG}", "{#DEVICEID}", "{#DEVICE}"}]` |
| `zabbcomm.device.status[{#ORGID},{#DEVICEID}]` | trapper, int | `1` online, `0` offline (isolated within its site, or within the org if unsited), `2` offline because its site or org is down (suppressed) |
| `zabbcomm.device.wan_ip[{#ORGID},{#DEVICEID}]` | trapper, text | Last WAN IP seen. Informational, and useful when triaging |

- Device items are keyed by device only, not by site. A device moving between sites (a
  laptop, or an ISP IP change) keeps its item history.
- `site_id` is the slug of the IP. For example `203.0.113.7` becomes `203-0-113-7`, and
  an IPv6 `/64` becomes something like `2001-db8-1-2--64`. A changed WAN IP is therefore
  a *new* site and the old one is retired, which is intended (see CLAUDE.md "Sites").

- **Item keys use slug IDs only.** Display names (`{#ORG}`, `{#DEVICE}`) go in item and
  trigger *names*. Slugs never need key-parameter quoting, and display names can contain
  commas, quotes and brackets.
- Changing a key is a **breaking change**: Zabbix creates a new item and the old one's
  history is lost. Point this out before doing it.
- Always send discovery **before** values for a new org or device. Send it the moment a
  new entity appears, and send a full resend every `DISCOVERY_INTERVAL`. Devices dropped
  after `DEVICE_FORGET_AFTER` leave the discovery payload, and the LLD rule's lost
  resources settings handle cleanup.

## Trigger design (lives in the template, not in Python)

- `nodata(/zabbcomm/zabbcomm.alive,5m)=1`: "zabbcomm connector not reporting" (High).
  Every org and device trigger **depends on this one**, so a dead connector doesn't page
  for every client.
- `last(org.status)=0`: "{#ORG}: all endpoints offline (internet or power outage)" (High).
- `nodata(org.status,15m)=1`: "{#ORG}: no status data" (Average). Catches an org that
  never re-registered.
- `last(site.status)=0 and nodata(site.status,15m)=0`: "{#ORG} / site {#SITE}: all
  endpoints offline" (High). The `nodata` term matters. When a site is retired because
  its devices moved to a new IP, the app stops sending for it. The trigger then resolves
  by itself instead of staying stuck on the last `0` until LLD deletes the item. Sites
  get **no** standalone `nodata` trigger, because going quiet is how a site retires.
- `last(device.status)=0`: "{#ORG} / {#DEVICE}: endpoint offline" (Warning).
- Value `2` deliberately never matches. Suppression lives in the app's cascade, not in
  trigger dependencies between LLD rules.

## Template file (`zabbix/zabbcomm_template.yaml`)

- Store it as a Zabbix YAML export. Confirm the target server version first, because
  the export's `version:` must not be newer than the server.
- Every template, item, prototype and trigger needs a stable 32-hex-char `uuid`. Create
  new ones only for new entities, and **never change an existing UUID**.
- The keys in the template must match the constants in `zabbix.py` exactly. Use the
  `zabbix-template` skill when editing.
