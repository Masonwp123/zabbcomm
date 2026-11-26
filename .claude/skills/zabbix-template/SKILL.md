---
name: zabbix-template
description: Create or update zabbix/zabbcomm_template.yaml (the Zabbix template export with trapper items, LLD rules, item/trigger prototypes) so it matches the Zabbix keys defined in src/zabbcomm/__init__.py. Use when adding/renaming/removing a Zabbix item or trigger, or when asked to produce the template for import.
---

# Maintain the Zabbix template

The template and the "Zabbix payloads" section of `src/zabbcomm/__init__.py` form one
contract. If they drift, Zabbix quietly rejects values (`failed: N` in the sender
responses) and alerts stop firing.

1. **Read the contract.** Read the key constants and builders in the "Zabbix payloads"
   section of `src/zabbcomm/__init__.py`, plus the key table and trigger design in
   `.claude/rules/zabbix.md`. If the code and the rules file disagree, stop and ask
   which one is right.

2. **Check the Zabbix server version.** It's 7.4, as recorded in the template. The
   export's `version:` field must not be newer than the server.

3. **Edit `zabbix/zabbcomm_template.yaml`:**
   - Template name `zabbcomm by trapper`, linked to host `ZABBIX_HOST` (default
     `zabbcomm`). Templates and hosts share one namespace in Zabbix, so the template
     can't also be called `zabbcomm`. Trigger expressions in the template reference the
     template name, e.g. `last(/zabbcomm by trapper/zabbcomm.org.status[{#ORGID}])=0`.
   - `zabbcomm.alive`: a plain trapper item with its `nodata` trigger.
   - Three LLD rules of type Zabbix trapper (`zabbcomm.org.discovery`,
     `zabbcomm.site.discovery`, `zabbcomm.device.discovery`), each with item prototypes
     and trigger prototypes.
   - Trigger prototypes **depend on** the connector `alive` trigger.
   - Item and trigger *names* use display macros (`{#ORG}`, `{#SITE}`, `{#DEVICE}`).
     *Keys* use ID macros (`{#ORGID}`, `{#SITEID}`, `{#DEVICEID}`) only.
   - Give every new entity a fresh `uuid` (32 lowercase hex chars, e.g.
     `python -c "import uuid;print(uuid.uuid4().hex)"`). **Never change an existing uuid.**
   - Leave trapper "Allowed hosts" empty in the template and set it per environment.

4. **Check consistency.** Every key the app actually sends should appear in the YAML,
   and the other way round. Keys are built by functions, so generate them from the code
   rather than grepping it:

   ```bash
   uv run python -c "import zabbcomm as z; d=[z.Device('o',i,'O',i,0,'203.0.113.1') for i in 'ab']; ev=z.evaluate(d,0,device_timeout=1,site_min_devices=2,ipv6_prefix=64); print(chr(10).join(sorted({k.split('[')[0] for k,_ in z.build_values(ev,in_grace=False)}|set(z.build_discovery(ev)))))" | tr -d '\r' > /tmp/code_keys
   grep -oE 'zabbcomm\.[a-z_.]+[a-z_]' zabbix/zabbcomm_template.yaml | sort -u > /tmp/tpl_keys
   diff /tmp/code_keys /tmp/tpl_keys
   ```

   Also check that the YAML parses: `uv run python -c "import yaml,sys;yaml.safe_load(open('zabbix/zabbcomm_template.yaml'))"`
   (use `uv run --with pyyaml` if PyYAML isn't a dependency).

5. **Report.** List what changed. Flag any **renamed or removed key** as breaking,
   because history is lost and the item is recreated. Remind the user to import the file
   in Zabbix (Data collection > Templates > Import, with "Update existing" checked).
