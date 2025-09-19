#!/usr/bin/env python3
"""Unified Load Control abstraction for SmartSolar 75/15 (and similar).

Supports two methods selectable via config:
1. vedirect  - Uses `VEDirectController` (frame reader) and a write pattern you may
               need to adapt to actual device command syntax.
2. modbus    - Uses Modbus RTU over the same serial line (requires pymodbus if expanded).

NOTE: To keep dependencies minimal we implement a tiny Modbus RTU write for a single
holding register (function code 0x06). This avoids pulling in full pymodbus for one write.
If you prefer robustness, replace `_modbus_write_register` with pymodbus client code.

CRC16 implementation based on standard Modbus polynomial 0xA001.

Bitfield Support:
If the Modbus register controlling the load output is a bit within a wider
status/command word rather than a dedicated 0/1 register, you may specify
`bit_index` (0-15) in the `control.modbus` config. When present:
* The controller will first read the current value (from `state_register` if
    set, else from `load_register`).
* It will set or clear the specified bit and write the whole modified word
    back using function 0x06.
* State is derived from the bit value after a successful write (or a readback
    if `state_register` given).
If `bit_index` is absent the legacy direct write using `on_value` / `off_value`
is performed.
This keeps overhead minimal while enabling safe bit-preserving updates.
"""
from __future__ import annotations

import serial  # type: ignore
import time
from typing import Optional
from vedirect_control import VEDirectController


def _modbus_crc(frame: bytes) -> bytes:
    crc = 0xFFFF
    for b in frame:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc.to_bytes(2, "little")


class LoadController:
    def __init__(self, method: str, vedirect_port: Optional[str], modbus_cfg: dict, on_state_update):
        self.method = method
        self._vedirect_port = vedirect_port
        self._modbus_cfg = modbus_cfg
        self._on_state_update = on_state_update
        self._ved: Optional[VEDirectController] = None
        self._modbus_ser: Optional[serial.Serial] = None
        self._current_state: Optional[bool] = None
        self._state_register = modbus_cfg.get("state_register")  # optional distinct register to read
        self._bit_index = modbus_cfg.get("bit_index")  # optional bit index (0-15)
        try:
            if self._bit_index is not None:
                self._bit_index = int(self._bit_index)
                if not (0 <= self._bit_index <= 15):
                    raise ValueError("bit_index out of range")
        except Exception:
            # Invalid bit_index -> disable bitfield mode
            self._bit_index = None

    def start(self):
        if self.method == "vedirect" and self._vedirect_port:
            self._start_vedirect()
        elif self.method == "modbus" and self._vedirect_port:
            self._start_modbus()

    def stop(self):
        if self._ved:
            try: self._ved.stop()
            except Exception: pass
        if self._modbus_ser:
            try: self._modbus_ser.close()
            except Exception: pass

    def get_state(self) -> Optional[bool]:
        if self.method == "vedirect" and self._ved:
            return self._ved.get_load_state()
        return self._current_state

    # Public toggle -------------------------------------------------------
    def set_state(self, desired: bool) -> bool:
        if self.method == "vedirect":
            if not self._ved:
                return False
            ok = self._ved.turn_on() if desired else self._ved.turn_off()
            if ok:
                # optimistic
                self._current_state = desired
                self._on_state_update(self.get_state())
            return ok
        elif self.method == "modbus":
            return self._modbus_set(desired)
        return False

    # VE.Direct -----------------------------------------------------------
    def _start_vedirect(self):
        def on_frame(_frame):
            self._on_state_update(self.get_state())
        self._ved = VEDirectController(self._vedirect_port, on_frame=on_frame)
        self._ved.start()

    # Modbus minimal ------------------------------------------------------
    def _start_modbus(self):
        self._modbus_ser = serial.Serial(self._vedirect_port, 19200, timeout=1)

    def _modbus_set(self, desired: bool) -> bool:
        if not self._modbus_ser:
            return False
        unit_id = int(self._modbus_cfg.get("unit_id", 1)) & 0xFF
        reg = int(self._modbus_cfg.get("load_register", 0x0120)) & 0xFFFF
        # Bitfield strategy ------------------------------------------------
        if self._bit_index is not None:
            # Read current word
            base_reg_for_read = int(self._state_register if self._state_register is not None else reg) & 0xFFFF
            current_word = self._modbus_read_register(base_reg_for_read)
            if current_word is None:
                return False
            mask = 1 << self._bit_index
            if desired:
                new_word = current_word | mask
            else:
                new_word = current_word & ~mask
            if new_word == current_word:
                # No change needed
                self._current_state = bool(current_word & mask)
                self._on_state_update(self.get_state())
                return True
            pdu = bytes([unit_id, 0x06, reg >> 8, reg & 0xFF, (new_word >> 8) & 0xFF, new_word & 0xFF])
            frame = pdu + _modbus_crc(pdu)
            try:
                self._modbus_ser.write(frame)
                self._modbus_ser.flush()
                resp = self._modbus_ser.read(8)
                if len(resp) == 8 and resp[:6] == pdu:
                    # Determine state from resulting word (optimistic if read register differs)
                    if base_reg_for_read == reg:
                        self._current_state = bool(new_word & mask)
                    else:
                        self._current_state = self._modbus_read_state()
                    self._on_state_update(self.get_state())
                    return True
            except Exception:
                return False
            return False
        # Direct value strategy --------------------------------------------
        on_value = int(self._modbus_cfg.get("on_value", 1)) & 0xFFFF
        off_value = int(self._modbus_cfg.get("off_value", 0)) & 0xFFFF
        value = on_value if desired else off_value
        pdu = bytes([unit_id, 0x06, reg >> 8, reg & 0xFF, value >> 8, value & 0xFF])
        frame = pdu + _modbus_crc(pdu)
        try:
            self._modbus_ser.write(frame)
            self._modbus_ser.flush()
            resp = self._modbus_ser.read(8)
            if len(resp) == 8 and resp[:6] == pdu:
                if self._state_register is not None:
                    self._current_state = self._modbus_read_state()
                else:
                    self._current_state = desired
                self._on_state_update(self.get_state())
                return True
        except Exception:
            return False
        return False

    def _modbus_read_state(self) -> Optional[bool]:
        if not self._modbus_ser or self._state_register is None:
            return self._current_state
        try:
            unit_id = int(self._modbus_cfg.get("unit_id", 1)) & 0xFF
            reg = int(self._state_register) & 0xFFFF
            # Function 0x03 read 1 register
            pdu = bytes([unit_id, 0x03, reg >> 8, reg & 0xFF, 0x00, 0x01])
            frame = pdu + _modbus_crc(pdu)
            self._modbus_ser.write(frame)
            self._modbus_ser.flush()
            resp = self._modbus_ser.read(7)  # unit, fc, bytecount, hi, lo, crc_lo, crc_hi
            if len(resp) == 7 and resp[1] == 0x03 and resp[2] == 0x02:
                val = (resp[3] << 8) | resp[4]
                if self._bit_index is not None:
                    return bool(val & (1 << self._bit_index))
                else:
                    on_value = int(self._modbus_cfg.get("on_value", 1)) & 0xFFFF
                    off_value = int(self._modbus_cfg.get("off_value", 0)) & 0xFFFF
                    if val == on_value:
                        return True
                    if val == off_value:
                        return False
        except Exception:
            return self._current_state
        return self._current_state

    # Low-level helper to read a single register returning raw 16-bit value
    def _modbus_read_register(self, reg: int) -> Optional[int]:
        if not self._modbus_ser:
            return None
        try:
            unit_id = int(self._modbus_cfg.get("unit_id", 1)) & 0xFF
            pdu = bytes([unit_id, 0x03, (reg >> 8) & 0xFF, reg & 0xFF, 0x00, 0x01])
            frame = pdu + _modbus_crc(pdu)
            self._modbus_ser.write(frame)
            self._modbus_ser.flush()
            resp = self._modbus_ser.read(7)
            if len(resp) == 7 and resp[1] == 0x03 and resp[2] == 0x02:
                return (resp[3] << 8) | resp[4]
        except Exception:
            return None
        return None

__all__ = ["LoadController"]
