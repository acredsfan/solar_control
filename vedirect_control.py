#!/usr/bin/env python3
"""VE.Direct load control helper.

This module provides a minimal, low-impact interface to a Victron device's
load output via the VE.Direct serial port. Actual writable command syntax can
vary by device/firmware; this implementation includes a placeholder write
format you can adjust once you confirm the proper command sequence from the
official Victron VE.Direct protocol or Modbus register list.

Strategy:
* A background reader thread continuously parses VE.Direct key/value frames.
* Most frames contain repeated telemetry lines ended by a Checksum line.
* We extract keys relevant to load state (heuristically: 'LOAD', 'Relay', or
  model dependent). The last seen raw frame dict is stored.
* Public methods: `turn_on()`, `turn_off()`, which attempt to send a command.
* A simple callback can be registered to receive updated parsed frames.

DISCLAIMER: Write command format MUST be verified against Victron docs for
your device. The placeholder ':LOAD=1' style may not be accepted. Some models
require Modbus register writes instead. Adjust `_write_load` accordingly.
"""
from __future__ import annotations

import serial  # type: ignore
import threading
import time
from typing import Callable, Optional, Dict, Any


class VEDirectController:
    def __init__(self, port: str, baudrate: int = 19200, on_frame: Optional[Callable[[Dict[str, str]], None]] = None):
        self.port = port
        self.baudrate = baudrate
        self.on_frame = on_frame
        self._ser: Optional[serial.Serial] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_frame: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._last_load_state: Optional[bool] = None

    def start(self):
        if self._thread:
            return
        self._ser = serial.Serial(self.port, self.baudrate, timeout=1)
        self._stop.clear()
        self._thread = threading.Thread(target=self._reader_loop, name="vedirect-reader", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
        self._thread = None
        self._ser = None

    # Public API -----------------------------------------------------------
    def turn_on(self) -> bool:
        return self._write_load(True)

    def turn_off(self) -> bool:
        return self._write_load(False)

    def get_last_frame(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._last_frame)

    def get_load_state(self) -> Optional[bool]:
        return self._last_load_state

    # Internal -------------------------------------------------------------
    def _reader_loop(self):
        buf: Dict[str, str] = {}
        assert self._ser is not None
        while not self._stop.is_set():
            try:
                line_b = self._ser.readline()
                if not line_b:
                    # timeout
                    continue
                line = line_b.decode(errors="ignore").strip()
                if not line:
                    continue
                if '\t' in line:
                    k, v = line.split('\t', 1)
                    if k == 'Checksum':
                        # Frame end
                        self._process_frame(buf)
                        buf = {}
                    else:
                        buf[k] = v
            except Exception:
                time.sleep(0.5)
        # flush last partial
        if buf:
            self._process_frame(buf)

    def _process_frame(self, frame: Dict[str, str]):
        load_state = None
        # Heuristic: Some devices output 'LOAD' or 'Relay'
        for candidate in ("LOAD", "Load", "Relay"):
            if candidate in frame:
                val = frame[candidate].strip().lower()
                if val in ("on", "1", "yes"): load_state = True
                elif val in ("off", "0", "no"): load_state = False
                break
        with self._lock:
            self._last_frame = frame
            if load_state is not None:
                self._last_load_state = load_state
        if self.on_frame:
            try:
                self.on_frame(frame)
            except Exception:
                pass

    def _write_load(self, state: bool) -> bool:
        """Attempt to write load state. Adjust command format if needed.

        Placeholder pattern used here: ':LOAD=1' or ':LOAD=0' with trailing CR.
        Many Victron devices DO NOT accept this raw text command; update to the
        correct documented write procedure (e.g., Modbus register write) for
        your model.
        """
        if not self._ser:
            return False
        try:
            cmd = f":LOAD={'1' if state else '0'}\r".encode()
            self._ser.write(cmd)
            self._ser.flush()
            return True
        except Exception:
            return False


__all__ = ["VEDirectController"]
