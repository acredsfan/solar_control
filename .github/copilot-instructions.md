# Copilot Instructions for `solar_control`

Concise project knowledge so AI agents can be productive quickly. Keep output changes aligned with the real code & conventions below.

## 1. Purpose & High-Level Flow
Bridge Victron Energy BLE "Instant Readout" advertisements to MQTT, explicitly optimized for a Raspberry Pi Zero 2W (limited CPU/RAM, BlueZ stack). Keep per-advert processing minimal, avoid blocking I/O inside callbacks. Runtime pipeline:
BLE Advertisements -> Filter by manufacturer ID (0x02E1) -> Match known MAC -> Decrypt via `victron_ble.parse_advertisement` using per-device adv key -> Extract `values` -> Publish individual metric topics + compact JSON state.

## 2. Key Files
- `victron_bridge.py` Main async bridge. Defines `VictronBridge` with BLE scan + MQTT publish.
- `config.yaml` (git-ignored) Deployment config: MQTT connection + device map (MAC → {name, adv_key}). Example provided in repo root; sensitive keys excluded via `.gitignore`.
- `requirements.txt` Pinned runtime deps (bleak, paho-mqtt, PyYAML, victron-ble).
- `victron-ble-bridge.service` Example systemd unit for Raspberry Pi (Zero 2W) deployment.
- `.gitignore` Ignores `config.yaml` and mypy cache.

## 3. Configuration Contract (`config.yaml`)
Structure (example with extended `bridge` + optional TLS):
```
mqtt:
  host: 127.0.0.1
  port: 1883
  username: ""
  password: ""
  base_topic: victron
  # tls:
  #   enabled: true
  #   ca_cert: /etc/ssl/certs/ca.crt
  #   certfile: /etc/ssl/certs/client.crt
  #   keyfile: /etc/ssl/private/client.key

bridge:
  throttle_seconds: 5              # Suppress unchanged publishes within window (0 = disable)
  home_assistant_discovery: true   # Publish HA discovery configs
  publish_unknown_devices: false   # If true, publish raw adverts for un-configured Victron MACs
  device_timeout_seconds: 120      # Mark device offline if not seen in this period
  stats_interval_seconds: 60       # Interval for bridge stats JSON
  # unknown_topic: victron/unknown # Optional override (default <base>/unknown)
  per_metric_thresholds:           # Optional per-metric delta thresholds (publish only if change >= threshold)
    voltage: 0.02
    current: 0.05
  enable_basic_derived_metrics: true  # Derive simple metrics (currently power_w)
  prometheus_port: 0                  # >0 enables /metrics exporter

victron:
  devices:
    "AA:BB:CC:DD:EE:FF":
      name: battery_shunt
      adv_key: 0123456789ABCDEF0123456789ABCDEF
control:
  enabled: false
  vedirect_port: /dev/ttyUSB0
  control_device_name: controller
  method: vedirect  # vedirect | modbus
  modbus:
    unit_id: 1
    load_register: 0x0120
    on_value: 1
    off_value: 0
    # bit_index: 5          # OPTIONAL: if the load control is a single bit inside the register (0-15)
    # state_register: 0x0120 # OPTIONAL: separate register to read state if different from load_register
  sunrise_sunset:
    enabled: false
    latitude: 0.0
    longitude: 0.0
    on_at_sunrise: true
    off_at_sunset: true
    sunrise_offset_min: 0
    sunset_offset_min: 0
```
Rules:
- MAC keys MUST be uppercase colon-separated or will be uppercased internally.
- `adv_key` is hex, converted with `bytes.fromhex` – invalid length raises exception during first matching advertisement.
- `base_topic` is normalized by stripping any trailing `/`.
- Only devices listed are decrypted / parsed. If `publish_unknown_devices` is true, unknown Victron adverts are still published under `<base>/unknown/<MAC>/...` (raw + minimal state) but not decrypted.
- Whole-dict throttling compares entire `values` dict; identical consecutive frames inside window skipped (`throttled_skipped`).
- Per-metric thresholds applied after frame passes whole-dict throttling; suppressed metrics increment `metric_suppressed` counter.
- Derived metrics (currently `power_w`) calculated if voltage & current present and metric absent.
- Optional load control via VE.Direct (if `control.enabled`); adds command/state topics + HA switch.
 - Optional load control via VE.Direct OR Modbus. Modbus supports either direct register writes
   (`on_value`/`off_value`) or bitfield mode via `bit_index` (read-modify-write). If `state_register`
   is provided it is used for authoritative readback; otherwise the written value (or modified word)
   determines state.

## 4. MQTT Topic Schema
Per known configured device (`name`):
- `<base>/<name>/<metric>` Individual metrics from parsed `values`.
- `<base>/<name>/state` JSON: `{mac, rssi, type, values}` (compact separators, retained).
- `<base>/<name>/availability` = `online` / `offline` (retained) derived from last seen time vs `device_timeout_seconds`.

Bridge topics:
- `<base>/bridge/state` Lifecycle (`online`/`offline`, retained, also HA availability).
- `<base>/bridge/stats` JSON stats (retained): `uptime_s, adverts_seen, messages_published, throttled_skipped, metric_suppressed, known_devices, unknown_devices, load_actions, load_state`.

Unknown device (when `publish_unknown_devices: true`):
- `<base>/unknown/<MAC>/state` JSON: `{mac, rssi, last_seen, count}`.
- `<base>/unknown/<MAC>/raw` Hex string of manufacturer frame (undecrypted).

Home Assistant Discovery (if enabled):
- `homeassistant/sensor/<device>_<metric>/config` retained JSON config (includes availability list referencing bridge & per-device availability topics).
- `homeassistant/switch/<control_device_name>_load_switch/config` retained JSON for load control (if enabled).

Retention: All publishes use `retain=True` for fast consumer bootstrap. Optional Prometheus exporter (if `prometheus_port > 0`) exposes equivalent counters at `/metrics`.

## 5. Runtime & Control Flow
`main()` loads config → instantiate `VictronBridge` → `asyncio.run(bridge.start())`.

Inside `start()`:
1. MQTT connect (sets LWT, publishes `bridge/state=online`).
2. Launch maintenance task (`_maintenance_loop`) for availability & stats.
3. Start `BleakScanner` with `detection_duplicates=True` calling `_on_advert` for every advert.
4. On signals (SIGINT/SIGTERM) set stop event; scanner context exits.
5. Publish `bridge/state=offline`, cancel maintenance, disconnect.

Advertisement callback `_on_advert` steps:
- Filter manufacturer ID 0x02E1.
- Increment counters: adverts_seen.
- If MAC unknown:
  - If `publish_unknown_devices` -> publish minimal state + raw frame under `<base>/unknown/...`.
  - Return (no parse attempt).
- Decrypt & parse via compatibility shim (`parse_frame`).
- Optionally derive metrics (power_w) if enabled.
- Update last seen, mark availability online if transitioning.
- Whole-dict throttling (skip unchanged entire `values`).
- HA discovery for new metrics.
- Per-metric delta threshold suppression.
- Publish individual metrics (those not suppressed) + aggregated state.
- (Separately) VE.Direct reader thread updates load state & publishes switch state if enabled.

Maintenance loop `_maintenance_loop`:
- Runs every `stats_interval_seconds`.
- Marks devices offline if stale (`now - last_seen > device_timeout_seconds`).
- Publishes bridge stats JSON.

Parse errors isolated and logged at DEBUG only to avoid noise.

## 6. External Dependencies
- `bleak` BLE scanning (system BlueZ).
- `victron-ble` Advertisement parsing; compatibility shim handles API variance.
- `paho-mqtt` MQTT client with optional TLS.
- `PyYAML` Config parsing.
- `pyserial` VE.Direct serial access (only when control enabled).
- `astral` Sunrise/sunset scheduling (only when enabled).
All versions pinned; verify CPU impact before upgrading on Pi Zero 2W.

## 7. Development Workflow
Primary deployment target: Raspberry Pi Zero 2W running Raspberry Pi OS (Bookworm or Bullseye) with system `python3` (3.11 or distro default). Keep environment lean to reduce package compile time.

Install deps:
```
python -m venv .venv
. .venv/Scripts/activate  # Windows
pip install -r requirements.txt
```
Run locally (needs BLE + real adverts in range):
```
python victron_bridge.py config.yaml
```
Systemd deployment (Linux/Pi): copy repo to `/home/pi/victron-ble-bridge`, adjust path/user in `victron-ble-bridge.service`, then:
```
sudo cp victron-ble-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now victron-ble-bridge
```

## 8. Conventions & Patterns
- Async boundary confined to `start()`; other methods sync for simplicity (reduces context switches on constrained Pi Zero 2W CPU).
- Logging namespace: `victron_bridge`; default level INFO; noisy parse errors suppressed unless DEBUG enabled (avoid sustained DEBUG on Zero 2W to reduce SD wear + CPU usage).
- Device names used directly in topic paths: keep lowercase snake or kebab (avoid spaces) for MQTT compatibility.
- All metrics published individually + aggregated JSON for flexibility.
- Graceful shutdown ensures `offline` retained state.
- Keep per-advert logic O(number_of_metrics); no disk writes, network calls beyond MQTT publishes.

## 9. Safe Extension Points
1. TLS Hardening: Add TLS version / ciphers, client cert rotation.
2. Advanced Derived Metrics: Energy accumulation (Wh / Ah) with persisted state.
3. HA Customization: Configurable device_class/unit/icon overrides via mapping.
4. Threshold Strategy: Support relative (%) thresholds or hysteresis bands.
5. Structured Logging: Replace `basicConfig` with dictConfig; add JSON logs for ingestion.
6. Prometheus: Add per-device gauges (last RSSI, last_seen_age_s).
7. Persistence: Save last metrics/throttle state across restarts for continuity.
8. Unknown Promotion Tool: Auto-generate YAML snippet from stored unknown adverts.

## 10. Pitfalls / Gotchas
- Wrong `adv_key` => parse exceptions (DEBUG only). Temporarily raise log level for diagnosis.
- Whole-dict throttling may delay visibility of small but important metric changes—tune thresholds carefully.
- Per-metric thresholds only suppress publishes (actual state JSON still shows raw values) which can cause perceived mismatch in dashboards relying on per-metric topics vs state.
- Unknown device publishing can accumulate many retained topics—disable when not actively onboarding.
- Availability depends on frequent adverts; extend `device_timeout_seconds` for slow-reporting hardware.
- TLS certificate renewal requires restart (no live reload implemented).
- Prometheus exporter adds an HTTP listener; ensure firewalling if exposed beyond localhost.
- VE.Direct write pattern is placeholder — adjust to actual command or Modbus register required by device.
- Sunrise/sunset scheduling uses UTC; offsets may be needed for local policy.

## 11. When Editing Code
Maintain:
- Publish lifecycle state messages (online/offline).
- Retained messages for metrics to aid consumers after reconnect.
- Uppercasing MAC addresses when indexing `device_map`.
- Exception isolation inside `_on_advert` (never let a single bad frame break scanning).

## 12. Minimal Checklist Before PR
- Code runs: `python victron_bridge.py config.yaml` (with a sanitized test config) starts scanning without stack traces.
- New config keys documented in section 3 & not committed with secrets.
- If topic schema changes, update section 4 and ensure backward compatibility or note breaking change.

---
Questions / unclear areas to refine: (1) Expected metric names set produced by typical Victron devices? (2) Need built-in caching/throttling? (3) Plan to support TLS or HA discovery soon?
