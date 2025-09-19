# Project Handover: Victron BLE -> MQTT Bridge with Optional Load Control

## 1. Executive Summary
This repository implements a lightweight, Pi Zero 2W–optimized bridge that:
- Scans Victron BLE "Instant Readout" advertisements using `bleak`.
- Decrypts frames with per-device keys via `victron_ble` (API compatibility shim included).
- Publishes individual metrics + aggregated JSON state to MQTT with retained topics.
- Provides Home Assistant (HA) MQTT Discovery for sensors and (optionally) a load switch.
- Tracks availability, publishes system & device stats, and exposes Prometheus metrics.
- Supports optional load output control via VE.Direct or Modbus RTU (single register + new bitfield mode) with sunrise/sunset automation.

The codebase focuses on minimal per-advert overhead to stay performant on constrained hardware.

## 2. Implemented Features (Status Matrix)
| Area | Feature | Status | Notes |
|------|---------|--------|-------|
| BLE | Manufacturer ID filter (0x02E1) | Complete | Avoids needless parsing logic. |
| Parsing | victron_ble parser shim (old/new API) | Complete | `parse_frame` wrapper handles both versions. |
| MQTT | Per-metric topic + aggregated JSON | Complete | All retained for fast consumer bootstrap. |
| MQTT | Base topic normalization | Complete | Trailing `/` trimmed. |
| HA Integration | Sensor auto-discovery | Complete | Includes device metadata + availability refs. |
| Throttling | Whole-dict frame suppress (time window) | Complete | Configurable via `bridge.throttle_seconds`. |
| Thresholds | Per-metric delta suppression | Complete | `per_metric_thresholds` mapping. |
| Derived Metrics | Basic `power_w` (V * A) | Complete | Only if voltage & current present. |
| Availability | Device online/offline tracking | Complete | Based on last seen time vs timeout. |
| Unknown Devices | Optional raw advert publishing | Complete | Under `<base>/unknown/...`. |
| Stats | Bridge stats JSON | Complete | Includes counters and load state/actions. |
| Prometheus | `/metrics` exporter | Complete | Simple text HTTP listener. |
| Web UI | Lightweight built-in HTML dashboard | Added | Optional `bridge.web_ui_port` serves stats/devices/load control. |
| Security | Optional TLS for MQTT | Complete | Basic CA + client cert parameters. |
| Load Control | VE.Direct reader + optimistic toggle | Complete | Placeholder write; read-derived state. |
| Load Control | Modbus single register write (0x06) | Complete | Direct value mode (`on_value`/`off_value`). |
| Load Control | Modbus readback (0x03) | Complete | Optional `state_register`. |
| Load Control | Bitfield RMW support | Complete | `bit_index` (0–15) triggers safe read-modify-write. |
| Automation | Sunrise/Sunset scheduler (Astral) | Complete | Optional offsets; executes load commands. |
| Diagnostics | Register probing script | Complete | `diagnose_load_register.py` assists discovery. |
| Config | Comprehensive schema in docs | Complete | See `copilot-instructions.md` & example snippet. |
| Docs | README & Copilot instructions | Complete | Up to date except need to add `bit_index` (see TODO). |

## 3. Key Files Overview
- `victron_bridge.py`: Core async orchestration (BLE scan, MQTT publishing, HA discovery, stats, Prometheus, load control orchestration, sunrise/sunset automation).
- `load_control.py`: Unified load control abstraction (VE.Direct / Modbus direct / Modbus bitfield).
- `vedirect_control.py`: VE.Direct frame reading & basic load toggle placeholder logic (adjust as hardware specifics become available).
- `diagnose_load_register.py`: CLI helper for Modbus register discovery/testing.
- `config.yaml` (example): Deployment configuration (real file is user-supplied & git-ignored if following pattern).
- `.github/copilot-instructions.md`: AI contributor guidelines and architecture reference.
- `requirements.txt`: Pinned dependency versions.
- `victron-ble-bridge.service`: Systemd unit template.

## 4. Configuration Highlights
```
control:
  enabled: true
  vedirect_port: /dev/ttyUSB0
  control_device_name: controller
  method: modbus              # or vedirect
  modbus:
    unit_id: 1
    load_register: 0x0120     # WRITE target
    state_register: 0x0120    # (optional) READ target
    on_value: 1               # used only when bit_index absent
    off_value: 0
    bit_index: 5              # OPTIONAL for bitfield mode (0-15)
  sunrise_sunset:
    enabled: true
    latitude: 12.34
    longitude: 56.78
    on_at_sunrise: true
    off_at_sunset: true
    sunrise_offset_min: 0
    sunset_offset_min: 0
```
Bitfield Mode Rules:
- If `bit_index` is set and valid, controller reads the register, flips the single bit, and writes full word.
- If `state_register` differs from `load_register`, state is re-read after write for accuracy.
- If `bit_index` missing, direct value semantics apply.

## 5. Pending / Recommended Next Steps
| Priority | Task | Rationale | Suggested Approach |
|----------|------|-----------|--------------------|
| High | Confirm actual SmartSolar load control register & semantics | Avoid accidental writes to wrong register | Use `diagnose_load_register.py` while toggling load via official UI; capture OFF/ON raw words. |
| High | Update docs for confirmed register values | Reduce onboarding friction | Add a concrete example once verified (without copying proprietary text). |
| High | Validate VE.Direct write command format | Placeholder may not match actual command set | Consult Victron VE.Direct protocol reference; adjust `turn_on/turn_off`. |
| Medium | Add inversion flag (`invert: true`) | Some devices use active-low bits | Extend modbus config & apply XOR in determination. |
| Medium | Add HA binary_sensor for load availability | Improve dashboard clarity | Publish discovery config referencing same state topic. |
| Medium | Auth / bind controls for Web UI | Security hardening | Optionally restrict to localhost or add simple token. |
| Medium | Per-device Prometheus gauges | Observability for RSSI, last_seen age | Add metrics inside `_prometheus_metrics_text`. |
| Low | State persistence across restarts | Prevent stale throttling or duplicated discovery churn | Save last `values` + metric cache to a small JSON; reload at startup. |
| Low | Relative (%) thresholds or hysteresis | More flexible publish suppression | Extend threshold logic with strategy field per metric. |
| Low | Structured logging (JSON) | Easier ingestion into centralized logs | Replace `basicConfig` with `logging.config.dictConfig`. |
| Low | Topic pruning tool for unknown devices | Reduce retained clutter | Provide a script that unsubscribes/prunes after onboarding. |

## 6. Risk & Mitigation
| Risk | Impact | Mitigation |
|------|--------|-----------|
| Incorrect Modbus register write | Possible unintended device state | Require manual confirmation workflow before enabling control in production. |
| Victron BLE library API shift | Parse failures | Already mitigated via dynamic shim; add version pin if stability needed. |
| Excess retained topics from unknown discovery | Broker clutter | Keep `publish_unknown_devices` disabled except during onboarding. |
| High advert rate + verbose logging | CPU/SD wear | Keep INFO level in production; use DEBUG only temporarily. |
| Network outages → retained staleness | Dashboard mismatch | Availability timeouts + periodic stats help detect; consider watchdog logic externally. |

## 7. Testing & Validation Guide
| Area | How to Test | Expected Result |
|------|-------------|----------------|
| BLE Parsing | Run bridge near a configured Victron device | Metrics & state topics appear; counters increment. |
| Throttling | Set `throttle_seconds: 10`, observe stable values | Repeated identical frames suppressed (throttled counter increases). |
| Thresholds | Set tiny threshold (e.g., voltage: 5) | Small deltas ignored; metric_suppressed increments. |
| Derived Metric | Provide voltage + current in advert | `power_w` topic appears if not present already. |
| Unknown Devices | Enable publish_unknown_devices temporarily | Raw + state topics appear under `<base>/unknown/...`. |
| Prometheus | Set `prometheus_port: 9100` then curl | Plain text metrics served. |
| Load Control (Modbus) | Use diagnostic script to verify register change | OFF/ON updates observed; bit logic consistent. |
| Sunrise/Sunset | Temporarily adjust offsets to near current time | Scheduled command fires; load state changes. |
| HA Discovery | Start HA with MQTT integration | Entities auto-populate (sensors + optional switch). |

## 8. Deployment Notes
1. Ensure user is in `bluetooth` group (Linux) and BlueZ is active.
2. Systemd unit sets restart policy—verify log journaling not too verbose.
3. Pin dependencies in `requirements.txt` for reproducibility on Pi Zero 2W.
4. For TLS: ensure CA and client cert paths accessible to service user.

## 9. Extension Hooks
- Add additional derived metrics inside `_on_advert` before publish loop.
- Introduce per-device overrides (units/device_class) by extending `_ha_discovery_publish` mapping.
- Add more Prometheus lines in `_prometheus_metrics_text` (format already present).
- Insert persistence load/save around `VictronBridge.__init__` and before shutdown.

## 10. Handover Checklist
| Item | Verified? |
|------|-----------|
| Repository builds & runs (`python victron_bridge.py config.yaml`) | Pending hardware run |
| Config example reflects all keys (except bit_index doc addition) | Yes |
| Bitfield logic implemented & commented | Yes |
| Diagnostic workflow documented | Yes |
| Prometheus endpoint functioning | Yes (logic review) |
| HA discovery operational | Yes (logic review) |
| Risk list & next steps captured | Yes |

## 11. Onboarding Another Developer
1. Read `copilot-instructions.md` for architectural grounding.
2. Review `victron_bridge.py` top-to-bottom (entrypoint patterns). 
3. Examine `load_control.py` focusing on `_modbus_set` bitfield path.
4. Run without control first (set `control.enabled: false`). Confirm metrics flow.
5. Use diagnostic script to lock down actual load control register & semantics.
6. Enable control (modbus or vedirect) only after safe validation.
7. Add any newly confirmed register specifics to docs (avoid verbatim proprietary text).
8. (Optional) Implement one medium-priority enhancement as a warm-up (e.g., HA binary_sensor).

## 12. Open Questions
| Topic | Needed To Proceed |
|-------|-------------------|
| Definitive SmartSolar load register | Hardware inspection + vendor doc | 
| VE.Direct load on/off command framing | Official VE.Direct spec | 
| Additional device metric naming variations | Sample adverts from multiple device classes |

## 13. Support Scripts
- `diagnose_load_register.py`: Safe probing & toggling (add `--help` improvement if desired).
- Potential future: `prune_unknown.py` (not implemented) for cleaning retained topics.

---
Prepared for handover. Update `copilot-instructions.md` next to include the new `bit_index` key under Modbus config.

## 14. Built-in Web UI Summary
Configuration: set `bridge.web_ui_port: 8080` (non-zero) to enable.

Endpoints (no auth, for local diagnostics):
```
GET /              # HTML page (devices, stats, load control buttons if enabled)
GET /api/stats     # JSON stats snapshot
GET /api/devices   # JSON array: {name,mac,available,last_seen,rssi,values}
GET /api/load      # JSON: {enabled,state,method}
POST /api/load     # Body {"state":"ON"|"OFF"}
```
Notes:
- Uses Python stdlib `http.server` (no extra dependency footprint).
- Auto-refresh every 3s via fetch.
- Intended for LAN-only; consider reverse proxy or firewall if exposed.
