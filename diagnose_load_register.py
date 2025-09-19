#!/usr/bin/env python3
"""Diagnostic tool for SmartSolar load control register.

Functions:
1. Read current value of a holding register (function 0x03) containing load state.
2. Optionally write ON/OFF value (function 0x06) and read back.

Usage examples:
  python diagnose_load_register.py --port /dev/ttyUSB0 --unit 1 --register 0x0120 --on-value 1 --off-value 0 --read
  python diagnose_load_register.py --port /dev/ttyUSB0 --unit 1 --register 0x0120 --on-value 1 --off-value 0 --set on

NOTE: Replace register and values with those from the official Victron Modbus list.
"""
from __future__ import annotations
import argparse
import serial  # type: ignore


def crc16(frame: bytes) -> bytes:
    crc = 0xFFFF
    for b in frame:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc.to_bytes(2, 'little')


def read_register(ser: serial.Serial, unit: int, reg: int) -> int | None:
    pdu = bytes([unit & 0xFF, 0x03, reg >> 8, reg & 0xFF, 0x00, 0x01])
    ser.write(pdu + crc16(pdu))
    ser.flush()
    resp = ser.read(7)
    if len(resp) == 7 and resp[1] == 0x03 and resp[2] == 0x02:
        return (resp[3] << 8) | resp[4]
    return None


def write_register(ser: serial.Serial, unit: int, reg: int, value: int) -> bool:
    pdu = bytes([unit & 0xFF, 0x06, reg >> 8, reg & 0xFF, value >> 8, value & 0xFF])
    ser.write(pdu + crc16(pdu))
    ser.flush()
    resp = ser.read(8)
    return len(resp) == 8 and resp[:6] == pdu


def main(argv=None):  # noqa: D401
    ap = argparse.ArgumentParser(description="Diagnose load control Modbus register")
    ap.add_argument('--port', required=True, help='Serial device (e.g. /dev/ttyUSB0)')
    ap.add_argument('--unit', type=int, default=1)
    ap.add_argument('--register', type=lambda x: int(x, 0), required=True, help='Holding register (e.g. 0x0120)')
    ap.add_argument('--on-value', type=lambda x: int(x, 0), default=1)
    ap.add_argument('--off-value', type=lambda x: int(x, 0), default=0)
    ap.add_argument('--read', action='store_true', help='Only read register')
    ap.add_argument('--set', choices=['on', 'off'], help='Write ON or OFF then read back')
    args = ap.parse_args(argv)

    ser = serial.Serial(args.port, 19200, timeout=1)
    try:
        if args.read or not args.set:
            val = read_register(ser, args.unit, args.register)
            print(f"Current register 0x{args.register:04X} value: {val}")
        if args.set:
            target = args.on_value if args.set == 'on' else args.off_value
            if write_register(ser, args.unit, args.register, target):
                print(f"Wrote value {target} to 0x{args.register:04X}")
                new_val = read_register(ser, args.unit, args.register)
                print(f"Read-back value: {new_val}")
            else:
                print("Write failed (no echo)")
    finally:
        ser.close()


if __name__ == '__main__':  # pragma: no cover
    main()
