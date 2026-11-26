---
name: simulate-checkins
description: Run zabbcomm locally and simulate endpoint check-ins from fake orgs, sites (WAN IPs) and devices to exercise the endpoint-down, site-down, org-down and WAN-IP-change paths end to end. Use when asked to try, demo, or manually verify the check-in -> evaluation -> Zabbix flow without a real Zabbix server or real endpoints.
---

# Simulate check-ins locally

Fake WAN IPs come in through `X-Forwarded-For`. Uvicorn honours proxy headers from
`127.0.0.1` by default (`FORWARDED_ALLOW_IPS`), so local curl calls can pose as any
site. The addresses below are documentation ranges (RFC 5737).

1. **Start the app** in dry-run mode with short timers. Run it in the background and keep
   its log output:

   ```bash
   API_TOKEN=dev-token ZABBIX_SERVER=127.0.0.1 ZABBIX_DRY_RUN=true \
   DEVICE_TIMEOUT=20 EVAL_INTERVAL=5 DATA_DIR=./.devdata LOG_LEVEL=DEBUG \
   uv run zabbcomm
   ```

   Startup grace equals `DEVICE_TIMEOUT`, so expect no `0` values for the first 20s.

2. **Define the helpers and the fleet:**

   ```bash
   post() { curl -s -o /dev/null -w "%{http_code} $1/$2 via $3\n" -X POST http://127.0.0.1:8000/api/v1/checkin \
     -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
     -H "X-Forwarded-For: $3" -d "{\"org\":\"$1\",\"device\":\"$2\"}"; }
   HQ=203.0.113.10; BRANCH=198.51.100.20; HOME=192.0.2.50; GLOBEX=203.0.113.99
   hq()     { for d in ACME-WS01 ACME-WS02 ACME-SRV01; do post "Acme Inc" $d ${1:-$HQ}; done; }
   branch() { for d in ACME-BR01 ACME-BR02; do post "Acme Inc" $d $BRANCH; done; }
   laptop() { post "Acme Inc" ACME-LT01 $HOME; }
   globex() { post "Globex" GLOBEX-WS01 $GLOBEX; }
   hq; branch; laptop; globex
   ```

   Expected after one cycle: site discovery holds `acme-inc/203-0-113-10` and
   `acme-inc/198-51-100-20`. The laptop and Globex have **no** site entry, because each
   is a single device on its IP (`SITE_MIN_DEVICES=2`).

Each scenario below loops 6 times, 5 seconds apart (about 30s, past `DEVICE_TIMEOUT`),
checking in only the devices that should stay alive.

3. **Single endpoint down:** keep everything alive except `ACME-WS02`.

   ```bash
   for i in $(seq 1 6); do post "Acme Inc" ACME-WS01 $HQ; post "Acme Inc" ACME-SRV01 $HQ; branch; laptop; globex; sleep 5; done
   ```

   Expected: `device.status[acme-inc,acme-ws02] = 0`, HQ `site.status = 1`, and
   `org.status[acme-inc] = 1`.

4. **Site down:** stop the branch, keep the rest going.

   ```bash
   for i in $(seq 1 6); do hq; laptop; globex; sleep 5; done
   ```

   Expected: `site.status[acme-inc,198-51-100-20] = 0`, both `ACME-BR0x` at `2`, and
   `org.status[acme-inc] = 1`.

5. **Org down plus unsited endpoint:** stop Globex and the laptop.

   ```bash
   for i in $(seq 1 6); do hq; branch; sleep 5; done
   ```

   Expected: `org.status[globex] = 0` with `GLOBEX-WS01 = 2`. The laptop gets
   `device.status[acme-inc,acme-lt01] = 0` and no site item.

6. **WAN IP change:** HQ moves to a new address.

   ```bash
   for i in $(seq 1 6); do hq 203.0.113.11; branch; laptop; globex; sleep 5; done
   ```

   Expected: a new site `203-0-113-11` with HQ's three devices, all at `1`. The old
   `203-0-113-10` drops out of site discovery and **never reports `0`**.

7. **Negative checks:** a wrong token should return `401`, and `{"org":"","device":"x"}`
   should return `422`.

8. Stop the app and delete `./.devdata`.

Report what was observed against each expectation. Quote the relevant dry-run log lines
rather than summarising them, and call out any mismatch. If the endpoint path, env vars,
key names or dry-run log format have changed, update this skill to match.
