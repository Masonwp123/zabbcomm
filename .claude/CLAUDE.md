# zabbcomm

A FastAPI connector that turns endpoint check-ins into Zabbix data. It tells three cases
apart:

- **Org down:** every endpoint in the org went silent.
- **Site down:** every endpoint behind one WAN IP went silent while the org's other sites
  are fine.
- **Single endpoint down:** one device went silent while the rest of its site is fine.

## How it works

```
Client device (Windows)           zabbcomm (this app)                 Zabbix
-----------------------           -------------------                 ------
Syncro policy script        --->  POST {APP_URL}/api/v1/checkin
  every 15 min {org, device}      records last_seen per (org, device)
                                  every EVAL_INTERVAL:
                                    evaluate each org/device  ----->  trapper items (sender
                                    batch-send values                 protocol, TCP 10051)
                                                                      triggers -> alerts
```

1. A Syncro policy runs `scripts/Send-ZabbcommCheckin.ps1` on client devices **every 15
   minutes**. It gets the org name (`$orgName`) and device name (`$assetName`) from
   **Syncro platform variables**, and the device type (`$deviceType`: `server` or
   `user`) from a dropdown variable set per policy. It POSTs them to `APP_URL`. It
   **installs nothing on the client**: no scheduled tasks, no files. See
   `.claude/rules/powershell-syncro.md`.
   **Alternative for faster detection:** `scripts/agent/Install-ZabbcommAgent.ps1`
   installs a scheduled task that checks in every 5 minutes (see "Endpoint agent and
   deny list" below). A device uses one method or the other.
2. For each `(org, device)` the app stores `last_seen` and `last_ip`, the WAN IP that
   check-in arrived from. It keeps them in memory and writes them to disk regularly
   (see Persistence).
3. **Sites are worked out from WAN IPs** (see "Sites" below). A device belongs to the
   site of its most recent check-in IP.
4. A background task, started from the FastAPI lifespan, evaluates state every
   `EVAL_INTERVAL` and sends all values to Zabbix in one batched sender request.
   Statuses cascade from org to site to device. Value `2` means "down, but a level
   above already covers it". That way one outage pages once, at the highest level that
   is fully down:

   | Item | `1` | `0` (alerts) | `2` (suppressed) |
   |---|---|---|---|
   | `org.status` | Some device in the org is online. This is the "this org is OK" heartbeat | Every device in the org is stale | n/a |
   | `site.status` | Some device at the site is online | Every device at the site is stale, but other sites in the org are up | Site is down because the whole org is down |
   | `device.status` | Online | Stale, while its site (or org, if it has no site) still has devices online | Stale because its site or org is down |

5. Each Zabbix item is created per org, site and device through **trapper low-level
   discovery (LLD)**. The app sends discovery JSON, and Zabbix creates the items from
   prototypes. The app needs no Zabbix API credentials, only sender access.

### Staggered check-ins (`PENDING`)

Devices check in at different times, so during an outage, and the recovery from it, some
look stale while others still look online. Reporting those as `0` would page once per
device before the one real alert. So a stale device or site is reported `0` only if its
parent (site, else org) is **confirmed** up:

- **Confirmed** means some member checked in within the *confirm window*
  (`DEVICE_TIMEOUT / 2`) **and** has been checking in without a gap that long for at
  least that long (`Device.online_since`).
- Parent online but not confirmed: the status is `PENDING` and **is not sent**, so
  Zabbix keeps the previous value.
- Parent entirely stale: `2`. Org status itself is never pending.
- This adds no delay to a real single-device or single-site failure, and recovery is
  quiet because a returning group isn't confirmed until everyone has had a turn.
- It tolerates check-ins running late by `DEVICE_TIMEOUT / 2 - CHECKIN_INTERVAL` (150 s
  by default) in the worst case. Beyond that, occasional extra alerts become possible:
  `tests/test_outages.py` simulations were clean to 300 s late, not at 600 s. Raising
  `DEVICE_TIMEOUT` widens the margin and slows detection.

### Sites (WAN IP grouping)

- **The server observes the WAN IP; the endpoint never reports it.** The source address
  of the check-in (`request.client.host`) *is* the site's public IP. It's free, it's the
  path actually in use, and it needs no third-party "what's my IP" lookup on the
  endpoint (which would fail in exactly the outages we care about).
- `site_id` = slug of the WAN IP (IPv4 address, or IPv6 `/SITE_IPV6_PREFIX` network).
  Display name `{#SITE}` = the IP. Friendly site names can come later as a config
  mapping, but don't build that unasked.
- **A WAN IP counts as a site if at least one server, or at least `SITE_MIN_DEVICES`
  devices (default 2), last checked in from it.** Other devices are *unsited*: they're
  judged against the org only. This keeps a laptop at someone's home from becoming a
  "site down" alert every evening, while a branch with only a server still gets one.
  Servers qualify alone because they stay put and stay on.
- **"Server" is decided in Syncro, not by the operating system.** Server devices are on
  a separate policy whose `$deviceType` is `server`; the script sends that as
  `is_server`. Some clients' servers don't run Windows Server, so don't detect it. A
  check-in that omits `is_server` keeps the stored value, and a device that has never
  sent it counts as a workstation.
- **IP changes move devices, they don't raise alarms.** Membership follows each
  device's latest IP. If a site's ISP address changes (DHCP renewal, ISP swap, dual-WAN
  failover), its devices check in from the new IP and move to a new site. The old site
  ends up with no members and drops out of discovery. It is **not** reported down. Only
  devices whose *last known* site is X can make X go down.
- **Leftovers don't make an abandoned IP go down.** If every device still on an IP went
  quiet at least a confirm window before another device last used that IP and then moved
  away (`prev_ip` / `prev_seen`), the IP isn't treated as a site. This covers a server
  that was already off when the ISP address changed.

### Endpoint agent and deny list

The optional agent (requested 2026-11-26, in addition to the footprint-free script):

- Syncro runs the installer (daily is enough). It checks that the folder, script, config
  and scheduled task `\zabbcomm\Checkin` are present, current and have run recently, and
  repairs them if not. `Uninstall-ZabbcommAgent.ps1` removes everything.
- It's a **scheduled task, not a Windows service**: a script can't be a service without
  compiling or bundling an executable on client devices.
- With every device on the agent, use `CHECKIN_INTERVAL=300` and `DEVICE_TIMEOUT=900`.
  With a mix, keep the settings for the slowest method (900 / 2100).

**Expiry** (backstop for when the deny list can't help): every installer run writes a
new ping on the device, and the agent removes itself once either limit passes without
one. Syncro script variables, whole days, `0` turns a limit off:

| Variable | Default | Counts |
|---|---|---|
| `expireAfterOnlineDays` | 7 | runs where zabbcomm accepted the check-in (`204`) |
| `expireAfterRunningDays` | 30 | all runs, so it works when the app is gone or the token changed |

- Time is counted in agent runs, not dates: powered-off time and clock changes don't
  matter. Offline, rejected and captive-portal runs never count as online.
- Expiry can't help if someone else keeps Syncro running the installer. That's what the
  deny list is for.
- An expired device stays in the app and alerts as offline. That is intentional: it
  shows where Syncro stopped reaching a device that should be monitored.

**Deny list** (`DATA_DIR/deny.json`, re-read when it changes, no restart):

```json
{ "orgs": ["Acme Inc"], "devices": [{ "org": "Globex", "device": "GLOBEX-WS01" }] }
```

- Names match by slug. Unknown keys or invalid JSON log an error and **deny nobody**.
- A denied check-in gets `410` and the device is removed from the registry (the monitor
  also drops denied devices), so it leaves discovery instead of alerting.
- If the request carried `X-Zabbcomm-Nonce` (32 hex), the reply has
  `X-Zabbcomm-Uninstall: HMAC-SHA256(API_TOKEN, "uninstall:" + nonce)`. **The agent
  uninstalls only on a `410` with a valid signature.** Nothing else, including `401` and
  `403`, may ever trigger removal.
- Consequence: **rotating `API_TOKEN` stops denial reaching agents that hold the old
  token** (they get `401`). Deny first, rotate afterwards.
- Footprint-free script devices get the `410` too, but have nothing to remove.

**Guiding principle:** the app decides *state* and Zabbix decides *alerting* (severity,
escalation, maintenance windows, business hours). Don't put alert policy in Python.

## Status

- **Done:** the Python connector (tests in `tests/test_zabbcomm.py`), the endpoint
  script `scripts/Send-ZabbcommCheckin.ps1`, and the Zabbix 7.4 template
  `zabbix/zabbcomm_template.yaml`.
- **Reported working by the user** (2026-11-26) against their Zabbix server.
- **Not yet confirmed there:** the `last_checkin` items, added afterwards.
- **Not yet confirmed in Syncro:** script version 1.2.0, which takes `$deviceType`.
- **Agent (2.1.0) not yet run on a real device:** tested with stand-in task commands,
  without elevation. Real task registration, the SYSTEM-only folder ACL and
  self-removal as SYSTEM are unverified.
- **Not yet run under Docker since the pre-publish fixes:** the `data-permissions`
  service and loopback port binding in `compose.yaml`, and the release workflow's test
  job and tag ranking (the ranking script was tested locally).

## Layout

The whole app is **one module, `../src/zabbcomm/__main__.py`**, ordered from pure logic to
I/O:

| Section | Contents |
|---|---|
| Settings | `Settings`, `load_settings()` |
| IDs and sites | `slugify`, `site_network` |
| Registry | `Registry`, `Device` |
| Evaluator | `evaluate`, the status dataclasses |
| Zabbix payloads | key constants, `build_discovery`, `build_values` |
| Senders | `ZabbixSender`, `DryRunSender` |
| Persistence | `SnapshotStore` |
| Monitor loop | `Monitor.run_cycle` / `Monitor.run_forever` |
| FastAPI app | `create_app()` factory |
| Entry point | `main()` (the Docker ENTRYPOINT `zabbcomm`) |

- Keep it a single file. It's about 880 lines now. If it passes about 900 lines, or a
  section starts needing its own helpers, split along those section lines.
- **Importing `zabbcomm` must have no side effects.** No module-level `Settings()` and
  no module-level `app`. Settings are read only in `create_app()` and `main()`.
- `create_app(settings, sender=, clock=)` takes everything injectable. Tests pass a
  fake sender and a fixed clock, and `app.state.registry` exposes state.

```
scripts/
  Send-ZabbcommCheckin.ps1   Syncro policy script, runs every 15 min, one check-in per run
  agent/
    Install-ZabbcommAgent.ps1    installs / checks / repairs the 5-minute agent
    Uninstall-ZabbcommAgent.ps1  removes it
zabbix/
  zabbcomm_template.yaml     Zabbix template export (items, LLD rules, triggers)
tests/
  test_zabbcomm.py           unit and API tests
  test_outages.py            outages and recoveries simulated over time
  test_deny.py               deny list and signed uninstall replies
compose.yaml                 Docker Compose deployment; settings are listed inline
.env.example                 the few values compose.yaml reads from .env
Dockerfile                   includes the HEALTHCHECK
```

## API contract

`POST /api/v1/checkin`, header `Authorization: Bearer <API_TOKEN>`

```json
{ "org": "Acme Inc", "device": "ACME-SRV01", "agent_version": "1.2.0", "is_server": true }
```

- `org` and `device` are required, trimmed, 1 to 128 chars, with no control characters.
  `agent_version` and `is_server` (boolean) are optional. Ignore unknown fields. There is deliberately **no WAN IP
  field**, because the site comes from the request's source address.
- Returns:
  - `204` on success.
  - `401` for a bad or missing token. The `RequestGuard` ASGI middleware checks it
    **before reading any of the body**, for every request that isn't GET or HEAD. Don't
    move auth into a route dependency: FastAPI parses the body first.
  - `413` for a body over 8 KB (`MAX_BODY_BYTES`).
  - `410` for a device on the deny list (see "Endpoint agent and deny list").
  - `422` for validation errors.
  - `403` when `MAX_ORGS` or `MAX_DEVICES_PER_ORG` would be exceeded.

  The response body must not echo the token or internal state.
- Interactive docs and the OpenAPI schema are disabled, because the app faces the
  internet.
- **IDs:** `org_id` and `device_id` are slugs of the names (casefold, accents removed,
  runs of non `[a-z0-9]` become `-`, trimmed). If a letter or digit has no ASCII form
  (non-Latin scripts, `ø`, `ł`), a hash of the whole name is appended
  (`rsted-<12 hex>`, or `h-<12 hex>` if nothing is left) so names can't merge. IDs key
  everything, including Zabbix item keys, so **changing these rules loses Zabbix history
  for affected names**. The registry recomputes IDs from names when loading a snapshot.
  Display names (latest seen) only appear in Zabbix item and trigger *names*.
- Record the client IP from `request.client.host`. **Site detection depends on this
  being the real client IP.** Uvicorn's proxy-header support is on by default and trusts
  only `127.0.0.1`. Behind a reverse proxy, set `FORWARDED_ALLOW_IPS=<proxy IP>`.
  `main()` must keep uvicorn's proxy-header behaviour and not override it. Never parse
  `X-Forwarded-For` by hand, and never trust it from arbitrary sources. If it's misconfigured, every device
  shows the proxy's IP, so each org collapses into one site.
- **`APP_URL` should resolve to IPv4 only** (an A record, no AAAA). On a dual-stack site,
  some devices would otherwise arrive over IPv6 with different addresses and split one
  site in two. IPv6 sources are still accepted and grouped by `/SITE_IPV6_PREFIX`.
- The check-in handler only touches the registry. It **never awaits Zabbix**.

`GET /v1/health` returns `200 {"status": "ok"}` and is unauthenticated, for Docker or a load
balancer. It must not expose org or device data. The Dockerfile `HEALTHCHECK` hardcodes
this path and port 8000, so change them together.

## Configuration (env vars, read by `Settings`)

Keep this table, `Settings` and `compose.yaml` in step. How the container gets its
settings:

- **`compose.yaml` lists them under `environment:`**, most with fixed values. There is no
  `env_file`, so **a variable reaches the container only if it is listed there**.
- **`.env` supplies only what compose interpolates:** `APP_URL`, `API_TOKEN`,
  `ZABBIX_SERVER`, and optionally `ZABBCOMM_IMAGE` (the image to run),
  `FORWARDED_ALLOW_IPS` (default `127.0.0.1`) and `PUBLISH_ADDRESS` (default
  `127.0.0.1`). Change other settings in `compose.yaml`. Keep `.env.example` to this
  set.
- `HOST`, `PORT` and `DATA_DIR` aren't listed, so the code defaults apply.
- **Port 8000 is published on loopback only**, because the app serves plain HTTP and
  expects a TLS reverse proxy. Set `PUBLISH_ADDRESS` if the proxy is elsewhere.
- **Data is bind-mounted from `./docker/data`** (ignored by git and Docker). The one-shot
  `data-permissions` service makes it writable by `nobody` (uid 65534) before the app
  starts.

| Var | Default | Meaning |
|---|---|---|
| `APP_URL` | none | Public base URL that endpoints post to. Documentation and script only; not used for binding |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | uvicorn bind address |
| `API_TOKEN` | **required**, at least 16 chars | Shared bearer token that endpoints send |
| `ZABBIX_SERVER` | **required** | Zabbix server or proxy for sender data. Bare hostname or IPv4 only: no scheme, port, path or IPv6 literal (`zabbix_utils` can't parse them) |
| `ZABBIX_PORT` | `10051` | Trapper port |
| `ZABBIX_HOST` | `zabbcomm` | Zabbix host (template linked) that owns all items |
| `ZABBIX_DRY_RUN` | `false` | Log the payloads instead of sending them (local development) |
| `CHECKIN_INTERVAL` | `900` | Expected seconds between check-ins (the Syncro policy schedule, 15 min) |
| `DEVICE_TIMEOUT` | `2100` | Seconds without a check-in before a device counts as offline (35 min). **Must be more than `2 × CHECKIN_INTERVAL`** or startup fails; half of it is the confirm window |
| `EVAL_INTERVAL` | `60` | Seconds between evaluate-and-send cycles |
| `DISCOVERY_INTERVAL` | `3600` | Full LLD resend period. New orgs and devices are also sent immediately |
| `DEVICE_FORGET_AFTER` | `604800` | Seconds (7 days) after which a silent device is dropped from the registry and discovery. Must be at least `2 × DEVICE_TIMEOUT` |
| `SITE_MIN_DEVICES` | `2` | Devices that must share a WAN IP before it's treated as a site. A single server qualifies regardless |
| `SITE_IPV6_PREFIX` | `64` | IPv6 source addresses are grouped by this prefix length |
| `MAX_ORGS` / `MAX_DEVICES_PER_ORG` | `500` / `1000` | Abuse caps. Reject new entries past them with a logged warning |
| `DATA_DIR` | `/data` | Registry snapshot location. Must be writable by the app's user |
| `LOG_LEVEL` | `INFO` | |

## Design rules

- **Inject the clock.** The registry and evaluator take `now` as a parameter. Tests must
  never sleep.
- **The evaluator is pure.** Given a snapshot and `now` it returns statuses. It does no
  I/O and touches no globals.
- **The registry is the only mutable state.** Mutate it only in sync code with no `await`
  between read and write. That is safe on one event loop without locks. **Route
  handlers that touch it must be `async def`.** A plain `def` handler runs in
  Starlette's threadpool and would race the monitor. Keep it to one uvicorn worker. Multiple workers would split state, so changing that needs a shared
  store and is a design change.
- **Zabbix failures never break check-ins.** Log sender errors and retry on the next
  cycle. A Zabbix outage must not crash the evaluator loop: catch, log, continue.
- **Batch, don't stream.** Send values in one batch per cycle, never one request per
  check-in. It goes in chunks of 250, in order.
- **Every network call needs a timeout.** `zabbix_utils` doesn't time out while reading
  a reply, so `ZabbixSender` bounds each chunk with `SEND_TIMEOUT`.
- **Discovery is its own request, sent before the values.** Zabbix replies only with
  counts, so this is how we know it was accepted. Discovery with any rejected rule
  stays pending and is resent every cycle until accepted.
- **Config errors must not echo values.** `load_settings()` formats validation errors
  without their inputs, because a too-short `API_TOKEN` would otherwise be printed.
- **Startup grace period.** For `DEVICE_TIMEOUT` seconds after startup, send only
  `alive` and status `1` values, never `0`. Devices loaded from the snapshot look stale
  only because the app was down.
- **Persistence.** Write the registry to `DATA_DIR/registry.json` atomically (write a
  temp file, then `os.replace`) each cycle, and load it at startup. Don't add a database
  or ORM without asking.
- **All Zabbix key strings live in the "Zabbix payloads" section.** Never inline them
  elsewhere.
- **Never log `API_TOKEN`** or the `Authorization` header.

## Known limitations and open questions (raise these, don't silently "fix" them)

- **Devices that power off look like an outage.** If every monitored device in an org
  shuts down overnight, the org reads as "internet down". The fix is operational:
  deploy to at least one always-on device per org, or use Zabbix maintenance or
  time-based trigger conditions.
- **Power outage and internet outage look the same.** Word the trigger as "all
  endpoints offline", not as a certain internet failure.
- **WAN IP is only a proxy for a site:**
  - A location with one monitored *workstation* never forms a site, so its outage shows
    as a Warning "endpoint offline". **Don't set `SITE_MIN_DEVICES=1` to change this:**
    every home laptop would then raise "site down".
  - A lone server that is off for longer than `DEVICE_TIMEOUT`, even for maintenance,
    raises High "site offline" (or only the org alert, if the whole org is down). A
    cloud-hosted server with its own public IP is treated as its own site.
  - A site behind two WANs that load-balance per connection may show as two sites.
  - Full-tunnel VPN users appear at the VPN concentrator's site.
  - Dual-WAN failover moves devices to a new site silently. A "WAN IP changed" info
    item could surface that later.
- **An IP change during an outage:** if a site goes down and comes back on a different
  IP, the old site's alert resolves only as its devices check in from the new IP and
  move. That's correct, but it can look odd in Zabbix history.
- **Detection is slow by design.** With 15-minute check-ins, an outage is reported
  about 35 to 36 minutes after the last successful check-in (`DEVICE_TIMEOUT` +
  `EVAL_INTERVAL`). Lowering `DEVICE_TIMEOUT` toward one interval means a single late
  Syncro run raises a false alert. Faster detection would need a more frequent
  check-in, which Syncro can't schedule without installing something on the client.
  The optional agent does that.
- **The agent stores the shared token on each device**, readable by that device's local
  administrators.
- **The Syncro agent is in the path.** If the Syncro agent on a device is broken or
  stopped, that device reads as "endpoint offline". A Syncro-wide delay in running
  scheduled scripts looks like every device across all orgs going offline at once.
  The `alive` trigger won't catch that, because the connector itself is healthy.
- **A Syncro customer rename** makes a new org appear on the next run while the old
  one goes silent until it's forgotten. Expect one false "all endpoints offline"
  alert per rename.
- **A Syncro asset rename** likewise registers a new device, and the old name reports
  "endpoint offline" until it's forgotten.
- **Forgetting removes alerts.** After `DEVICE_FORGET_AFTER`, an org that stays down
  drops out of discovery, and Zabbix removes its items once the LLD "lost resources"
  period passes.
- **Shared token.** One leaked token lets anyone fake check-ins for any org. Per-org
  tokens are a possible later hardening.

## Commands

```bash
uv sync                                            # install deps
uv run zabbcomm                                    # run like the container does
uv run uvicorn zabbcomm:create_app --factory --reload   # dev server
uv run pytest                                      # tests
uv run ruff check . && uv run ruff format --check .
docker compose up -d --build                       # build and run (needs .env)
DATA_DIR=./.devdata uv run --env-file .env zabbcomm   # run without Docker
```

## Conventions

- Python 3.15 with full type hints, async FastAPI, and pydantic v2 models. Use stdlib
  `logging`.
- Run ruff for lint and format before calling work done.
- The Docker image is `python:3.15-alpine` (musl). If a new dependency has no
  `musllinux` wheel for cp315, the build will try to compile it. Check this before
  adding compiled dependencies.
- Releases publish to GHCR through `.github/workflows/docker-publish.yml` when a GitHub
  release is published. Tests and ruff must pass first. Tag releases `X.Y.Z` or
  `vX.Y.Z`: `latest`, `X` and `X.Y` move only for the highest version they cover.
- Area-specific rules live in `.claude/rules/` (Zabbix, PowerShell/Syncro, tests).
