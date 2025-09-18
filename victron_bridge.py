#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Victron BLE -> MQTT bridge for Raspberry Pi Zero 2W.
- Listens for Victron Instant Readout BLE advertisements
- Decrypts with per-device advertisement key
- Publishes metrics to MQTT topics: <base>/<device_name>/<metric>
"""

from __future__ import annotations
import asyncio
import json
import logging
import signal
import sys
from typing import Dict, Any, Optional

import yaml
from bleak import BleakScanner, AdvertisementData
from paho.mqtt.client import Client as MQTTClient
from victron_ble import parse_advertisement  # from keshavdv/victron-ble

LOGGER = logging.getLogger("victron_bridge")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

VICRON_MFG_ID = 0x02E1  # Victron manufacturer ID in BLE advertisements


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

    # ---------- MQTT ----------
    def _mqtt_connect(self) -> None:
        mqtt_cfg = self.cfg["mqtt"]
        client = MQTTClient(client_id="victron_ble_bridge", clean_session=True)
        if mqtt_cfg.get("username"):
            client.username_pw_set(mqtt_cfg["username"], mqtt_cfg.get("password", ""))
        client.will_set(f"{self.base}/bridge/state", "offline", retain=True)
        client.connect(mqtt_cfg["host"], mqtt_cfg.get("port", 1883), keepalive=60)
        client.loop_start()
        client.publish(f"{self.base}/bridge/state", "online", retain=True)
        self.mqtt = client
        LOGGER.info("Connected to MQTT at %s:%s", mqtt_cfg["host"], mqtt_cfg.get("port", 1883))

    def _mqtt_pub(self, topic: str, payload: Any, retain: bool = True) -> None:
        if not self.mqtt:
            return
        data = payload if isinstance(payload, (str, bytes)) else json.dumps(payload, separators=(",", ":"))
        self.mqtt.publish(topic, data, retain=retain)

    # ---------- BLE ----------
    async def start(self) -> None:
        self._mqtt_connect()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._stop.set)

        async with BleakScanner(self._on_advert, detection_duplicates=True) as scanner:
            LOGGER.info("Scanning for Victron BLE advertisements...")
            await self._stop.wait()
            LOGGER.info("Shutting down scanner...")
        if self.mqtt:
            self._mqtt_pub(f"{self.base}/bridge/state", "offline", retain=True)
            self.mqtt.loop_stop()
            self.mqtt.disconnect()

    def _on_advert(self, device, adv: AdvertisementData) -> None:
        mac = device.address.upper()
        # Filter for Victron manufacturer advertisements
        mfg = adv.manufacturer_data or {}
        raw = mfg.get(VICRON_MFG_ID)
        if not raw:
            return

        # Only process known devices (you can relax this if you want auto-discovery)
        dev_cfg = self.device_map.get(mac)
        if not dev_cfg:
            return

        try:
            # Parse & decrypt using victron-ble (Instant Readout frames)
            parsed = parse_advertisement(raw, adv_key=bytes.fromhex(dev_cfg["key"]))
            # Example structure includes device type, values (volt, current, power, temp, etc.)
            name = dev_cfg["name"]
            topic_prefix = f"{self.base}/{name}"

            # Standard metrics if present
            values = parsed.get("values", {})
            for k, v in values.items():
                self._mqtt_pub(f"{topic_prefix}/{k}", v)

            # Publish a compact JSON as well
            self._mqtt_pub(f"{topic_prefix}/state", {
                "mac": mac,
                "rssi": adv.rssi,
                "type": parsed.get("device_type"),
                "values": values,
            })

        except Exception as exc:
            LOGGER.debug("Parse fail for %s: %s", mac, exc)


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
