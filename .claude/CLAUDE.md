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
   **Syncro platform variables** and POSTs them to `APP_URL`. It **installs nothing on
   the client**: no scheduled tasks, no files. See
   `.claude/rules/powershell-syncro.md`.
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

### Sites (WAN IP grouping)

- **The server observes the WAN IP; the endpoint never reports it.** The source address
  of the check-in (`request.client.host`) *is* the site's public IP. It's free, it's the
  path actually in use, and it needs no third-party "what's my IP" lookup on the
  endpoint (which would fail in exactly the outages we care about).
- `site_id` = slug of the WAN IP (IPv4 address, or IPv6 `/SITE_IPV6_PREFIX` network).
  Display name `{#SITE}` = the IP. Friendly site names can come later as a config
  mapping, but don't build that unasked.
- **A WAN IP counts as a site only once at least `SITE_MIN_DEVICES` devices (default 2)
  last checked in from it.** Devices on a WAN IP below that are *unsited*: they're
  judged against the org only. This keeps a laptop at someone's home from becoming a
  "site down" alert every evening.
- **IP changes move devices, they don't raise alarms.** Membership follows each
  device's latest IP. If a site's ISP address changes (DHCP renewal, ISP swap, dual-WAN
  failover), its devices check in from the new IP and move to a new site. The old site
  ends up with no members and drops out of discovery. It is **not** reported down. Only
  devices whose *last known* site is X can make X go down.

**Guiding principle:** the app decides *state* and Zabbix decides *alerting* (severity,
escalation, maintenance windows, business hours). Don't put alert policy in Python.

## Status

- **Done:** the Python connector (tests in `tests/test_zabbcomm.py`), the endpoint
  script `scripts/Send-ZabbcommCheckin.ps1`, and the Zabbix 7.4 template
  `zabbix/zabbcomm_template.yaml`.
- **Not yet verified:** importing the template into a real Zabbix server.

## Layout

The whole app is **one module, `src/zabbcomm/__init__.py`**, ordered from pure logic to
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

- Keep it a single file. It's about 680 lines now. If it passes about 900 lines, or a
  section starts needing its own helpers, split along those section lines.
- **Importing `zabbcomm` must have no side effects.** No module-level `Settings()` and
  no module-level `app`. Settings are read only in `create_app()` and `main()`.
- `create_app(settings, sender=, clock=)` takes everything injectable. Tests pass a
  fake sender and a fixed clock, and `app.state.registry` exposes state.

```
scripts/
  Send-ZabbcommCheckin.ps1   Syncro policy script, runs every 15 min, one check-in per run
zabbix/
  zabbcomm_template.yaml     Zabbix template export (items, LLD rules, triggers)
tests/
  test_zabbcomm.py
compose.yaml                 Docker Compose deployment, configured through .env
Dockerfile
```

## API contract

`POST /api/v1/checkin`, header `Authorization: Bearer <API_TOKEN>`

```json
{ "org": "Acme Inc", "device": "ACME-WS01", "agent_version": "1.0.0" }
```

- `org` and `device` are required, trimmed, 1 to 128 chars, with no control characters.
  `agent_version` is optional. Ignore unknown fields. There is deliberately **no WAN IP
  field**, because the site comes from the request's source address.
- Returns:
  - `204` on success.
  - `401` for a bad or missing token. Auth is checked before the body, so
    unauthenticated callers can't probe validation.
  - `422` for validation errors.
  - `403` when `MAX_ORGS` or `MAX_DEVICES_PER_ORG` would be exceeded.

  The response body must not echo the token or internal state.
- Interactive docs and the OpenAPI schema are disabled, because the app faces the
  internet.
- Names with no ASCII letters or digits (for example non-Latin scripts) get a hash ID,
  `h-<12 hex>`, instead of being rejected.
- **IDs:** `org_id` and `device_id` are slugs of the names (casefold, runs of non
  `[a-z0-9]` become `-`, trimmed). IDs key everything, including Zabbix item keys. The
  display names (latest seen) only appear in Zabbix item and trigger *names*.
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
balancer. It must not expose org or device data.

## Configuration (env vars, read by `Settings`)

Keep this table, `Settings` and `.env.example` in step. `compose.yaml` pins `HOST`,
`PORT` and `DATA_DIR` inside the container and adds `PUBLISH_ADDRESS` (default
`127.0.0.1`), `PUBLISH_PORT` (`8000`) and `ZABBCOMM_IMAGE` for the host side.

| Var | Default | Meaning |
|---|---|---|
| `APP_URL` | none | Public base URL that endpoints post to. Documentation and script only; not used for binding |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | uvicorn bind address |
| `API_TOKEN` | **required**, at least 16 chars | Shared bearer token that endpoints send |
| `ZABBIX_SERVER` | **required** | Zabbix server or proxy for sender data. Bare hostname or IPv4 only: no scheme, port, path or IPv6 literal (`zabbix_utils` can't parse them) |
| `ZABBIX_PORT` | `10051` | Trapper port |
| `ZABBIX_HOST` | `zabbcomm` | Zabbix host (template linked) that owns all items |
| `ZABBIX_DRY_RUN` | `false` | Log the payloads instead of sending them (local development) |
| `CHECKIN_INTERVAL` | `900` | Expected seconds between check-ins (the Syncro policy schedule, 15 min). Used only to sanity-check `DEVICE_TIMEOUT` |
| `DEVICE_TIMEOUT` | `2100` | Seconds without a check-in before a device counts as offline: 35 min, i.e. two missed runs plus 5 min slack for Syncro scheduling jitter. At startup, log a warning if it's below `2 × CHECKIN_INTERVAL` |
| `EVAL_INTERVAL` | `60` | Seconds between evaluate-and-send cycles |
| `DISCOVERY_INTERVAL` | `3600` | Full LLD resend period. New orgs and devices are also sent immediately |
| `DEVICE_FORGET_AFTER` | `604800` | Seconds (7 days) after which a silent device is dropped from the registry and discovery |
| `SITE_MIN_DEVICES` | `2` | Devices that must share a WAN IP before it's treated as a site |
| `SITE_IPV6_PREFIX` | `64` | IPv6 source addresses are grouped by this prefix length |
| `MAX_ORGS` / `MAX_DEVICES_PER_ORG` | `500` / `1000` | Abuse caps. Reject new entries past them with a logged warning |
| `DATA_DIR` | `/app/data` | Registry snapshot location. Mount it as a volume; it must be writable by `nobody` |
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
  check-in. `zabbix_utils` sends it in chunks of 250, in order.
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
  - A small branch with one monitored device never forms a site, so its outage shows as
    "endpoint offline".
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
  check-in, which Syncro can't schedule without installing something on the client,
  and we rejected that.
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
uv run --env-file .env zabbcomm                    # run with .env, without Docker
```

## Conventions

- Python 3.15 with full type hints, async FastAPI, and pydantic v2 models. Use stdlib
  `logging`.
- Run ruff for lint and format before calling work done.
- The Docker image is `python:3.15-alpine` (musl). If a new dependency has no
  `musllinux` wheel for cp315, the build will try to compile it. Check this before
  adding compiled dependencies.
- Releases publish to GHCR through `.github/workflows/docker-publish.yml` when a GitHub
  release is published.
- Area-specific rules live in `.claude/rules/` (Zabbix, PowerShell/Syncro, tests).
