# Stable Apple Home Layout After HA Restart

By default, HA's HomeKit bridge starts immediately on boot — before all
entities are fully loaded. This causes accessory IDs (aid/iid) to shift,
which makes Apple Home forget room assignments.

The fix is two parts:
1. Tell the HomeKit bridge not to auto-start
2. Start it via an automation 30 seconds after HA boots (giving all entities time to load)

---

## Step 1 — configuration.yaml

Add or update your HomeKit config:

```yaml
homekit:
  - name: Home
    port: 21063
    auto_start: false   # <-- key line
```

If you already have a homekit section, just add `auto_start: false` to it.

---

## Step 2 — Automation

Go to Settings → Automations → Create Automation → Edit in YAML,
paste the contents of `docs/homekit_start_automation.yaml`.

Or import it directly from the file.

---

## Step 3 — Assign rooms once in Apple Home

After applying the fix:
1. Restart HA
2. Wait ~30 seconds for HomeKit to start
3. Open Apple Home and assign every device to its room
4. These assignments will now persist across all future restarts

---

## Why this works

HomeKit accessory IDs (aid) are assigned based on the order entities appear
in the entity registry. If HomeKit starts before all entities are registered,
the order is inconsistent and IDs shift. Waiting 30 seconds ensures the full
registry is stable before HomeKit reads it.
