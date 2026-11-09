"""Pointing module: radar X/Y -> pan/tilt angles -> PCA9685 pulse widths.

技术方案.md 7.2 / 4.3. Pure functions; the I2C driver lives in hw.py.
Conventions: pan right = +, tilt up = +, degrees.
    pulse_us = clamp(1500 - angle_deg * 1000/90 + trim_us, 1000, 2000)
    right 18 deg -> 1300 us, left 18 deg -> 1700 us; up 18 -> 1300, down 18 -> 1700
Effective range: pan +-45 deg, tilt +-25 deg (software limits).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import config as C


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def azimuth_deg(x_mm: float, y_mm: float, x_sign: int = None) -> float:
    """theta = atan2(sign*X, Y). 0 = straight ahead, right positive."""
    sx = C.RADAR_X_SIGN if x_sign is None else x_sign
    return math.degrees(math.atan2(sx * x_mm, y_mm))


def range_mm(x_mm: float, y_mm: float) -> float:
    return math.hypot(x_mm, y_mm)


def tilt_geometry_deg(r_mm: float, h_sensor_m: float = None, h_target_m: float = None) -> float:
    """phi = atan2(h_t - h_s, r). LD2450 has no elevation, so tilt comes from geometry."""
    hs = C.SENSOR_HEIGHT_M if h_sensor_m is None else h_sensor_m
    ht = C.TARGET_HEIGHT_M if h_target_m is None else h_target_m
    return math.degrees(math.atan2(ht - hs, max(r_mm / 1000.0, 0.05)))


def angle_to_pulse_us(angle_deg: float, offset_us: float = 0.0, sign: int = 1,
                      lo: float = C.SERVO_MIN_US, hi: float = C.SERVO_MAX_US) -> int:
    """sign=-1 flips the servo direction without changing the angle convention.
    lo/hi = channel clamp (tilt 1000..2000; pan uses PAN_MIN_US..PAN_MAX_US)."""
    us = C.SERVO_CENTER_US - sign * angle_deg * C.US_PER_DEG + offset_us
    return int(round(clamp(us, lo, hi)))


def channel_limits_us(ch: int):
    return (C.PAN_MIN_US, C.PAN_MAX_US) if ch == C.PAN_CH else (C.TILT_MIN_US, C.TILT_MAX_US)


def pixel_to_deg(d_px: float, size_px: float, fov_deg: float) -> float:
    """Pinhole model: pixel offset from the image centre -> angle (deg). f = (size/2)/tan(fov/2)."""
    f = (size_px / 2.0) / math.tan(math.radians(fov_deg) / 2.0)
    return math.degrees(math.atan2(d_px, f))


def pulse_to_ticks(us: float) -> int:
    """12-bit count at 50 Hz (1 LSB ~ 4.88 us)."""
    return int(round(us * 4096 / C.PWM_PERIOD_US))


def ticks_to_us(ticks: int) -> float:
    return ticks * C.PWM_PERIOD_US / 4096


def limit_pan(a: float) -> float:
    return clamp(a, -C.PAN_LIMIT_DEG, C.PAN_LIMIT_DEG)


def limit_tilt(a: float) -> float:
    return clamp(a, -C.TILT_LIMIT_DEG, C.TILT_LIMIT_DEG)


@dataclass
class PointCommand:
    azimuth_deg: float
    range_mm: float
    pan_deg: float
    tilt_deg: float
    pan_us: int
    tilt_us: int
    pan_limited: bool
    tilt_limited: bool


def command_for_target(x_mm: float, y_mm: float, s: dict = None) -> PointCommand:
    """s = settings dict (x_sign, sensor_height_m, target_height_m, theta0_deg, phi0_deg,
    pan_offset_us, tilt_offset_us); falls back to config.py defaults."""
    s = s or {}
    az = azimuth_deg(x_mm, y_mm, s.get("x_sign"))
    r = range_mm(x_mm, y_mm)
    pan_raw = s.get("az_scale", 1.0) * az + s.get("theta0_deg", C.THETA0_DEG)
    tilt_raw = tilt_geometry_deg(r, s.get("sensor_height_m"), s.get("target_height_m")) + s.get("phi0_deg", C.PHI0_DEG)
    pan, tilt = limit_pan(pan_raw), limit_tilt(tilt_raw)
    return PointCommand(az, r, pan, tilt,
                        angle_to_pulse_us(pan, s.get("pan_offset_us", C.PAN_OFFSET_US), s.get("pan_sign", 1),
                                          C.PAN_MIN_US, C.PAN_MAX_US),
                        angle_to_pulse_us(tilt, s.get("tilt_offset_us", C.TILT_OFFSET_US), s.get("tilt_sign", 1)),
                        pan != pan_raw, tilt != tilt_raw)


def _selftest() -> None:
    assert angle_to_pulse_us(0) == 1500
    assert angle_to_pulse_us(18) == 1300 and angle_to_pulse_us(-18) == 1700
    assert angle_to_pulse_us(90) == 1000 and angle_to_pulse_us(-200) == 2000
    assert abs(azimuth_deg(1000, 1000, 1) - 45) < 1e-9 and abs(azimuth_deg(1000, 1000, -1) + 45) < 1e-9
    c = command_for_target(30000, 1000, {"x_sign": 1})
    lim = C.PAN_LIMIT_DEG
    assert c.pan_limited and c.pan_deg == lim and c.pan_us == round(1500 - lim * 1000 / 90)
    assert angle_to_pulse_us(80, 0, 1, C.PAN_MIN_US, C.PAN_MAX_US) == 611
    assert angle_to_pulse_us(-85, 0, 1, C.PAN_MIN_US, C.PAN_MAX_US) == 2389
    c = command_for_target(0, 2000, {"sensor_height_m": 1.0, "target_height_m": 1.2})
    assert abs(c.tilt_deg - math.degrees(math.atan2(0.2, 2.0))) < 1e-9 and c.tilt_us < 1500
    print("pointing selftest OK")


if __name__ == "__main__":
    _selftest()
