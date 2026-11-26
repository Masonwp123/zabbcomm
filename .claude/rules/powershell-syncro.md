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

## Inputs (Syncro script variables, injected as PowerShell variables)

- **Org name:** a Syncro **platform variable** mapped to the customer name. Use the exact
  platform variable name shown in Syncro's script editor, and note which one at the top
  of the script.
- **`APP_URL` and the API token:** Syncro script variables. **Never commit real values.**
  The repo copy only declares the variables and exits with a clear message if one is
  empty. This is a configuration error, so it exits non-zero.
- **Device name:** `$env:COMPUTERNAME`.

## Behaviour

- Send:
  `Invoke-RestMethod -Method Post -Uri "$AppUrl/api/v1/checkin" -Headers @{Authorization = "Bearer $Token"} -ContentType 'application/json' -Body ($body | ConvertTo-Json -Compress) -TimeoutSec 15`
- The body matches the API contract in `.claude/CLAUDE.md`: `org`, `device` and
  `agent_version`.
- **Don't look up or send the WAN IP.** The server reads it from the connection's source
  address. A "what's my IP" lookup would add a third-party dependency that fails during
  outages, and could disagree with the path actually used.
- **A failed request is a normal outcome.** When the internet is down the request *will*
  fail. Catch it, write one line, and **exit 0**. A non-zero exit or `Rmm-Alert` would
  create Syncro noise for every device during an outage, and Zabbix is the alerting
  system.
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

## Versioning

- A `$AgentVersion` constant is sent as `agent_version`. Bump it whenever the script
  changes behaviour. Since Syncro holds the deployed copy, the version is how we tell
  which copy endpoints are running.
