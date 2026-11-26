---
paths:
  - "scripts/**/*.ps1"
  - "scripts/**/*.psm1"
---

# Endpoint check-in script (Syncro / PowerShell) rules

`scripts/Send-ZabbcommCheckin.ps1` runs on client Windows machines we don't control, as
SYSTEM, **directly from a Syncro policy every 15 minutes** (`CHECKIN_INTERVAL`). It has
to be boring, quiet and short.

## Leave no footprint on the client

- **The script installs nothing and changes nothing.** No scheduled tasks, services,
  files, registry keys or modules. Each run sends one HTTP request and exits.
- That was a deliberate choice: we rejected a self-installing Windows Scheduled Task to
  avoid touching client devices. Don't reintroduce local persistence without asking.

## Inputs (injected by Syncro as PowerShell variables)

| Variable | Syncro source | Meaning |
|---|---|---|
| `$orgName` | platform variable | Organization (customer) name, sent as `org` |
| `$assetName` | platform variable | Asset name, sent as `device` |
| `$appUrl` | script variable | zabbcomm base URL |
| `$apiToken` | script variable | The app's `API_TOKEN` |

- **Never assign these in the script** (no `param()` block, no defaults). That would
  break or overwrite Syncro's injected values. The script only checks them.
- **Never commit real values.**
- The device name is the Syncro asset name, **not** `$env:COMPUTERNAME`.
- A missing variable, or an `$appUrl` that isn't `https://` (plain HTTP is allowed only
  for `localhost` / `127.0.0.1` testing), is a setup error and exits non-zero.

## Behaviour

- Use `Invoke-WebRequest -UseBasicParsing` so the status code can be checked: **only
  `204` counts as success.** A captive portal answering `200` must not look like a
  check-in. Use `-MaximumRedirection 0` and `-TimeoutSec 15`.
- **Send the JSON body as UTF-8 bytes.** Windows PowerShell 5.1 mangles non-ASCII names
  in a string body.
- The body matches the API contract in `.claude/CLAUDE.md`: `org`, `device` and
  `agent_version`.
- **Don't look up or send the WAN IP.** The server reads it from the connection's source
  address. A "what's my IP" lookup would add a third-party dependency that fails during
  outages, and could disagree with the path actually used.
- **An undelivered check-in is a normal outcome.** When the internet is down the request
  *will* fail. For no connection, timeouts, redirects and server errors, write one line
  and **exit 0**. A non-zero exit or `Rmm-Alert` would create Syncro noise for every
  device during an outage, and Zabbix is the alerting system.
- **A rejection is a setup error:** `401` (token), `403` (registration limit) and `422`
  (bad name) exit `1` so they show up in Syncro. Don't add `404`, because some reverse
  proxies return it while the app is down.
- Make no retries and no loops, because the next scheduled run is the retry. Keep the
  runtime under about 20 seconds.
- Keep output minimal: one short line saying success or failure, since Syncro stores
  script output per run. **Never print the token.**

## Compatibility

- Target **Windows PowerShell 5.1**, not PowerShell 7. Don't use `??`, ternary
  `? :`, `&&` / `||`, `ForEach-Object -Parallel`, `-SkipCertificateCheck`,
  `ConvertFrom-Json -AsHashtable`, or any other 6+/7+ only syntax.
- Force TLS 1.2 before the web call:
  `[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12`
- Use only built-in cmdlets.
- Keep the script file ASCII-only, so its encoding survives being pasted into Syncro.

## Versioning

- A `$AgentVersion` constant is sent as `agent_version`. Bump it whenever the script
  changes behaviour. Since Syncro holds the deployed copy, the version is how we tell
  which copy endpoints are running.
