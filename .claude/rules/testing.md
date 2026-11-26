---
paths:
  - "tests/**"
---

# Testing rules

- Use pytest. For the API, use FastAPI's `TestClient`, or `httpx.AsyncClient` with
  `ASGITransport`, against `create_app(settings=...)` with explicit test settings. Never
  rely on the real environment.
- **No real network, no real sleeping.** Use a fake clock (pass `now`) and a fake sender
  that records payloads. The evaluator loop body should be callable as one `run_cycle(now)`
  step so tests can drive it.
- Use `tmp_path` for `DATA_DIR`.

## Scenarios that must stay covered

1. All devices in an org go stale: `org.status=0`, each site gets `site.status=2` and
   each device gets `device.status=2`.
2. One device goes stale while the others at its site stay fresh: that device gets
   `device.status=0` and both its site and the org stay at `1`.
2a. Every device at site A goes stale while site B is fresh: `site.status[A]=0`, A's
   devices get `2`, and the org stays at `1`.
2b. Site formation: one workstation on a WAN IP stays unsited (no site discovery
   entry). Reaching `SITE_MIN_DEVICES` creates the site, and so does a single server.
   A check-in that omits `is_server` keeps the stored value.
2c. WAN IP change: every device at site A checks in from a new IP. They move to the new
   site, A drops out of discovery, and **A never reports `0`**.
2d. An unsited device (say a home laptop) goes stale: `device.status=0` and no site
   items appear for it.
2e. IPv6 sources in the same `/SITE_IPV6_PREFIX` map to one site, and IPv4 gets its
   exact-address `site_id`.
2f. With proxy headers configured, a forwarded client IP from a trusted proxy is what
   gets recorded.
3. A stale device checks in again and recovers to `1`, and the org recovers.
4. A new org or device: the discovery payload is sent before or with its first values,
   in the same cycle.
5. Startup grace: devices loaded stale from the snapshot produce no `0` values until
   `DEVICE_TIMEOUT` has passed since startup.
6. A device past `DEVICE_FORGET_AFTER` disappears from the registry and from discovery.
7. Auth: a missing or wrong token gives 401, and the token never appears in logs (use
   `caplog`).
8. Validation: empty, overlong or control-character names give 422. Names differing only
   in case or punctuation map to the same slug.
9. Caps: `MAX_ORGS` / `MAX_DEVICES_PER_ORG` are enforced.
10. The sender raises or reports failures: the cycle logs and continues, check-ins still
    succeed, and the next cycle resends.
11. Persistence round-trip: save, load, and the same registry comes back. Loading
    recomputes IDs from names.
12. Outages over time (`tests/test_outages.py`): with staggered, late check-ins, an org
    or site outage and its recovery raise **only** the one alert at that level. A single
    dead device is reported without extra delay. Any change to `evaluate` or to the
    registry's timing fields must keep these passing.
13. No token means 401 before the body is read; oversized bodies are 413.
14. A Zabbix server that never replies times out.
15. Deny list (`tests/test_deny.py`): denied devices get a signed `410` and leave the
    registry; no signature without a valid nonce or token; an invalid file denies
    nobody; changes apply without a restart.
