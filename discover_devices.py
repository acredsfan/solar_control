#!/usr/bin/env python3
"""Simple Victron BLE advertiser discoverer.

Lists Victron manufacturer advertisements (ID 0x02E1) with MAC, RSSI and raw hex.
Does NOT decrypt or publish to MQTT – use to collect MAC addresses before
adding them to `config.yaml`.

Usage:
  python discover_devices.py [--seconds 30]

Press Ctrl+C to stop early.
"""
from __future__ import annotations
import argparse
import asyncio
import signal
import sys
from datetime import datetime

from bleak import BleakScanner, AdvertisementData

VICRON_MFG_ID = 0x02E1


def _on_advert(device, adv: AdvertisementData):  # type: ignore
    raw = (adv.manufacturer_data or {}).get(VICRON_MFG_ID)
    if not raw:
        return
    ts = datetime.utcnow().strftime('%H:%M:%S')
    print(f"[{ts}] MAC={device.address.upper()} RSSI={adv.rssi:>4}dBm RAW={raw.hex()}")


async def run(duration: float):
    stop = asyncio.Event()

    def _stop(*_):  # noqa: D401
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:  # Windows fallback
            pass

    async with BleakScanner(_on_advert, detection_duplicates=True):
        if duration <= 0:
            await stop.wait()
        else:
            try:
                await asyncio.wait_for(stop.wait(), timeout=duration)
            except asyncio.TimeoutError:
                pass


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Discover Victron BLE advertisers")
    ap.add_argument("--seconds", type=float, default=30.0, help="Scan duration (<=0 for infinite)")
    args = ap.parse_args(argv)
    try:
        asyncio.run(run(args.seconds))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
