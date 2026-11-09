"""System metrics + ESTIMATED power model for the report (技术方案: 功耗).

The Pi 4B has no power ADC, so whole-system power is a documented linear model (README "Power estimate"):
  P_pi    = P_IDLE + P_CPU_FULL * cpu_util            Pi 4B board incl. Wi-Fi, no USB devices
  P_cam   = P_CAM_STREAM while the USB camera streams, P_CAM_IDLE while closed (enumerated, still powered)
  P_radar = P_RADAR (HLK-LD2450 + CH340 adapter, always on)
  P_hmi   = P_OLED + P_LED * leds_on
  P_servo = P_SERVO_HOLD * 2 + P_SERVO_MOVE * motion_fraction   (separate 5 V servo supply, reported apart)
Coefficients are typical values / assumptions, not measurements; change them here.
"""
from __future__ import annotations

import csv
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

P_IDLE = 2.85        # W  Pi 4B idle, Wi-Fi on, HDMI off (RPi docs / community USB-meter tests: 2.7-3.0 W)
P_CPU_FULL = 3.5     # W  extra at 100 % on all 4 cores (stress tests reach ~6.4 W total)
P_CAM_STREAM = 0.85  # W  UVC camera streaming MJPEG (typical 150-200 mA @ 5 V)        [assumption]
P_CAM_IDLE = 0.25    # W  UVC camera enumerated but stream closed (USB suspend not used) [assumption]
P_RADAR = 0.55       # W  HLK-LD2450 (~100 mA @ 5 V, datasheet: supply >= 200 mA) + CH340 (~0.05 W)
P_OLED = 0.06        # W  SSD1306 128x32, ~30-50 % pixels lit (datasheet ~10-20 mA @ 3.3 V)
P_LED = 0.02         # W  per lit indicator LED (~6 mA @ 3.3 V incl. resistor)
P_SERVO_HOLD = 0.05  # W  per servo, idle holding (~10 mA @ 5 V)                        [assumption]
P_SERVO_MOVE = 1.0   # W  per servo while moving (~200 mA @ 5 V)                        [assumption]

LOG_DIR = Path(__file__).resolve().parent / "logs"
FIELDS = ["time", "cpu_pct", "load1", "mem_used_mb", "mem_pct", "cpu_mhz", "temp_c", "throttled", "cam_state",
          "cam_fps", "vision_hz", "radar_hz", "latency_ms", "servo_motion", "rssi_dbm", "disk_pct",
          "p_pi_w", "p_cam_w", "p_radar_w", "p_hmi_w", "p_servo_w", "p_total_w", "p_logic_w"]


def _read(path, default=""):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


class Metrics:
    def __init__(self, app):
        self.app = app
        self.prev = None
        self.cpu_pct = 0.0
        self.throttled = {"raw": None, "t": 0.0}
        self.last = {}
        self.lock = threading.Lock()
        self.history = []          # last 360 samples (1 h at 10 s) for the settings-page chart
        threading.Thread(target=self.loop, daemon=True, name="metrics").start()

    # ------------------------------------------------------------------ raw readings
    def _cpu(self):
        f = [int(x) for x in _read("/proc/stat").splitlines()[0].split()[1:]]
        idle, total = f[3] + f[4], sum(f)
        if self.prev:
            di, dt = idle - self.prev[0], total - self.prev[1]
            if dt > 0:
                self.cpu_pct = 100.0 * (1 - di / dt)
        self.prev = (idle, total)
        return self.cpu_pct

    def _throttled(self):
        now = time.monotonic()
        if now - self.throttled["t"] > 10:
            try:
                out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2).stdout
                self.throttled = {"raw": int(out.strip().split("=")[1], 16), "t": now}
            except Exception:
                self.throttled = {"raw": None, "t": now}
        v = self.throttled["raw"]
        if v is None:
            return {"raw": None}
        flags = {0: "under-voltage", 1: "freq capped", 2: "throttled", 3: "soft temp limit"}
        return {"raw": f"0x{v:x}", "now": [n for b, n in flags.items() if v & (1 << b)],
                "since_boot": [n for b, n in flags.items() if v & (1 << (b + 16))]}

    @staticmethod
    def _mem():
        m = {}
        for line in _read("/proc/meminfo").splitlines():
            k, _, v = line.partition(":")
            m[k] = int(v.split()[0]) if v.split() else 0
        total, avail = m.get("MemTotal", 1), m.get("MemAvailable", 0)
        return {"used_mb": round((total - avail) / 1024), "total_mb": round(total / 1024),
                "pct": round(100 * (total - avail) / total, 1)}

    @staticmethod
    def _wifi():
        rssi = None
        for line in _read("/proc/net/wireless").splitlines()[2:]:
            parts = line.split()
            if parts and parts[0].startswith("wlan0"):
                try:
                    rssi = int(float(parts[3].rstrip(".")))
                except (ValueError, IndexError):
                    pass
        ssid = ""
        try:
            ssid = subprocess.run(["iwgetid", "-r"], capture_output=True, text=True, timeout=1).stdout.strip()
        except Exception:
            pass
        return {"rssi_dbm": rssi, "ssid": ssid}

    # ------------------------------------------------------------------ sample
    def sample(self):
        a = self.app
        now = time.monotonic()
        cpu = self._cpu()
        load = os.getloadavg()
        mem = self._mem()
        mhz = round(int(_read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", "0")) / 1000)
        du = shutil.disk_usage("/")
        cam = a.camera
        cam_state = "sleeping" if cam.sleeping.is_set() else ("streaming" if cam.ok else "no frames")
        # servo motion fraction over the last 2 s of commands (deg/s relative to 60 deg/s = "moving")
        hist = [h for h in list(a.cmd_hist) if now - h[0] <= 2.0]
        motion = 0.0
        if len(hist) >= 2:
            d = sum(abs(hist[i][1] - hist[i - 1][1]) + abs(hist[i][2] - hist[i - 1][2]) for i in range(1, len(hist)))
            motion = min(1.0, d / max(0.1, hist[-1][0] - hist[0][0]) / 60.0)
        leds = sum(1 for v in (a.gpio.led_state.values() if a.gpio else []) if v)
        p_pi = P_IDLE + P_CPU_FULL * cpu / 100
        p_cam = P_CAM_IDLE if cam_state == "sleeping" else P_CAM_STREAM
        p_radar = P_RADAR if a.radar.is_open else 0.0
        p_hmi = (P_OLED if a.oled else 0) + P_LED * leds
        p_servo = (2 * P_SERVO_HOLD + 2 * P_SERVO_MOVE * motion) if a.pca is not None else 0.0
        lat = sorted(a.latency_ms)
        m = a.mqtt.status()
        out = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "cpu_pct": round(cpu, 1), "load": [round(x, 2) for x in load], "mem": mem, "cpu_mhz": mhz,
            "temp_c": round(a.cpu_temp(), 1), "throttled": self._throttled(),
            "uptime_s": int(float(_read("/proc/uptime", "0 0").split()[0])), "app_uptime_s": int(now - a.t0),
            "radar_hz": round(a.radar_fps(), 1), "cam_state": cam_state, "cam_fps": round(cam.fps, 1),
            "vision_hz": round(a.vision.rate, 1) if a.vision else 0.0,
            "objects_hz": round(a.objects.rate, 2) if a.objects else 0.0,
            "latency_ms": lat[len(lat) // 2] if lat else None, "servo_motion": round(motion, 2),
            "servo_writes": a.guide["writes"], "wifi": {**self._wifi(), "ip": a.ip},
            "mqtt": {"connected": m.get("connected"), "queue": len(a.mqtt.queue), "last_hb": m.get("last_heartbeat")},
            "disk": {"used_gb": round(du.used / 1e9, 1), "total_gb": round(du.total / 1e9, 1),
                     "pct": round(100 * du.used / du.total, 1)},
            "power_est": {"pi_w": round(p_pi, 2), "camera_w": round(p_cam, 2), "radar_w": round(p_radar, 2),
                          "hmi_w": round(p_hmi, 2), "servo_w": round(p_servo, 2),
                          "logic_w": round(p_pi + p_cam + p_radar + p_hmi, 2),
                          "total_w": round(p_pi + p_cam + p_radar + p_hmi + p_servo, 2), "estimated": True},
            "camera_wake": list(cam.wake_log)[-5:], "detect_wake": list(a.wake_detect_log)[-5:],
        }
        with self.lock:
            self.last = out
        return out

    def row(self, o):
        p = o["power_est"]
        return [datetime.now().strftime("%Y-%m-%d %H:%M:%S"), o["cpu_pct"], o["load"][0], o["mem"]["used_mb"],
                o["mem"]["pct"], o["cpu_mhz"], o["temp_c"], o["throttled"].get("raw"), o["cam_state"], o["cam_fps"],
                o["vision_hz"], o["radar_hz"], o["latency_ms"], o["servo_motion"], o["wifi"]["rssi_dbm"],
                o["disk"]["pct"], p["pi_w"], p["camera_w"], p["radar_w"], p["hmi_w"], p["servo_w"], p["total_w"],
                p["logic_w"]]

    def loop(self):
        last_log = 0.0
        while True:
            time.sleep(2.0)
            try:
                o = self.sample()
            except Exception as exc:
                print("[metrics]", exc, flush=True)
                continue
            iv = self.app.settings.get().get("metrics_log_s", 10)
            if iv > 0 and time.monotonic() - last_log >= iv:
                last_log = time.monotonic()
                with self.lock:
                    self.history.append({"t": o["time"], "cpu": o["cpu_pct"], "temp": o["temp_c"],
                                         "w": o["power_est"]["total_w"], "cam": o["cam_state"]})
                    self.history = self.history[-360:]
                try:
                    LOG_DIR.mkdir(exist_ok=True)
                    f = LOG_DIR / f"metrics_{datetime.now():%Y%m%d}.csv"
                    new = not f.exists()
                    with f.open("a", newline="") as fh:
                        w = csv.writer(fh)
                        if new:
                            w.writerow(FIELDS)
                        w.writerow(self.row(o))
                except OSError as exc:
                    print("[metrics] log:", exc, flush=True)
