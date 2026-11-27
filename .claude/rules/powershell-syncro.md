---
paths:
  - "scripts/**/*.ps1"
  - "scripts/**/*.psm1"
---

# Endpoint scripts (Syncro / PowerShell) rules

Two ways to check in. Inputs, compatibility and versioning rules below apply to both.
"Leave no footprint" and "Behaviour" describe the direct script,
`Send-ZabbcommCheckin.ps1`. The agent is covered under "Agent".

## Agent (`scripts/agent/`)

- The agent script is **embedded in `Install-ZabbcommAgent.ps1`** as a single-quoted
  here-string, so Syncro needs one file. No line in it may start with `'@`.
- The installer is idempotent: it changes nothing on a healthy device. "Healthy" means
  locked folder, exact script and config, correct task, and a run within 3 intervals.
  An unreachable server is still healthy; rejected check-ins exit `1`.
- **The install folder runs as SYSTEM and holds the token.** It must be owned and
  writable only by SYSTEM and Administrators. Any user can create folders in
  `ProgramData`, so a folder that isn't locked down is deleted and recreated, never
  reused. Never put the token in task arguments.
- **Never look inside or delete a folder an untrusted user can change.** Its owner can
  swap a subfolder for a link between our check and our delete, and SYSTEM would then
  delete files elsewhere. An install folder that fails `Test-LockedDirectory` is renamed
  aside unopened (`Move-Aside`) and reported. `Remove-Tree` is only for the locked
  folder, and never `Remove-Item -Recurse`.
- **Self-removal has exactly two triggers:** a `410` with a valid
  `X-Zabbcomm-Uninstall` signature for that request's nonce (keep the server's
  `uninstall_signature` and `Test-UninstallOrder` in step), or expiry.
- **Expiry:** the installer pings (`ping.txt`, new value each run) **before** validating
  Syncro variables, so a policy mistake can't expire working agents. The agent is the
  only writer of `usage.json`, and resets its counts when the ping changes. Count runs;
  never compare dates. Only a `204` counts as online. Unreadable counters restart from
  zero, so errors delay removal rather than cause it.
- Test expiry by preloading `usage.json` with the current ping and counts just below
  the limit.
- Test with `$env:ProgramData` redirected and the ScheduledTasks commands replaced.
  `Import-Module ScheduledTasks` **before** defining stand-ins, or loading the module
  overwrites them. Never register a real task on a developer machine without asking.

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
| `$deviceType` | dropdown variable, set per policy (servers have their own policy) | `server` or `user`, sent as `is_server` |
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
- The body matches the API contract in `.claude/CLAUDE.md`: `org`, `device`,
  `agent_version` and `is_server`.
- `is_server` comes **only from `$deviceType`** (`server` → true, `user` → false,
  case-insensitive). Don't detect it from the operating system: some clients' servers
  don't run Windows Server, and the Syncro policy is the source of truth.
- **A missing or invalid `$deviceType` must not block the check-in**, or a policy mistake
  would make every device in it look offline. Omit `is_server` (the app keeps its stored
  value), check in, then exit `1`. An undelivered check-in still exits `0`.
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
