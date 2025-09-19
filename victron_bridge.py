#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Victron BLE -> MQTT bridge (Pi Zero 2W optimized).

Features:
* victron_ble API compatibility shim (old/new parser paths)
* Home Assistant discovery (sensor metadata enriched)
* Whole-dict throttling + optional per-metric delta thresholds
* Optional derived metrics (basic power in Watts)
* Unknown device passive discovery (optional)
* Per-device availability & bridge stats publishing
* Optional Prometheus metrics exporter
* Optional MQTT TLS

Limitations: BLE Instant Readout is read-only (no control / load toggling).
"""

from __future__ import annotations
import asyncio
import json
import logging
import signal
import sys
import time
from typing import Dict, Any, Optional, Callable

import yaml
from bleak import BleakScanner, AdvertisementData
from paho.mqtt.client import Client as MQTTClient
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from functools import partial
from vedirect_control import VEDirectController  # backward compatibility (legacy)
from load_control import LoadController
from datetime import datetime, timedelta
try:
    from astral import LocationInfo
    from astral.sun import sun
except ImportError:  # astral optional; only needed if sunrise_sunset enabled
    LocationInfo = None  # type: ignore
    def sun(*a, **k):  # type: ignore
        return {}

try:
    from victron_ble import parse_advertisement as _parse_advertisement  # type: ignore
except ImportError:
    _parse_advertisement = None  # type: ignore

if _parse_advertisement is None:
    try:
        from victron_ble.devices import detect_device_type  # type: ignore
    except Exception:
        detect_device_type = None  # type: ignore

    def parse_frame(raw: bytes, adv_key: bytes):  # type: ignore
        if detect_device_type is None:
            raise RuntimeError("victron_ble API not available")
        parser_cls = detect_device_type(raw)
        parser = parser_cls(adv_key)
        values = parser.parse(raw)
        return {"device_type": getattr(parser_cls, "__name__", "unknown"), "values": values}
else:
    def parse_frame(raw: bytes, adv_key: bytes):  # type: ignore
        return _parse_advertisement(raw, adv_key=adv_key)  # type: ignore

LOGGER = logging.getLogger("victron_bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

VICRON_MFG_ID = 0x02E1


class VictronBridge:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self.mqtt: Optional[MQTTClient] = None
        self.device_map: Dict[str, Dict[str, str]] = {
            mac.upper(): {"name": v["name"], "key": v["adv_key"]}
            for mac, v in cfg["victron"]["devices"].items()
        }
        self.base = cfg["mqtt"]["base_topic"].rstrip("/")
        self._stop = asyncio.Event()
        bridge_cfg = cfg.get("bridge", {})
        self._throttle_seconds = float(bridge_cfg.get("throttle_seconds", 5.0))
        self._ha_enabled = bool(bridge_cfg.get("home_assistant_discovery", True))
        self._publish_unknown = bool(bridge_cfg.get("publish_unknown_devices", False))
        self._unknown_topic = bridge_cfg.get("unknown_topic", f"{self.base}/unknown")
        self._device_timeout = float(bridge_cfg.get("device_timeout_seconds", 120.0))
        self._stats_interval = float(bridge_cfg.get("stats_interval_seconds", 60.0))
        # Config extras
        self._per_metric_thresholds = {
            str(k): float(v) for k, v in bridge_cfg.get("per_metric_thresholds", {}).items()
        }
        self._derive_basic = bool(bridge_cfg.get("enable_basic_derived_metrics", False))
        self._prom_port = int(bridge_cfg.get("prometheus_port", 0))
        self._web_ui_port = int(bridge_cfg.get("web_ui_port", 0))  # 0 disables built-in web UI

        # State
        self._last_values: Dict[str, Dict[str, Any]] = {}
        self._last_metric_values: Dict[str, Dict[str, Any]] = {}
        self._last_publish_ts: Dict[str, float] = {}
        self._ha_announced: Dict[str, set] = {}
        self._unknown_devices: Dict[str, Dict[str, Any]] = {}
        self._device_last_seen: Dict[str, float] = {}
        self._device_available: Dict[str, bool] = {}
        self._device_last_rssi: Dict[str, int] = {}
        # Stats
        self._start_time = time.time()
        self._messages_published = 0
        self._throttled_skipped = 0
        self._metric_suppressed = 0
        self._adverts_seen = 0
        self._maint_task: Optional[asyncio.Task] = None
        self._prom_server: Optional[HTTPServer] = None
        self._prom_thread: Optional[threading.Thread] = None
        self._web_server: Optional[HTTPServer] = None
        self._web_thread: Optional[threading.Thread] = None
        # Control (VE.Direct load)
        control_cfg = cfg.get("control", {})
        self._control_enabled = bool(control_cfg.get("enabled", False))
        self._control_device_name = control_cfg.get("control_device_name", "controller")
        self._vedirect_port = control_cfg.get("vedirect_port")
        self._load_controller: Optional[LoadController] = None
        self._load_state: Optional[bool] = None
        self._load_actions = 0
        self._sun_cfg = control_cfg.get("sunrise_sunset", {})
        self._sun_task: Optional[asyncio.Task] = None
        self._next_sun_events: Dict[str, float] = {}

    # MQTT
    def _mqtt_connect(self):
        cfg = self.cfg["mqtt"]
        client = MQTTClient(client_id="victron_ble_bridge", clean_session=True)
        if cfg.get("username"):
            client.username_pw_set(cfg["username"], cfg.get("password", ""))
        client.will_set(f"{self.base}/bridge/state", "offline", retain=True)
        # TLS (optional)
        tls_cfg = cfg.get("tls") or {}
        if tls_cfg.get("enabled"):
            try:
                client.tls_set(
                    ca_certs=tls_cfg.get("ca_cert"),
                    certfile=tls_cfg.get("certfile"),
                    keyfile=tls_cfg.get("keyfile"),
                )
                LOGGER.info("MQTT TLS enabled")
            except Exception as exc:  # pragma: no cover
                LOGGER.error("Failed to configure MQTT TLS: %s", exc)
        client.connect(cfg["host"], cfg.get("port", 1883), keepalive=60)
        client.loop_start()
        client.publish(f"{self.base}/bridge/state", "online", retain=True)
        self.mqtt = client
        LOGGER.info("Connected to MQTT at %s:%s", cfg["host"], cfg.get("port", 1883))
        # Subscribe to control command topic if enabled
        if self._control_enabled:
            cmd_topic = f"{self.base}/{self._control_device_name}/load/command"
            def on_msg(client, userdata, msg):  # noqa: ANN001
                try:
                    payload = msg.payload.decode().strip().upper()
                    if payload in ("ON", "OFF"):
                        asyncio.get_event_loop().call_soon_threadsafe(
                            lambda: asyncio.create_task(self._apply_load_command(payload == "ON"))
                        )
                except Exception:
                    pass
            client.on_message = on_msg
            client.subscribe(cmd_topic)
            LOGGER.info("Subscribed to load command topic %s", cmd_topic)

    def _mqtt_pub(self, topic: str, payload: Any, retain: bool = True):
        if not self.mqtt:
            return
        data = payload if isinstance(payload, (str, bytes)) else json.dumps(payload, separators=(",", ":"))
        self.mqtt.publish(topic, data, retain=retain)
        self._messages_published += 1

    # HA discovery
    def _ha_discovery_publish(self, name: str, values: Dict[str, Any]):
        if not self._ha_enabled:
            return
        announced = self._ha_announced.setdefault(name, set())
        for metric, val in values.items():
            if metric in announced:
                continue
            ml = metric.lower()
            unit = device_class = state_class = None
            state_class = "measurement"
            if ml in ("voltage",) or ml.endswith("_v"):
                unit = "V"; device_class = "voltage"
            elif ml in ("current", "amps", "current_a") or ml.endswith("_a"):
                unit = "A"; device_class = "current"
            elif ml in ("power", "watts") or ml.endswith("_w"):
                unit = "W"; device_class = "power"
            elif ml in ("temperature", "temp"):
                unit = "°C"; device_class = "temperature"
            elif ml == "soc":
                unit = "%"; device_class = "battery"
            elif ml.endswith("_alarm"):
                device_class = "problem"; state_class = None
            uniq = f"{name}_{metric}".lower()
            payload = {
                "name": f"{name} {metric}",
                "state_topic": f"{self.base}/{name}/{metric}",
                "unique_id": uniq,
                "availability": [
                    {"topic": f"{self.base}/bridge/state"},
                    {"topic": f"{self.base}/{name}/availability"}
                ],
                "device": {
                    "identifiers": [f"victron_{name}"],
                    "manufacturer": "Victron",
                    "name": name,
                    "model": "Victron Smart Device",
                },
            }
            if unit:
                payload["unit_of_measurement"] = unit
            if device_class:
                payload["device_class"] = device_class
            if state_class:
                payload["state_class"] = state_class
            self._mqtt_pub(f"homeassistant/sensor/{uniq}/config", payload)
            announced.add(metric)

    async def _maintenance_loop(self):
        try:
            while not self._stop.is_set():
                await asyncio.sleep(self._stats_interval)
                now = time.time()
                for mac, last in list(self._device_last_seen.items()):
                    dev = self.device_map.get(mac)
                    if not dev:
                        continue
                    name = dev["name"]
                    alive = (now - last) <= self._device_timeout
                    if self._device_available.get(mac) and not alive:
                        self._mqtt_pub(f"{self.base}/{name}/availability", "offline")
                        self._device_available[mac] = False
                stats = {
                    "uptime_s": int(now - self._start_time),
                    "adverts_seen": self._adverts_seen,
                    "messages_published": self._messages_published,
                    "throttled_skipped": self._throttled_skipped,
                    "metric_suppressed": self._metric_suppressed,
                    "known_devices": len(self.device_map),
                    "unknown_devices": len(self._unknown_devices),
                    "load_actions": self._load_actions,
                    "load_state": None if self._load_state is None else (1 if self._load_state else 0),
                }
                self._mqtt_pub(f"{self.base}/bridge/stats", stats)
        except asyncio.CancelledError:
            pass

    # Prometheus exporter -------------------------------------------------
    def _prometheus_metrics_text(self) -> str:
        now = time.time()
        lines = [
            "# HELP victron_bridge_uptime_seconds Bridge uptime in seconds",
            "# TYPE victron_bridge_uptime_seconds gauge",
            f"victron_bridge_uptime_seconds {{}} {int(now - self._start_time)}",
            "# HELP victron_bridge_adverts_seen Total BLE adverts observed",
            "# TYPE victron_bridge_adverts_seen counter",
            f"victron_bridge_adverts_seen {{}} {self._adverts_seen}",
            "# HELP victron_bridge_messages_published MQTT messages published",
            "# TYPE victron_bridge_messages_published counter",
            f"victron_bridge_messages_published {{}} {self._messages_published}",
            "# HELP victron_bridge_throttled_skipped Frames skipped by whole-dict throttle",
            "# TYPE victron_bridge_throttled_skipped counter",
            f"victron_bridge_throttled_skipped {{}} {self._throttled_skipped}",
            "# HELP victron_bridge_metric_suppressed Metrics suppressed by delta thresholds",
            "# TYPE victron_bridge_metric_suppressed counter",
            f"victron_bridge_metric_suppressed {{}} {self._metric_suppressed}",
            "# HELP victron_bridge_known_devices Configured devices",
            "# TYPE victron_bridge_known_devices gauge",
            f"victron_bridge_known_devices {{}} {len(self.device_map)}",
            "# HELP victron_bridge_unknown_devices Observed unknown devices",
            "# TYPE victron_bridge_unknown_devices gauge",
            f"victron_bridge_unknown_devices {{}} {len(self._unknown_devices)}",
            "# HELP victron_bridge_load_actions Load control actions executed",
            "# TYPE victron_bridge_load_actions counter",
            f"victron_bridge_load_actions {{}} {self._load_actions}",
            "# HELP victron_bridge_load_state Current load state (1=on,0=off,-1=unknown)",
            "# TYPE victron_bridge_load_state gauge",
            f"victron_bridge_load_state {{}} { -1 if self._load_state is None else (1 if self._load_state else 0)}",
        ]
        return "\n".join(lines) + "\n"

    def _start_prometheus(self):
        if self._prom_port <= 0:
            return

        bridge_ref = self

        class Handler(BaseHTTPRequestHandler):  # type: ignore
            def do_GET(self):  # noqa: N802
                if self.path != "/metrics":
                    self.send_response(404); self.end_headers(); return
                data = bridge_ref._prometheus_metrics_text().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, format, *args):  # noqa: A003 - silence
                return

        try:
            server = HTTPServer(("0.0.0.0", self._prom_port), Handler)
        except Exception as exc:  # pragma: no cover
            LOGGER.error("Failed to start Prometheus server: %s", exc)
            return
        self._prom_server = server
        thread = threading.Thread(target=server.serve_forever, name="prometheus-http", daemon=True)
        thread.start()
        self._prom_thread = thread
        LOGGER.info("Prometheus metrics exporter listening on :%s/metrics", self._prom_port)

    # ------------------- Built-in Web UI (optional) --------------------
    def _current_stats(self) -> Dict[str, Any]:
        """Snapshot of current stats (mirrors MQTT bridge/stats)."""
        now = time.time()
        return {
            "uptime_s": int(now - self._start_time),
            "adverts_seen": self._adverts_seen,
            "messages_published": self._messages_published,
            "throttled_skipped": self._throttled_skipped,
            "metric_suppressed": self._metric_suppressed,
            "known_devices": len(self.device_map),
            "unknown_devices": len(self._unknown_devices),
            "load_actions": self._load_actions,
            "load_state": None if self._load_state is None else (1 if self._load_state else 0),
        }

    def _json(self, handler: BaseHTTPRequestHandler, code: int, obj: Any):  # type: ignore
        data = json.dumps(obj, separators=(",", ":")).encode()
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers(); handler.wfile.write(data)

    def _start_web_ui(self):
        if self._web_ui_port <= 0:
            return
        bridge_ref = self
        INDEX_HTML = ("""
<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\"/>
<title>Victron Bridge UI</title>
<style>
body{font-family:system-ui,Arial,sans-serif;margin:1rem;background:#f5f7fa;color:#222}
header{display:flex;align-items:center;gap:.75rem;margin-bottom:1rem}
code{background:#eef;padding:2px 4px;border-radius:3px}
table{border-collapse:collapse;width:100%;margin-top:1rem}
th,td{border:1px solid #ccc;padding:4px 6px;font-size:.85rem;text-align:left}
th{background:#e6edf3}
button{cursor:pointer;padding:.4rem .9rem;font-size:.9rem;border:1px solid #357;border-radius:4px;background:#4689f1;color:#fff}
button.off{background:#666}
.pill{display:inline-block;padding:2px 6px;border-radius:12px;font-size:.7rem;color:#fff}
.on{background:#2e7d32}.off{background:#aa2e25}.unknown{background:#777}
</style></head><body>
<header><h2>Victron Bridge</h2><span id=uptime></span></header>
<section id=loadBox style=\"display:none\">
  <h3>Load Control</h3>
  <div>State: <span id=loadState class=\"pill unknown\">unknown</span>
  <button id=btnOn>Turn ON</button>
  <button id=btnOff class=off>Turn OFF</button>
  </div>
</section>
<section>
  <h3>Devices</h3>
  <table id=devTbl><thead><tr><th>Name</th><th>MAC</th><th>Avail</th><th>RSSI</th><th>Last Seen (s ago)</th><th>Values</th></tr></thead><tbody></tbody></table>
</section>
<section>
  <h3>Stats</h3>
  <pre id=statsBox>{}</pre>
</section>
<script>
async function j(u,opt){const r=await fetch(u,opt);if(!r.ok) throw new Error(r.status);return r.json();}
function formatAgo(ts){if(!ts) return '-'; const ago=Math.floor(Date.now()/1000 - ts); return ago+'s';}
async function refresh(){
  try{
    const [stats, devices, load] = await Promise.all([
      j('api/stats'), j('api/devices'), j('api/load')
    ]);
    document.getElementById('statsBox').textContent = JSON.stringify(stats,null,2);
    document.getElementById('uptime').textContent = 'Uptime '+stats.uptime_s+'s';
    const tb=document.querySelector('#devTbl tbody'); tb.innerHTML='';
    devices.forEach(d=>{const tr=document.createElement('tr');
      tr.innerHTML=`<td>${d.name}</td><td>${d.mac||''}</td><td>${d.available}</td><td>${d.rssi??''}</td><td>${formatAgo(d.last_seen)}</td><td><code>${JSON.stringify(d.values||{})}</code></td>`;tb.appendChild(tr);});
    const lb=document.getElementById('loadBox');
    if(load.enabled){ lb.style.display='block'; const stSp=document.getElementById('loadState');
      stSp.textContent=load.state; stSp.className='pill '+(load.state==='ON'?'on':(load.state==='OFF'?'off':'unknown')); }
  }catch(e){console.error(e);}
}
async function sendLoad(state){await j('api/load',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({state})}); setTimeout(refresh,400);}
document.getElementById('btnOn').onclick=()=>sendLoad('ON');
document.getElementById('btnOff').onclick=()=>sendLoad('OFF');
refresh(); setInterval(refresh,3000);
</script></body></html>
""")

        class Handler(BaseHTTPRequestHandler):  # type: ignore
            def do_GET(self):  # noqa: N802
                path = self.path.split('?')[0]
                if path in ('/', '/index.html'):
                    data = INDEX_HTML.encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers(); self.wfile.write(data); return
                if path == '/api/stats':
                    bridge_ref._json(self, 200, bridge_ref._current_stats()); return
                if path == '/api/devices':
                    devices = []
                    for mac, info in bridge_ref.device_map.items():
                        name = info['name']
                        devices.append({
                            'name': name,
                            'mac': mac,
                            'available': bool(bridge_ref._device_available.get(mac)),
                            'last_seen': int(bridge_ref._device_last_seen.get(mac, 0)) or None,
                            'rssi': bridge_ref._device_last_rssi.get(mac),
                            'values': bridge_ref._last_values.get(name, {})
                        })
                    for mac, u in bridge_ref._unknown_devices.items():
                        devices.append({
                            'name': '(unknown)', 'mac': mac, 'available': True,
                            'last_seen': int(u.get('last_seen', 0)) or None,
                            'rssi': None, 'values': {}
                        })
                    bridge_ref._json(self, 200, devices); return
                if path == '/api/load':
                    state = 'UNKNOWN'
                    if bridge_ref._load_state is not None:
                        state = 'ON' if bridge_ref._load_state else 'OFF'
                    bridge_ref._json(self, 200, {
                        'enabled': bridge_ref._control_enabled,
                        'state': state,
                        'method': bridge_ref.cfg.get('control', {}).get('method', 'vedirect')
                    }); return
                self.send_response(404); self.end_headers()
            def do_POST(self):  # noqa: N802
                if self.path == '/api/load':
                    if not bridge_ref._control_enabled:
                        bridge_ref._json(self, 400, {'error': 'control disabled'}); return
                    length = int(self.headers.get('Content-Length', '0') or 0)
                    body = self.rfile.read(length) if length else b''
                    try:
                        data = json.loads(body.decode() or '{}')
                        desired = data.get('state','').upper()
                        if desired not in ('ON','OFF'):
                            raise ValueError('state must be ON/OFF')
                        asyncio.get_event_loop().call_soon_threadsafe(
                            lambda: asyncio.create_task(bridge_ref._apply_load_command(desired=='ON'))
                        )
                        bridge_ref._json(self, 200, {'ok': True})
                    except Exception as exc:  # pragma: no cover
                        bridge_ref._json(self, 400, {'error': str(exc)})
                    return
                self.send_response(404); self.end_headers()
            def log_message(self, format, *args):  # noqa: A003 - silence
                return

        try:
            server = HTTPServer(('0.0.0.0', self._web_ui_port), Handler)
        except Exception as exc:  # pragma: no cover
            LOGGER.error('Failed to start web UI server: %s', exc); return
        self._web_server = server
        thread = threading.Thread(target=server.serve_forever, name='web-ui', daemon=True)
        thread.start(); self._web_thread = thread
        LOGGER.info('Web UI listening on :%s', self._web_ui_port)

    async def start(self):
        self._mqtt_connect()
        self._start_prometheus()
        self._start_web_ui()
        if self._control_enabled and self._vedirect_port:
            self._start_load_controller()
            self._publish_load_state()
            self._ha_publish_switch()
            if self._sun_cfg.get("enabled"):
                self._sun_task = asyncio.create_task(self._sun_scheduler())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._stop.set)
        self._maint_task = asyncio.create_task(self._maintenance_loop())
        async with BleakScanner(self._on_advert, detection_duplicates=True):
            LOGGER.info("Scanning for Victron BLE advertisements...")
            await self._stop.wait()
            LOGGER.info("Shutting down scanner...")
        if self.mqtt:
            self._mqtt_pub(f"{self.base}/bridge/state", "offline", retain=True)
            self.mqtt.loop_stop(); self.mqtt.disconnect()
        if self._maint_task:
            self._maint_task.cancel()
        if self._prom_server:
            self._prom_server.shutdown()
        if self._web_server:
            self._web_server.shutdown()
        if self._sun_task:
            self._sun_task.cancel()
        if self._load_controller:
            self._load_controller.stop()

    def _on_advert(self, device, adv: AdvertisementData):
        mac = device.address.upper()
        mfg = adv.manufacturer_data or {}
        raw = mfg.get(VICRON_MFG_ID)
        if not raw:
            return
        self._adverts_seen += 1
        dev_cfg = self.device_map.get(mac)
        if not dev_cfg:
            if self._publish_unknown:
                now = time.time()
                info = self._unknown_devices.setdefault(mac, {"first_seen": now, "count": 0})
                info["count"] += 1; info["last_seen"] = now
                self._mqtt_pub(f"{self._unknown_topic}/{mac}/state", {
                    "mac": mac, "rssi": adv.rssi, "last_seen": int(now), "count": info["count"]
                })
                self._mqtt_pub(f"{self._unknown_topic}/{mac}/raw", raw.hex())
            return
        try:
            parsed = parse_frame(raw, adv_key=bytes.fromhex(dev_cfg["key"]))
            name = dev_cfg["name"]
            topic_prefix = f"{self.base}/{name}"
            values: Dict[str, Any] = parsed.get("values", {})
            # Derived metrics (basic)
            if self._derive_basic and "power_w" not in values:
                # Attempt to find voltage & current keys
                v_key = "voltage" if "voltage" in values else next((k for k in values if k.endswith("_v")), None)  # type: ignore[arg-type]
                c_key = "current" if "current" in values else next((k for k in values if k.endswith("_a")), None)  # type: ignore[arg-type]
                try:
                    if v_key and c_key:
                        v_val = float(values[v_key]); c_val = float(values[c_key])
                        values["power_w"] = round(v_val * c_val, 2)
                except Exception:  # ignore bad casts
                    pass
            self._device_last_seen[mac] = time.time()
            self._device_last_rssi[mac] = adv.rssi
            if not self._device_available.get(mac):
                self._device_available[mac] = True
                self._mqtt_pub(f"{topic_prefix}/availability", "online")
            if self._throttle_seconds > 0:
                now = time.time(); prev = self._last_values.get(name)
                if prev == values and (now - self._last_publish_ts.get(name, 0)) < self._throttle_seconds:
                    self._throttled_skipped += 1; return
                self._last_values[name] = values; self._last_publish_ts[name] = now
            if name not in self._ha_announced:
                self._ha_discovery_publish(name, values)
            else:
                new_vals = {k: v for k, v in values.items() if k not in self._ha_announced[name]}
                if new_vals:
                    self._ha_discovery_publish(name, new_vals)
            # Per-metric thresholds (suppress minor deltas)
            metric_last = self._last_metric_values.setdefault(name, {})
            for k, v in values.items():
                publish_metric = True
                thr = self._per_metric_thresholds.get(k)
                if thr is not None and k in metric_last:
                    try:
                        delta = abs(float(v) - float(metric_last[k]))
                        if delta < thr:
                            publish_metric = False
                            self._metric_suppressed += 1
                    except Exception:
                        pass
                if publish_metric:
                    self._mqtt_pub(f"{topic_prefix}/{k}", v)
                    metric_last[k] = v
            self._mqtt_pub(f"{topic_prefix}/state", {
                "mac": mac, "rssi": adv.rssi, "type": parsed.get("device_type"), "values": values,
            })
        except Exception as exc:
            LOGGER.debug("Parse fail for %s: %s", mac, exc)

    # ---------------- Control / Load Output -----------------
    def _start_load_controller(self):
        def on_state_update(_state):
            new_state = self._load_controller.get_state() if self._load_controller else None
            if new_state is not None and new_state != self._load_state:
                self._load_state = new_state
                self._publish_load_state()
        try:
            method = self.cfg.get("control", {}).get("method", "vedirect")
            modbus_cfg = self.cfg.get("control", {}).get("modbus", {})
            self._load_controller = LoadController(method, self._vedirect_port, modbus_cfg, on_state_update)
            self._load_controller.start()
            LOGGER.info("Load controller started (%s) on %s", method, self._vedirect_port)
        except Exception as exc:
            LOGGER.error("Failed to start load controller: %s", exc)

    def _publish_load_state(self):
        if not self.mqtt or not self._control_enabled:
            return
        state_topic = f"{self.base}/{self._control_device_name}/load/state"
        val = "UNKNOWN" if self._load_state is None else ("ON" if self._load_state else "OFF")
        self._mqtt_pub(state_topic, val)

    async def _apply_load_command(self, desired: bool):
        if not self._load_controller:
            LOGGER.warning("Load command ignored; controller not initialized")
            return
        if self._load_state is not None and self._load_state == desired:
            return  # no-op
        ok = self._load_controller.set_state(desired)
        if ok:
            self._load_actions += 1
            # optimistic update (will be corrected by next frame if needed)
            self._load_state = desired
            self._publish_load_state()
        else:
            LOGGER.error("Failed to send load %s command", "ON" if desired else "OFF")

    def _ha_publish_switch(self):
        if not self._ha_enabled or not self.mqtt:
            return
        uniq = f"{self._control_device_name}_load_switch"
        payload = {
            "name": f"{self._control_device_name} load",
            "unique_id": uniq,
            "command_topic": f"{self.base}/{self._control_device_name}/load/command",
            "state_topic": f"{self.base}/{self._control_device_name}/load/state",
            "payload_on": "ON",
            "payload_off": "OFF",
            "availability": [
                {"topic": f"{self.base}/bridge/state"},
            ],
            "device": {
                "identifiers": [f"victron_load_{self._control_device_name}"],
                "manufacturer": "Victron",
                "name": self._control_device_name,
                "model": "VE.Direct Controlled Load",
            },
        }
        self._mqtt_pub(f"homeassistant/switch/{uniq}/config", payload)

    async def _sun_scheduler(self):
        if LocationInfo is None:
            LOGGER.error("Astral not installed; sunrise/sunset disabled")
            return
        try:
            lat = float(self._sun_cfg.get("latitude"))
            lon = float(self._sun_cfg.get("longitude"))
        except Exception:
            LOGGER.error("Invalid latitude/longitude for sunrise/sunset")
            return
        on_at_sunrise = bool(self._sun_cfg.get("on_at_sunrise", True))
        off_at_sunset = bool(self._sun_cfg.get("off_at_sunset", True))
        rise_off = int(self._sun_cfg.get("sunrise_offset_min", 0))
        set_off = int(self._sun_cfg.get("sunset_offset_min", 0))
        location = LocationInfo(latitude=lat, longitude=lon)
        while not self._stop.is_set():
            now = datetime.utcnow()
            s = sun(location.observer, date=now.date())
            # Astral returns aware datetimes (UTC if observer has no tz)
            sunrise = (s.get("sunrise") or now) + timedelta(minutes=rise_off)
            sunset = (s.get("sunset") or now) + timedelta(minutes=set_off)
            # Determine next events
            events: list[tuple[datetime, Callable[[], None]]] = []
            if on_at_sunrise and sunrise > now:
                events.append((sunrise, lambda: asyncio.create_task(self._apply_load_command(True))))
            if off_at_sunset and sunset > now:
                events.append((sunset, lambda: asyncio.create_task(self._apply_load_command(False))))
            if not events:
                # All for today passed; sleep until a bit after midnight UTC
                tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
                await asyncio.sleep((tomorrow - now).total_seconds())
                continue
            events.sort(key=lambda x: x[0])
            next_time, action = events[0]
            wait_s = max(1, (next_time - now).total_seconds())
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait_s)
                break
            except asyncio.TimeoutError:
                action()  # fire
                # loop recalculates for potential second event
        LOGGER.info("Sun scheduler exiting")


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def main() -> int:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} /path/to/config.yaml")
        return 2
    cfg = load_config(sys.argv[1])
    bridge = VictronBridge(cfg)
    asyncio.run(bridge.start())
    return 0


if __name__ == "__main__":
    sys.exit(main())
