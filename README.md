# Victron BLE → MQTT Bridge (Lawnberry Pi / Pi Zero 2W Optimized)

Lightweight Python bridge that decrypts Victron Energy *Instant Readout* BLE advertisements and republishes decoded metrics to MQTT with optional Home Assistant (HA) Discovery. Designed for extremely small footprint and deterministic behavior on a Raspberry Pi Zero 2W.

## Key Features
- Victron BLE parsing (compatibility shim for old/new `victron_ble` APIs)
- Individual metric retained topics + compact device state JSON
- Home Assistant MQTT Discovery (lazy metric announcement)
- Throttling of unchanged frames (`throttle_seconds`) + optional per-metric delta thresholds
- Per‑device availability topics (online/offline) using inactivity timeout
- Bridge stats JSON (uptime + counters) + optional Prometheus `/metrics`
- Optional publication of unknown Victron devices (raw + minimal state)
- Graceful lifecycle (`<base>/bridge/state`) with MQTT LWT
- All MQTT publishes `retain=True` for fast subscriber bootstrap

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.yaml.example config.yaml  # if you create an example file
# Edit config.yaml with device MAC + advertisement key
python victron_bridge.py config.yaml
```

Systemd (Pi Zero 2W):
```bash
sudo cp victron-ble-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now victron-ble-bridge
journalctl -u victron-ble-bridge -f -o cat
```

## Configuration (`config.yaml`)
```yaml
mqtt:
  host: 127.0.0.1
  port: 1883
  username: ""
  password: ""
  base_topic: victron
  # Optional TLS (uncomment + adjust paths)
  # tls:
  #   enabled: true
  #   ca_cert: /etc/ssl/certs/ca.crt
  #   certfile: /etc/ssl/certs/client.crt
  #   keyfile: /etc/ssl/private/client.key

bridge:
  throttle_seconds: 5              # Suppress unchanged publishes within window (0 = disable)
  home_assistant_discovery: true   # Publish HA discovery configs
  publish_unknown_devices: false   # Publish raw adverts for un-configured MACs
  device_timeout_seconds: 120      # Offline threshold for availability
  stats_interval_seconds: 60       # Interval for bridge stats JSON
  web_ui_port: 0                   # >0 to enable built-in lightweight HTML UI
  # unknown_topic: victron/unknown # Optional override (default <base>/unknown)
  # Per-metric delta thresholds (only publish if change >= threshold)
  per_metric_thresholds:
    voltage: 0.02   # Volts
    current: 0.05   # Amps
    power_w: 1      # Watts
  enable_basic_derived_metrics: true  # Derive power_w = voltage * current when absent
  prometheus_port: 0                  # >0 to enable /metrics exporter

victron:
  devices:
    "AA:BB:CC:DD:EE:FF":
      name: smartsolar_lawnberry_pi
      adv_key: eb8c557386614231dbd741db97e457c5

control:
  enabled: false                 # Enable VE.Direct load control
  vedirect_port: /dev/ttyUSB0    # VE.Direct USB serial device path
  control_device_name: controller
  method: vedirect               # vedirect | modbus
  modbus:
    unit_id: 1                   # Typical SmartSolar unit id
    load_register: 0x0120        # Placeholder register (update per Victron Modbus doc)
    on_value: 1
    off_value: 0
  sunrise_sunset:
    enabled: false
    latitude: 0.0
    longitude: 0.0
    on_at_sunrise: true
    off_at_sunset: true
    sunrise_offset_min: 0
    sunset_offset_min: 0
```
Notes:
- MACs uppercased internally.
- `adv_key` must be 32 hex chars (device advertisement/encryption key from Victron app logs).
- `base_topic` trailing slash removed automatically.
- Unknown devices only published (not decrypted) if `publish_unknown_devices: true`.
- Thresholds apply after throttling accepts a frame; suppressed metrics increment `metric_suppressed` counter (Prometheus + JSON stats).
- Derived metrics currently limited to `power_w` (rounded to 0.01) when both voltage & current present.
 - Load control optional; requires VE.Direct cable. Commands optimistic until next frame confirms.

## MQTT Topic Layout
Per configured device `name` (e.g. `smartsolar_lawnberry_pi`):
```
<base>/<name>/<metric>
<base>/<name>/state          # JSON {mac,rssi,type,values}
<base>/<name>/availability   # "online" | "offline"
```
Bridge / global:
```
<base>/bridge/state          # online|offline (LWT)
<base>/bridge/stats          # JSON stats (retained)
```
Unknown (if enabled):
```
<base>/unknown/<MAC>/state   # {mac,rssi,last_seen,count}
<base>/unknown/<MAC>/raw     # hex payload
```
Home Assistant discovery configs:
```
homeassistant/sensor/<device>_<metric>/config
homeassistant/switch/<control_device_name>_load_switch/config
```
All published with retain for rapid subscriber bootstrap.

## Throttling Behavior
If the entire `values` dict is unchanged within the last `throttle_seconds`, metrics + state publish are skipped (`throttled_skipped` counter). After a frame passes whole-dict throttling, per-metric thresholds (if configured) may still suppress individual metric publishes if change < threshold (`metric_suppressed` counter). Set `throttle_seconds=0` to disable whole-dict throttling.

## Home Assistant
Enable discovery (default true). Sensors appear automatically after first successful parse of each metric. Availability for each entity references both the bridge (`<base>/bridge/state`) and per-device availability topic.

## victron_ble API Compatibility
`victron_bridge.py` defines `parse_frame`:
- Uses legacy `parse_advertisement` when present.
- Falls back to `detect_device_type` + parser instance for newer versions.
No config changes required when upgrading `victron-ble` (as long as API pattern stays consistent).

## Unknown Device Onboarding Workflow
1. Temporarily set `publish_unknown_devices: true` in `bridge` section.
2. Monitor `<base>/unknown/#` to capture MAC + raw hex.
3. Add device under `victron.devices` with its `adv_key`.
4. Restart bridge and optionally disable unknown publishing again.

### Lightweight Passive Scan Alternative
You can also run the included utility (no MQTT required):
```
python discover_devices.py --seconds 45
```
Outputs lines like:
```
[12:34:56] MAC=AA:BB:CC:DD:EE:FF RSSI=-62dBm RAW=01020304...
```
Use the MAC plus the Victron app–retrieved advertisement key to populate `config.yaml`.

## Bridge Stats JSON
Published at `<base>/bridge/stats` (retained):
```
{
  "uptime_s": 1234,
  "adverts_seen": 4567,
  "messages_published": 890,
  "throttled_skipped": 12,
  "metric_suppressed": 34,
  "known_devices": 1,
  "unknown_devices": 0,
  "load_actions": 3,
  "load_state": 1
}
```

If Prometheus exporter enabled (`prometheus_port > 0`), scrape `http://<host>:<port>/metrics` for equivalent counters.
`load_state` gauge values: 1 = ON, 0 = OFF, -1 = UNKNOWN.

## Load Control (Optional)
When `control.enabled: true` and a valid `vedirect_port` are set, the bridge:
- Starts a VE.Direct reader thread parsing key/value frames.
- Exposes MQTT topics:
  - Command: `<base>/<control_device_name>/load/command` (payload `ON` or `OFF`)
  - State:   `<base>/<control_device_name>/load/state` (payload `ON|OFF|UNKNOWN`)
- Publishes a Home Assistant switch discovery config (`switch`).
- Tracks counters in stats & Prometheus: `load_actions`, `load_state` (gauge: 1/0/-1).

You can choose method:
1. `vedirect` (default): Uses the VE.Direct frame reader and a placeholder write pattern.
2. `modbus`: Issues a Modbus RTU Function 0x06 (write single holding register) with the configured register & values. Minimal internal CRC implementation avoids adding a heavy dependency.

SmartSolar 75/15 Notes:
- Many SmartSolar models expose load output control via a holding register documented in the "Victron Energy Solar Charger Modbus Register List". Update `load_register` to the correct address from that sheet.
- If your model does not support load over Modbus (or register differs), fall back to `vedirect` and adapt `_write_load` in `vedirect_control.py` to the proper command (if any) or use Modbus after confirming the register.

Safety: Always test with a small dummy load. Confirm register semantics (some devices may use bitfields where load is a single bit, requiring read-modify-write logic—extend code if needed).

### Verifying Your Register
Use the included diagnostic helper to probe and toggle the register safely:
```
python diagnose_load_register.py --port /dev/ttyUSB0 --unit 1 --register 0x0120 --read
python diagnose_load_register.py --port /dev/ttyUSB0 --unit 1 --register 0x0120 --set on
python diagnose_load_register.py --port /dev/ttyUSB0 --unit 1 --register 0x0120 --set off
```
Replace `0x0120` and ON/OFF values with the documented ones for your firmware. If a read value doesn't match either `on_value` or `off_value`, inspect manual: you may be dealing with a bitfield; implement bit masking before writing (add read-modify-write logic in `load_control.py`).

### Sunrise / Sunset Automation
If `control.sunrise_sunset.enabled: true` and latitude/longitude are provided, the bridge uses Astral to schedule:
- Turn load ON at (sunrise + optional `sunrise_offset_min`)
- Turn load OFF at (sunset  + optional `sunset_offset_min`)
Events recalculate daily using UTC. Manual MQTT commands always override until next scheduled event.

## Built-in Web UI (Optional)
Set `bridge.web_ui_port` to a non-zero TCP port (e.g. `8080`) to enable a minimalist single-page UI served directly by the bridge (no extra deps):

Endpoints:
```
GET  /                # HTML dashboard (devices, stats, optional load control)
GET  /api/stats       # JSON stats snapshot (same fields as bridge/stats topic)
GET  /api/devices     # JSON array of devices (configured + unknown if enabled)
GET  /api/load        # JSON load control status {enabled,state,method}
POST /api/load        # Body {"state":"ON"|"OFF"} to toggle (if control.enabled)
```
Example enable:
```yaml
bridge:
  web_ui_port: 8080
```
Then open: `http://<pi-host>:8080/`

Notes:
- UI auto-refreshes every 3 seconds with fetch() calls.
- Only exposes load control when `control.enabled: true`.
- Designed for quick local diagnostics; NOT authenticated—binds 0.0.0.0. Use firewall if network-exposed.


## Migration Option: Using victron-ble2mqtt
See `MIGRATION_victron_ble2mqtt.md` for a step-by-step guide if you later choose a fuller-featured external implementation.

## Troubleshooting
| Symptom | Action |
|---------|--------|
| ImportError for `parse_advertisement` | Already handled by shim. Ensure latest code deployed. |
| No metrics in MQTT | Check device key correctness (enable DEBUG logging). |
| High CPU usage | Increase `throttle_seconds`; limit BLE scan duplicates; reduce log level. |
| HA sensors missing | Verify discovery topics exist (`mosquitto_sub -v -t 'homeassistant/#'`). |

Enable debug temporarily:
```bash
PYTHONLOGLEVEL=DEBUG python victron_bridge.py config.yaml
```
(Be sure to revert to INFO for steady-state on Pi Zero 2W.)

## Future Enhancements (Ideas)
- Derived energy counters (Wh, Ah) computed from voltage/current over time
- Persist last metrics to disk for restart continuity
- More HA device_class / icon mapping overrides via config

## License
MIT (adjust if different).

---
Lawnberry Pi: harvesting sunlight while trimming grass.
