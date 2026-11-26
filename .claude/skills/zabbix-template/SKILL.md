---
name: zabbix-template
description: Create or update zabbix/zabbcomm_template.yaml (the Zabbix template export with trapper items, LLD rules, item/trigger prototypes) so it matches the item key constants in src/zabbcomm/zabbix.py. Use when adding/renaming/removing a Zabbix item or trigger, or when asked to produce the template for import.
---

# Maintain the Zabbix template

The template and `src/zabbcomm/zabbix.py` form one contract. If they drift, Zabbix quietly
rejects values (`failed: N` in the sender responses) and alerts stop firing.

1. **Read the contract.** Read the key constants in `src/zabbcomm/zabbix.py` and the key
   table and trigger design in `.claude/rules/zabbix.md`. If the code and the rules file
   disagree, stop and ask which one is right.

2. **Confirm the Zabbix server version** with the user if it isn't already recorded in
   the template. The export's `version:` field must not be newer than the server.

3. **Edit `zabbix/zabbcomm_template.yaml`:**
   - Template name `zabbcomm`, linked to host `ZABBIX_HOST`.
   - `zabbcomm.alive`: a plain trapper item with its `nodata` trigger.
   - Two LLD rules of type Zabbix trapper (`zabbcomm.org.discovery`,
     `zabbcomm.device.discovery`), each with item prototypes and trigger prototypes.
   - Trigger prototypes **depend on** the connector `alive` trigger.
   - Item and trigger *names* use display macros (`{#ORG}`, `{#DEVICE}`). *Keys* use ID
     macros (`{#ORGID}`, `{#DEVICEID}`) only.
   - Give every new entity a fresh `uuid` (32 lowercase hex chars, e.g.
     `python -c "import uuid;print(uuid.uuid4().hex)"`). **Never change an existing uuid.**
   - Leave trapper "Allowed hosts" empty in the template and set it per environment.

4. **Check consistency.** Every key constant in `zabbix.py` should appear in the YAML and
   the other way round:

   ```bash
   grep -oE 'zabbcomm\.[a-z.]+' src/zabbcomm/zabbix.py | sort -u > /tmp/code_keys
   grep -oE 'zabbcomm\.[a-z.]+' zabbix/zabbcomm_template.yaml | sort -u > /tmp/tpl_keys
   diff /tmp/code_keys /tmp/tpl_keys
   ```

   Also check that the YAML parses: `uv run python -c "import yaml,sys;yaml.safe_load(open('zabbix/zabbcomm_template.yaml'))"`
   (use `uv run --with pyyaml` if PyYAML isn't a dependency).

5. **Report.** List what changed. Flag any **renamed or removed key** as breaking,
   because history is lost and the item is recreated. Remind the user to import the file
   in Zabbix (Data collection > Templates > Import, with "Update existing" checked).
