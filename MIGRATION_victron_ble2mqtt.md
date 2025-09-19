# Migration Guide: Minimal Bridge -> victron-ble2mqtt

This document explains how to migrate from the lightweight `victron_bridge.py` implementation to the upstream `victron-ble2mqtt` project while preserving stability and enabling rollback.

## 1. Why Migrate?
Choose migration if you want:
- Built-in device discovery and interactive config management
- Publish throttling parameters without local code edits
- Potential Home Assistant–aligned outputs / additional sensors
- Automated systemd helper commands

Stay on the minimal bridge if you prioritize:
- Maximum control of topic naming
- Lowest possible runtime footprint
- Very small, auditable code base
 - Recently added optional features (TLS, per-metric thresholds, derived metrics, Prometheus exporter) already cover your needs

## 2. Pre-Migration Checklist
| Item | Why |
|------|-----|
| Working backup of current repo | Fast rollback |
| Note existing MQTT topic consumers (HA, Grafana, Node-RED) | Update if schema changes |
| Device keys handy | Needed for new settings file |
| Baseline CPU/RAM metrics | Compare after migration |

Gather baseline (5–10 minutes runtime):
```bash
top -b -n 2 | grep -i python  # capture CPU once stable
ps -o pid,rss,cmd -C python3
mosquitto_sub -t 'victron/#' -C 50 > baseline_messages.txt
```

## 3. Install victron-ble2mqtt
```bash
cd ~
git clone https://github.com/jedie/victron-ble2mqtt.git
cd victron-ble2mqtt
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
./cli.py --help
```
(Or simply: `pip install victron-ble2mqtt` inside your existing environment.)

## 4. Discover Devices
```bash
./cli.py discover
```
Record addresses. Confirm your device appears.

## 5. Edit Settings
```bash
./cli.py edit-settings
```
Enter:
- MQTT host/port/user/pass
- List of device keys (the tool may support multiple devices)
- Optional throttle/publish settings

## 6. Test Publish Loop
```bash
./cli.py publish-loop -vv
mosquitto_sub -v -t '#'
```
Verify expected topics. Compare naming vs old `<base>/<name>/<metric>` pattern.

## 7. Systemd Integration
Option A (tool-managed):
```bash
./cli.py systemd-setup
./cli.py systemd-status
```
Option B (manual service replace) - edit your existing unit:
```
ExecStart=/home/pi/victron-ble2mqtt/.venv/bin/python /home/pi/victron-ble2mqtt/cli.py publish-loop
WorkingDirectory=/home/pi/victron-ble2mqtt
```
Then:
```bash
sudo systemctl daemon-reload
sudo systemctl restart victron-ble-bridge
journalctl -u victron-ble-bridge -f -o cat
```

## 8. Benchmark Post-Migration
Repeat baseline steps; compare CPU, RSS, message rate. If CPU > +20% or memory > +30% vs baseline and you are resource constrained, reconsider.

## 9. Update Home Assistant / Dashboards
If topic naming changed, adjust HA MQTT sensor definitions or rely on new discovery entities. Remove obsolete retained topics if desired:
```bash
# Example: purge old retained metric
mosquitto_pub -t 'victron/old_device/voltage' -r -n
```

## 10. Rollback Procedure
```bash
sudo systemctl stop victron-ble-bridge
# Restore service file pointing to python /home/pi/solar_control/victron_bridge.py config.yaml
sudo systemctl daemon-reload
sudo systemctl start victron-ble-bridge
```
Remove or disable new victron-ble2mqtt unit if one was created.

## 11. Hybrid Strategy
You can keep both:
- Keep minimal bridge for core metrics
- Run victron-ble2mqtt in a separate namespace (different base topic) briefly for comparison
Then fully switch once validated.

If after enabling the local bridge's optional features (see README: TLS, per-metric thresholds, derived metrics `power_w`, Prometheus `/metrics`) the additional functionality of victron-ble2mqtt is marginal for your use case, you may defer migration indefinitely.

## 12. Post-Migration Cleanup
- Remove unused retained topics (publish retained nulls)
- Archive baseline vs new benchmark results in repo NOTES
- Update `copilot-instructions.md` to reflect new architecture (if you abandon the minimal bridge)

## 13. Troubleshooting
| Issue | Action |
|-------|--------|
| Missing sensors | Re-run `publish-loop -vv` and inspect logs |
| High CPU | Tune built-in throttle / reduce discovery verbosity |
| Keys rejected | Re-check copied advertisement keys; ensure no whitespace |
| systemd not starting | Inspect `./cli.py systemd-debug` or use `systemctl status` |

---
Migration is optional—only proceed if the feature delta clearly outweighs the extra complexity.
