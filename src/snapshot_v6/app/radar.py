#!/usr/bin/env python3
"""HLK-LD2450 radar module (技术方案.md 7.1) + live CLI test (section 11 step 3).

Frame as actually received on this bench (30 bytes, verified from raw bytes):
    AA FF 03 00 | target1 (8) | target2 (8) | target3 (8) | 55 CC
There is no separate length field - "03 00" is part of the 4-byte header.
Each target, little-endian 16-bit words:
    X (mm), Y (mm), speed (cm/s): SIGNED-MAGNITUDE, bit15 = 1 -> positive,
                                  bit15 = 0 -> negative, magnitude = bits 0..14
    distance resolution (mm): uint16
    all 8 bytes zero -> empty slot
Manual example: 0E 03 B1 86 10 00 40 01 -> X=-782 mm, Y=+1713 mm, v=-16 cm/s, res=320 mm.

Usage on the Pi:
    python3 radar.py                       # serial /dev/ttyUSB0 (needs the port free)
    python3 radar.py --source bench        # read-only: decode raw frames the bench service publishes
    python3 radar.py --duration 10 --raw   # stop after 10 s, also print raw frame hex
    python3 radar.py --selftest
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime

import config as C
import pointing

HEADER = b"\xAA\xFF\x03\x00"
TAIL = b"\x55\xCC"
FRAME_LEN = 30
TARGET_LEN = 8


def sm16(lo: int, hi: int) -> int:
    """LD2450 signed-magnitude: MSB set = positive, MSB clear = negative."""
    v = lo | (hi << 8)
    mag = v & 0x7FFF
    return mag if v & 0x8000 else -mag


@dataclass
class Target:
    index: int          # slot 1..3 (module slots are NOT stable track IDs)
    x_mm: int           # right positive (sign confirmed in step 3 check; see RADAR_X_SIGN)
    y_mm: int           # forward positive
    speed_cms: int      # radial speed cm/s (sign = approaching/receding, confirm on bench)
    res_mm: int         # distance resolution
    valid: bool
    raw: bytes

    @property
    def dist_mm(self) -> float:
        return math.hypot(self.x_mm, self.y_mm)

    @property
    def azimuth_deg(self) -> float:
        return pointing.azimuth_deg(self.x_mm, self.y_mm)


@dataclass
class Frame:
    t_wall: float
    t_mono: float
    targets: list          # list[Target], only occupied slots
    raw: bytes


def parse_target(raw: bytes, index: int) -> Target | None:
    if not any(raw):
        return None                         # empty slot
    x = sm16(raw[0], raw[1])
    y = sm16(raw[2], raw[3])
    v = sm16(raw[4], raw[5])
    res = raw[6] | (raw[7] << 8)
    r = math.hypot(x, y)
    az = math.degrees(math.atan2(x, y)) if (x or y) else 0.0
    valid = (x or y) and y > 0 and r <= C.RADAR_MAX_RANGE_MM and abs(az) <= C.RADAR_MAX_AZ_DEG
    return Target(index, x, y, v, res, bool(valid), bytes(raw))


def parse_frame(frame: bytes, t_wall: float = None, t_mono: float = None) -> Frame:
    if len(frame) != FRAME_LEN or frame[:4] != HEADER or frame[-2:] != TAIL:
        raise ValueError("not an LD2450 target frame")
    targets = []
    for i in range(3):
        t = parse_target(frame[4 + i * TARGET_LEN: 4 + (i + 1) * TARGET_LEN], i + 1)
        if t is not None:
            targets.append(t)
    return Frame(t_wall or time.time(), t_mono or time.monotonic(), targets, bytes(frame))


class FrameParser:
    """Streaming state machine: slide to header, wait for 30 bytes, check tail, else resync.
    Robust to USB packets splitting a frame anywhere."""

    def __init__(self):
        self.buf = bytearray()
        self.frames = 0
        self.dropped = 0

    def feed(self, data: bytes) -> list:
        self.buf += data
        out = []
        while True:
            start = self.buf.find(HEADER)
            if start < 0:
                del self.buf[:-3]           # keep a possible partial header
                break
            if start:
                self.dropped += start
                del self.buf[:start]
            if len(self.buf) < FRAME_LEN:
                break
            chunk = bytes(self.buf[:FRAME_LEN])
            if chunk[-2:] != TAIL:
                self.dropped += 1
                del self.buf[:1]            # false header, resync
                continue
            del self.buf[:FRAME_LEN]
            self.frames += 1
            out.append(parse_frame(chunk))
        if len(self.buf) > 4096:
            del self.buf[:-FRAME_LEN]
        return out


class SerialRadar(threading.Thread):
    """Radar receive thread (section 6): blocking read, keeps the latest frame snapshot."""

    def __init__(self, port=C.RADAR_PORT, baud=C.RADAR_BAUD, on_frame=None):
        super().__init__(daemon=True)
        self.port, self.baud = port, baud
        self.on_frame = on_frame        # called in this thread for EVERY decoded frame
        self.is_open = False
        self.lock = threading.Lock()
        self.latest: Frame | None = None
        self.error = ""
        self.running = True
        self.parser = FrameParser()

    def open(self):
        import serial  # pyserial
        # exclusive=True: fail loudly instead of silently sharing bytes with another reader
        ser = serial.Serial(self.port, self.baud, bytesize=8, parity="N", stopbits=1,
                            timeout=0.2, exclusive=True)
        ser.reset_input_buffer()
        return ser

    def run(self):
        while self.running:
            try:
                ser = self.open()
                self.error = ""
                self.is_open = True
                while self.running:
                    # read what is waiting (min 1 byte) so a frame is committed as soon as its
                    # tail arrives; read(N) with a timeout would add up to 200 ms latency
                    for f in self.parser.feed(ser.read(max(1, ser.in_waiting))):
                        with self.lock:
                            self.latest = f
                        if self.on_frame:
                            try:
                                self.on_frame(f)
                            except Exception as exc:  # never kill the reader
                                self.error = f"on_frame: {exc!r}"
                ser.close()
            except Exception as exc:        # unplugged / busy -> retry
                self.error = str(exc)
                self.is_open = False
                time.sleep(1.0)
        self.is_open = False

    def snapshot(self) -> Frame | None:
        with self.lock:
            return self.latest

    def stop(self):
        self.running = False


class BenchRadar(threading.Thread):
    """Read-only fallback while etec306-bench.service owns /dev/ttyUSB0: polls the bench
    page's GET /api/status, takes its raw 30-byte frame hex and decodes it with THIS module
    (the bench's own decoded x/y are ignored). No writes to the service.
    The bench reads the port in 0.3 s chunks and keeps only the last frame, so this mode
    sees ~3.3 snapshots/s out of the ~12.7 Hz radar stream; the serial mode sees every frame."""

    def __init__(self, url="http://127.0.0.1:8080/api/status", period=0.05):
        super().__init__(daemon=True)
        self.url, self.period = url, period
        self.lock = threading.Lock()
        self.latest: Frame | None = None
        self.error = ""
        self.running = True
        self._last_count = None
        self.frames = 0

    def run(self):
        while self.running:
            try:
                with urllib.request.urlopen(self.url, timeout=1.0) as resp:
                    d = json.load(resp)["radar"]
                self.error = d.get("error", "")
                if d.get("frames") != self._last_count and d.get("raw"):
                    self._last_count = d.get("frames")
                    age = (d.get("age_ms") or 0) / 1000.0
                    f = parse_frame(bytes.fromhex(d["raw"]), time.time() - age, time.monotonic() - age)
                    self.frames += 1
                    with self.lock:
                        self.latest = f
            except Exception as exc:
                self.error = str(exc)
                time.sleep(0.5)
            time.sleep(self.period)

    def snapshot(self) -> Frame | None:
        with self.lock:
            return self.latest

    def stop(self):
        self.running = False


def fmt_line(t: Target, ts: float) -> str:
    cmd = pointing.command_for_target(t.x_mm, t.y_mm)
    stamp = datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]
    flags = ("" if t.valid else " INVALID") + (" PAN-LIM" if cmd.pan_limited else "")
    return (f"{stamp}  T{t.index}  X={t.x_mm:+6d}mm  Y={t.y_mm:+6d}mm  v={t.speed_cms:+4d}cm/s  "
            f"r={cmd.range_mm:6.0f}mm  az={cmd.azimuth_deg:+6.1f}deg  "
            f"pan={cmd.pan_deg:+6.1f}deg->CH{C.PAN_CH}={cmd.pan_us}us  "
            f"tilt={cmd.tilt_deg:+5.1f}deg->CH{C.TILT_CH}={cmd.tilt_us}us  res={t.res_mm}mm{flags}")


def cli(argv=None) -> int:
    ap = argparse.ArgumentParser(description="LD2450 live target print (no servo output)")
    ap.add_argument("--source", choices=["serial", "bench"], default="serial")
    ap.add_argument("--port", default=C.RADAR_PORT)
    ap.add_argument("--rate", type=float, default=5.0, help="print ticks per second")
    ap.add_argument("--duration", type=float, default=0, help="seconds, 0 = until Ctrl+C")
    ap.add_argument("--raw", action="store_true", help="also print raw frame hex")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()

    src = SerialRadar(a.port) if a.source == "serial" else BenchRadar()
    src.start()
    print(f"# LD2450 test, source={a.source}, X sign={C.RADAR_X_SIGN:+d}, "
          f"h_s={C.SENSOR_HEIGHT_M} m h_t={C.TARGET_HEIGHT_M} m (pulses are NOT sent)", flush=True)
    t_end = time.monotonic() + a.duration if a.duration else None
    last_frame = None
    shown_err = ""
    try:
        while t_end is None or time.monotonic() < t_end:
            time.sleep(1.0 / a.rate)
            f = src.snapshot()
            if src.error and src.error != shown_err:
                shown_err = src.error
                print(f"# source error: {src.error}", flush=True)
                if a.source == "serial" and ("busy" in src.error.lower() or "lock" in src.error.lower()):
                    print("# /dev/ttyUSB0 is held (etec306-bench.service?). Use --source bench.", flush=True)
            if f is None:
                continue
            age = time.monotonic() - f.t_mono
            if age > 1.0:
                print(f"{datetime.now():%H:%M:%S}  -- no radar frame for {age:.1f}s", flush=True)
                continue
            if f is last_frame:
                continue                    # print each frame once, at most --rate per second
            last_frame = f
            if a.raw:
                print("   raw:", f.raw.hex(" "))
            if not f.targets:
                print(f"{datetime.fromtimestamp(f.t_wall):%H:%M:%S.%f}"[:-3] + "  -- no target", flush=True)
            for t in f.targets:
                print(fmt_line(t, f.t_wall), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        src.stop()
    n = getattr(src, "frames", None)
    if n is None:
        n = src.parser.frames
    print(f"# frames decoded: {n}", flush=True)
    return 0


def selftest() -> int:
    ex = bytes.fromhex("AAFF0300 0E03B186 10004001 00000000 00000000 00000000 00000000 55CC".replace(" ", ""))
    f = parse_frame(ex)
    t = f.targets[0]
    assert (t.x_mm, t.y_mm, t.speed_cms, t.res_mm) == (-782, 1713, -16, 320), t
    assert len(f.targets) == 1
    # split delivery + garbage + false header
    p = FrameParser()
    got = []
    data = b"\x01\x02\xAA\xFF\x03\x00\x99" + ex + ex
    for i in range(0, len(data), 7):
        got += p.feed(data[i:i + 7])
    assert len(got) == 2 and got[1].targets[0].y_mm == 1713
    pointing._selftest()
    print("radar selftest OK:", t)
    return 0


if __name__ == "__main__":
    sys.exit(cli())
