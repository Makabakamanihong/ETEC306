#!/usr/bin/env python3
"""ETEC306 formal program: radar -> tracking -> pan/tilt -> camera -> face / object detection ->
identity -> state machine / keys / LEDs / OLED -> MQTT, web page on :8080 (HTTP) and :8443 (HTTPS).

Threads: radar receive | guidance 50 Hz (only writer of PCA9685 CH4/CH8) | camera MJPEG pass-through |
vision (YuNet, motion blobs) | objects (NanoDet + palm) | identity (SFace) | calibration jobs |
HMI 5 Hz | MQTT | HTTP + HTTPS servers.

Guidance priority: manual > centre > calibration > disarmed hold > paused > VISION (face/person/hand
seen: closed-loop on the pixel error, radar ignored) > RADAR (confirmed track that is not marked as
a static object) > PATROL (step-and-stare) > return to centre > hold.
"""
from __future__ import annotations

import json
import math
import os
import re
import signal
import ssl
import statistics
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote, parse_qs

import config as C
import hw
import metrics
import notify
import pointing
import radar
import vision
from camera import Camera, SnapshotStore
from mqtt_client import MqttLink
from settings import Settings
from tracker import Tracker

ROOT = Path(__file__).resolve().parent
STATE_CN = {"DISARMED": "撤防", "ARMED": "布防守候", "VERIFY": "核实目标", "LOCKED": "锁定(确认是人)", "KNOWN": "已知",
            "ALARM": "告警"}
MODE_CN = {"start": "启动", "manual": "手动", "center": "归中", "calib": "标定", "disarmed-hold": "撤防保持",
           "paused": "暂停", "vision": "视觉跟踪", "radar": "雷达跟踪", "search": "主动搜索", "patrol": "巡逻", "return-center": "回中",
           "hold": "保持"}
TLS_DIR = ROOT / "tls"
# enrollment poses. Image-space yaw > 0 = nose towards image RIGHT = the person turned to THEIR OWN LEFT.
POSE_CN = {"front": "正面", "right": "向自己左边转", "left": "向自己右边转"}
POSE_PROMPT = {"front": "请正对镜头 1-2 m", "right": "请把头慢慢转向你自己的左边约 20-30°（眼睛仍看得到镜头）",
               "left": "请把头慢慢转向你自己的右边约 20-30°（眼睛仍看得到镜头）"}


def pose_quota(n):
    side = n // 3
    return {"front": n - 2 * side, "right": side, "left": side}


def next_pose(got, quota):
    for k in ("front", "right", "left"):
        if got.get(k, 0) < quota[k]:
            return k
    return None


KIND_CN = {"face": "人脸", "person": "人体", "hand": "手", "head": "头部(跟踪)", "profile": "侧脸", "face_weak": "人脸(弱)", "": "-"}
EVENT_LOG_MAX = 5 * 1024 * 1024


def ip_address():
    try:
        return subprocess.check_output(["hostname", "-I"], text=True, timeout=2).split()[0]
    except Exception:
        return "?"


def reencode(jpg, quality=70, long_edge=1280):
    """8.2 / 7.6: alarm image = JPEG q~70, long edge 1280."""
    if not vision.available():
        return jpg
    cv2, np = vision.cv2, vision.np
    img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return jpg
    h, w = img.shape[:2]
    if max(h, w) > long_edge:
        k = long_edge / max(h, w)
        img = cv2.resize(img, (int(w * k), int(h * k)), interpolation=cv2.INTER_AREA)
    ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return enc.tobytes() if ok else jpg


def tiny_gray(jpg):
    """64x36 grey thumbnail for snapshot de-duplication."""
    if not vision.available():
        return None
    cv2, np = vision.cv2, vision.np
    img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_REDUCED_GRAYSCALE_8)
    return None if img is None else cv2.resize(img, (64, 36)).astype(np.float32)


class App:
    def __init__(self):
        self.t0 = time.monotonic()
        self.lock = threading.RLock()
        self.settings = Settings()
        s = self.settings.get()
        self.running = True
        self.events = deque(maxlen=300)
        self.event_log = ROOT / "events.jsonl"
        self._ev_n = 0
        self.ip = ip_address()
        self.hw_error = {}
        # state machine
        self.armed = bool(s["arm_on_boot"])
        self.paused = False
        self.state = "ARMED" if self.armed else "DISARMED"
        self.alarm_until = 0.0
        self.last_alarm_t = -1e9
        self.alarm_last = {}                       # target key -> time of last alarm (per-target cooldown)
        self.last_auto_snap_t = -1e9
        self.last_auto_small = None
        self.snapped_keys = set()
        self.pending_snaps = []
        self.center_until = 0.0
        self.last_primary_id = None
        self.last_target_t = time.monotonic()      # radar primary or vision target seen (patrol idle timer)
        self.person_since = None
        self.red_blink_until = 0.0
        self.known_blink_until = 0.0
        self.known_banner = ("", 0.0)
        self.last_known_t = -1e9
        self.score_log = deque(maxlen=50)
        self.last_activity_t = time.monotonic()  # camera power saving
        self.wake_req_t = None                    # radar/web asked the sleeping camera to wake (for latency)
        self.wake_detect_log = deque(maxlen=20)   # {"t", "frame_ms", "detect_ms", "why"}
        self.wifi_combo_since = None
        self.wifi_qr_t = 0.0
        self.qr_det = None
        self.last_known_track = None      # (name, bearing, t, score): a new vision session nearby inherits it
        self.assoc_warn = {"t": -1e9, "n": 0, "last_err": None}
        self.assoc_log = deque(maxlen=100)
        self.search = {"tid": None, "aim_t": None, "i": 0, "step_t": None, "active": False, "motion": None,
                       "base": (0.0, 0.0)}
        self.force_detect_until = 0.0
        self.oled_page = 0
        # radar / tracking
        self.tracker = Tracker()
        self.radar_times = deque(maxlen=40)
        self.frames = 0
        self.last_frame = None
        self.last_frame_t = 0.0
        self.clutter = []                          # [x, y, until, label] static non-person spots
        self.verify = {"id": None, "since": None, "vis_n0": 0, "obj_t0": 0.0}
        self.radar = radar.SerialRadar(on_frame=self.on_radar_frame)
        # guidance
        self.guide = {"mode": "start", "pan_goal": 0.0, "tilt_goal": 0.0, "pan_f": 0.0, "tilt_f": 0.0,
                      "pan_cmd": 0.0, "tilt_cmd": 0.0, "pan_us": None, "tilt_us": None,
                      "desired_pan": None, "desired_tilt": None, "writes": 0, "error": ""}
        self.cmd_hist = deque(maxlen=150)         # (t, pan_cmd, tilt_cmd): pose at frame time, at-rest check
        self.latency_ms = deque(maxlen=100)       # radar frame -> pulse written (software path, 7.7)
        self.manual = None                        # {"4": ticks, "8": ticks} in servo test mode (T2)
        # vision tracking (vision priority)
        self.vt = {"active": False, "since": 0.0, "last": 0.0, "kind": "", "goal": None, "seen_n": 0,
                   "session": 0, "track": None, "bearing": None}
        self.patrol = {"active": False, "idx": 0, "dir": 1, "arrived": None}
        # calibration
        self.calib = {"mode": "off", "pose": [0.0, 0.0], "msg": "", "samples": [], "cand": None,
                      "result": None, "busy": False}
        # vision / identity
        self.vision_res = None
        self.vis_count = 0
        self.last_detect_t = 0.0
        self.ident = {}                           # target key ("T12" / "V3") -> identity info
        self.enroll = {"running": False, "name": "", "done": 0, "target": 0, "msg": "", "log": []}
        self.enroll_sessions = {}                 # phone / laptop enrollment sessions
        self.gallery = None
        self.vision = None
        self.objects = None
        # hardware
        self.i2c_found, self.bus, self.pca, self.oled, self.gpio = [], None, None, None, None
        self.init_i2c()
        try:
            self.gpio = hw.Gpio(self.on_button)
        except Exception as exc:
            self.hw_error["gpio"] = str(exc)
        self.camera = Camera()
        self.snaps = SnapshotStore(ROOT / "snapshots")
        self.notifier = notify.Notifier(log=lambda title, text: self.event(title, text))
        if vision.available():
            try:
                self.gallery = vision.Gallery()
                self.vision = vision.VisionThread(self)
            except Exception as exc:
                self.hw_error["vision"] = f"init: {exc!r}"
            try:
                import objects
                self.objects = objects.ObjectThread(self, ROOT / C.MODEL_DIR)
            except Exception as exc:
                self.hw_error["objects"] = f"init: {exc!r}"
        else:
            self.hw_error["vision"] = "OpenCV not installed"
        self.mqtt = MqttLink(on_cmd=self.command, on_event=self.event)
        self._temp = (0.0, 0.0)
        self.event("系统启动", f"布防={self.armed} I2C={[hex(a) for a in self.i2c_found]} "
                             f"视觉={'OK' if self.vision else '无'} 物体={'OK' if self.objects else '无'} "
                             f"MQTT={'配置' if self.mqtt.enabled else '未配置'}")

    # ------------------------------------------------------------------ hardware
    def init_i2c(self):
        try:
            import smbus2
            self.i2c_found = hw.scan_i2c()
            self.bus = smbus2.SMBus(C.I2C_BUS)
        except Exception as exc:
            self.hw_error["i2c"] = str(exc)
            return
        if C.PCA9685_ADDR in self.i2c_found:
            try:
                self.pca = hw.Pca9685(self.bus)
                self.write_servos(0.0, 0.0)
            except Exception as exc:
                self.hw_error["pca9685"] = str(exc)
        else:
            self.hw_error["pca9685"] = "0x40 not on bus"
        if C.OLED_ADDR in self.i2c_found:
            try:
                self.oled = hw.Ssd1306(self.bus)
                self.oled.show([f"ETEC306 {self.ip}", "starting..."])
            except Exception as exc:
                self.hw_error["oled"] = str(exc)
        else:
            self.hw_error["oled"] = "0x3C not on bus"

    def write_servos(self, pan_deg, tilt_deg):
        if self.pca is None:
            return None, None
        s = self.settings.get()
        pu = self.pca.write_us(C.PAN_CH, pointing.angle_to_pulse_us(pan_deg, s["pan_offset_us"], s["pan_sign"],
                                                                    C.PAN_MIN_US, C.PAN_MAX_US))
        tu = self.pca.write_us(C.TILT_CH, pointing.angle_to_pulse_us(tilt_deg, s["tilt_offset_us"], s["tilt_sign"]))
        return pu, tu

    def cpu_temp(self):
        now = time.monotonic()
        if now - self._temp[1] > 2.0:
            try:
                t = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000.0
            except Exception:
                t = 0.0
            self._temp = (t, now)
        return self._temp[0]

    # ------------------------------------------------------------------ events
    def event(self, kind, text="", mqtt_type=None, extra=None, image=None):
        e = {"t": datetime.now().strftime("%H:%M:%S"), "kind": kind, "text": text}
        with self.lock:
            self.events.appendleft(e)
        print(f"[event] {kind} {text}", flush=True)
        try:
            self._ev_n += 1
            if self._ev_n % 200 == 0 and self.event_log.exists() and self.event_log.stat().st_size > EVENT_LOG_MAX:
                for i in (2, 1):                   # events.jsonl -> .1 -> .2 -> .3
                    src = self.event_log.with_suffix(f".jsonl.{i}")
                    if src.exists():
                        os.replace(src, self.event_log.with_suffix(f".jsonl.{i + 1}"))
                os.replace(self.event_log, self.event_log.with_suffix(".jsonl.1"))
            with self.event_log.open("a") as fh:
                fh.write(json.dumps({"ts": datetime.now().isoformat(timespec="milliseconds"), **e,
                                     **({"extra": extra} if extra else {})}, ensure_ascii=False) + "\n")
        except OSError:
            pass
        if mqtt_type and getattr(self, "mqtt", None) is not None:
            eid = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
            payload = {"id": eid, "ts": datetime.now().isoformat(timespec="seconds"), "type": mqtt_type,
                       "text": text, "state": self.state, "armed": self.armed, **(extra or {})}
            if image is not None:
                payload["image"] = f"{eid}.jpg"
                self.mqtt.publish(f"image/{eid}", image, qos=1, queue_if_offline=True)
            self.mqtt.publish("event", payload, qos=1, queue_if_offline=True)

    def track_summary(self, p, s):
        if p is None:
            return None
        c = pointing.command_for_target(p.x, p.y, s)
        return {"track": p.id, "x_mm": round(s["x_sign"] * p.x), "y_mm": round(p.y), "r_m": round(p.r_m, 2),
                "v_r_cms": p.speed, "theta_deg": round(c.azimuth_deg, 1)}

    def snapshot(self, reason, upload=None, extra=None, dedupe=False):
        jpg = self.camera.frame if self.camera.ok else None
        if jpg is None:
            self.event("抓拍失败", f"{reason}: 摄像头无画面 {self.camera.error}")
            if upload:
                self.event(upload[0], f"{upload[1]}（无图）", mqtt_type=upload[2], extra=extra)
                if upload[2] == "alarm":
                    self.notifier.alarm(f"ETEC306 告警 {datetime.now():%H:%M:%S}: {upload[1]}（无图）")
            return None
        if dedupe:      # automatic snapshots: skip if the picture is almost the same as the last one
            small = tiny_gray(jpg)
            if small is not None and self.last_auto_small is not None:
                diff = float(vision.np.abs(small - self.last_auto_small).mean())
                if diff < 4.0:
                    print(f"[snap] {reason} skipped (same scene, diff {diff:.1f})", flush=True)
                    return None
            self.last_auto_small = small
        name = self.snaps.save(jpg, reason)
        if upload:   # 7.6: unknown-person alarm images (and K4 if enabled) go to the cloud
            send_img = upload[2] == "alarm" or self.settings.get()["upload_manual_snap"]
            self.event(upload[0], f"{upload[1]} 图 {name}", mqtt_type=upload[2], extra=extra,
                       image=reencode(jpg) if send_img else None)
            if upload[2] == "alarm":      # Telegram / WhatsApp (notify.json, off by default)
                self.notifier.alarm(f"ETEC306 告警 {datetime.now():%H:%M:%S}: {upload[1]}", jpg)
        else:
            self.event("抓拍", name)
        return name

    # ------------------------------------------------------------------ radar
    def on_radar_frame(self, f):
        now = time.monotonic()
        s = self.settings.get()
        with self.lock:
            self.frames += 1
            self.radar_times.append(now)
            self.last_frame, self.last_frame_t = f, now
            self.tracker.update(f.targets, s, now)
            if self.tracker.tracks and self.camera.sleeping.is_set():
                self.wake_camera("radar", now)
            self.update_state(now, s)

    def primary_command(self, s):
        p = self.tracker.primary
        return None if p is None else pointing.command_for_target(p.x, p.y, s)

    def mapped_pan(self, tr, s):
        """Unclamped pan angle the current radar->gimbal mapping gives for a track."""
        az = pointing.azimuth_deg(tr.x, tr.y, s["x_sign"])
        return s["az_scale"] * az + s["theta0_deg"]

    def match_radar(self, bearing, s, max_deg=12.0):
        best, bd = None, max_deg
        for tr in self.tracker.tracks:
            if not tr.confirmed:
                continue
            d = abs(self.mapped_pan(tr, s) - bearing)
            if d < bd:
                best, bd = tr, d
        return best

    def pose_at(self, t, lag=0.08):
        """Commanded pan/tilt when a camera frame was taken (frame arrival time minus a small lag)."""
        t -= lag
        best = None
        for ts, p, tl in reversed(self.cmd_hist):
            best = (p, tl)
            if ts <= t:
                break
        return best if best else (self.guide["pan_cmd"], self.guide["tilt_cmd"])

    def update_kinds(self, now, s):
        """Static non-person rejection: a radar track the camera has looked at for verify_s without
        seeing a face / person / hand is marked "object" and ignored (and its spot remembered)."""
        self.clutter = [c for c in self.clutter if c[2] > now]
        for tr in self.tracker.tracks:
            if tr.kind == "object":
                # only real displacement un-rejects it: speed spikes from a fan / curtain / limb do not
                moved = math.hypot(tr.x - tr.anchor[0], tr.y - tr.anchor[1]) > 500
                if moved or now - tr.kind_t > s["object_ignore_s"]:
                    tr.kind, tr.kind_t = "verify", now
            elif tr.kind == "verify" and now - tr.born < 1.0:
                for cx, cy, until, label in self.clutter:
                    if math.hypot(tr.x - cx, tr.y - cy) < 400:
                        tr.kind, tr.kind_t, tr.anchor, tr.obj_label = "object", now, (tr.x, tr.y), label
                        break
        p = self.tracker.primary
        g = self.guide
        v = self.verify
        if (p is None or p.kind != "verify" or g["mode"] not in ("radar", "search") or self.vision is None
                or not s["vision_enabled"] or self.calib["mode"] != "off"):
            v["id"], v["since"] = None, None
            return
        settled = abs(g["pan_goal"] - g["pan_cmd"]) < 3.0 and abs(g["tilt_goal"] - g["tilt_cmd"]) < 3.0
        if not settled:
            v["since"] = None
            return
        if v["id"] != p.id or v["since"] is None:
            v.update(id=p.id, since=now, vis_n0=self.vis_count)
            return
        objs_fresh = self.objects is None or self.objects.result["t"] > v["since"] + 0.3
        searched = (not s["search_enabled"] or (self.search["tid"] == p.id
                                                 and self.search["i"] >= len(self.search_pattern(s))))
        if now - v["since"] >= s["verify_s"] and self.vis_count - v["vis_n0"] >= 3 and objs_fresh and searched:
            label = self.object_label_at_center()
            p.kind, p.kind_t, p.anchor, p.obj_label = "object", now, (p.x, p.y), label
            self.clutter.append([p.x, p.y, now + s["object_ignore_s"], label])
            self.event("非人目标", f"T{p.id} r={p.r_m:.2f} m 画面里没有人{('，看到 ' + label) if label else ''}，"
                                   f"忽略 {s['object_ignore_s']:.0f} s（它动起来会重新核实）")
            v["id"], v["since"] = None, None
            self.tracker.reselect()

    def object_label_at_center(self):
        items = self.objects_result(max_age=2.0)
        best, bd = "", 0.35
        for o in items:
            if o["label"] in ("person", "hand"):
                continue
            d = abs(o["x"] + o["w"] / 2 - 0.5)
            if d < bd:
                best, bd = o["cn"], d
        return best

    def objects_result(self, max_age=1.2):
        if self.objects is None:
            return []
        r = self.objects.result
        return r["items"] if time.monotonic() - r["t"] < max_age else []

    def trigger_alarm(self, now, key, why, s, extra=None):
        self.last_alarm_t = now
        self.alarm_last[key] = now
        self.alarm_until = now + C.ALARM_HOLD_S
        self.pending_snaps.append((now + 0.1, f"alarm-{key}", ("告警", why, "alarm"), extra))

    def target_key(self):
        p = self.tracker.primary
        if self.vt["active"]:
            tr = self.vt["track"]
            if tr is not None and tr in self.tracker.tracks:
                return f"T{tr.id}"
            return f"V{self.vt['session']}"
        return f"T{p.id}" if p is not None and p.kind != "object" else None

    def update_state(self, now, s):
        """7.5 state machine (called with self.lock held)."""
        self.tracker.expire(now)
        vt = self.vt
        if vt["active"] and now - vt["last"] > s["vis_lost_s"]:
            vt.update(active=False, goal=None, track=None, seen_n=0)
            self.tracker.prefer_id = None
            self.event("视觉丢失", f"{s['vis_lost_s']:.1f} s 没看到人，交回雷达")
        self.update_kinds(now, s)
        p = self.tracker.primary
        if p is not None or vt["active"]:
            self.last_target_t = now
        pid = p.id if p else None
        if pid != self.last_primary_id:
            if p is not None:
                cmd = pointing.command_for_target(p.x, p.y, s)
                self.event("雷达目标", f"T{p.id} X={s['x_sign'] * p.x:+.0f} Y={p.y:.0f} mm r={p.r_m:.2f} m "
                                     f"θ={cmd.azimuth_deg:+.1f}° ({p.kind})")
            elif self.last_primary_id is not None:
                self.event("目标丢失", f"T{self.last_primary_id}")
            self.last_primary_id = pid
        person = vt["active"] or (p is not None and p.kind == "person" and now - self.last_detect_t < 1.5)
        if person and self.person_since is None:
            self.person_since = now
        elif not person:
            self.person_since = None
        if not self.armed:
            self.state = "DISARMED"
            return
        key = self.target_key()
        new_state = "LOCKED" if person else ("VERIFY" if p is not None and p.kind != "object" else "ARMED")
        if new_state == "LOCKED" and self.state not in ("LOCKED", "KNOWN", "ALARM") and key:
            r = f" r={p.r_m:.2f} m" if p is not None else ""
            self.event("锁定", f"{key} 确认是人（{vt['kind'] or '雷达+视觉'}）{r}")
            if (s["snapshot_on_lock"] and key not in self.snapped_keys
                    and now - self.last_auto_snap_t >= s["snapshot_min_interval_s"]):
                self.snapped_keys.add(key)
                self.last_auto_snap_t = now
                self.pending_snaps.append((now + s["snapshot_delay_s"], f"lock-{key}", None, None))
        if person and key and self.person_since is not None and now - self.person_since >= s["alarm_confirm_s"] \
                and now - self.alarm_last.get(key, -1e9) > s["alarm_cooldown_s"] and now - self.last_alarm_t > 5.0:
            info = self.ident.get(key, {})
            if info.get("status") != "known":
                extra = self.track_summary(p, s) if p is not None else {"key": key}
                if self.is_far(p, s):
                    # nobody to compare yet: give the identity check time (the face often shows up a moment
                    # after the body), and don't alarm on a body right after a household member was recognised
                    if (s["alarm_far_detect"] and now - self.person_since >= s["far_alarm_delay_s"]
                            and now - self.last_known_t > s["known_hold_s"] and not self.face_seen_recently(now, s)):
                        self.trigger_alarm(now, key, f"{key} 比对距离外看到人（{vt['kind'] or '人'}，身份未知）", s, extra)
                elif info.get("status") == "unknown":
                    self.trigger_alarm(now, key, f"{key} 未知人员 (相似度 {info.get('score') or 0:.2f}"
                                                 f"{' ' + info['best'] if info.get('best') else ''})", s, extra)
        if p is not None and p.kind != "object" and s["alarm_range_m"] > 0 and p.r_m <= s["alarm_range_m"]:
            k = f"T{p.id}"
            if self.ident.get(k, {}).get("status") != "known" and now - self.alarm_last.get(k, -1e9) > s["alarm_cooldown_s"]:
                self.trigger_alarm(now, k, f"{k} 接近到 {p.r_m:.2f} m（无视觉接近告警）", s, self.track_summary(p, s))
        if new_state == "LOCKED" and key and self.ident.get(key, {}).get("status") == "known":
            new_state = "KNOWN"           # recognised household member: own state (green), not yellow "locked"
        self.state = "ALARM" if now < self.alarm_until else new_state

    def face_seen_recently(self, now, s, window=2.0):
        """A face big enough to compare was seen in the last `window` s -> wait for the identity result."""
        res = self.vision_res
        if not res or now - res["t"] > window:
            return False
        need = s["min_eye_px"] * C.CAM_WIDTH / C.STILL_WIDTH
        return any(f.get("eye_px", 0) >= need for f in res["faces"])

    def is_far(self, p, s):
        if p is not None and (self.vt["track"] is p or not self.vt["active"]):
            return p.r_m > s["identify_range_m"]
        res = self.vision_res
        t = res["target"] if res else None
        if self.vt["active"] and self.vt.get("face_session") == self.vt["session"]:
            return False        # this vision session already showed a face big enough to compare
        if t and t["kind"] in ("head", "profile", "face_weak"):
            return False        # a face was big enough moments ago and just turned away: wait for it
        need = s["min_eye_px"] * C.CAM_WIDTH / C.STILL_WIDTH
        return not (t and t["kind"] == "face" and t["eye_px"] >= need)

    # ------------------------------------------------------------------ vision callbacks
    def vision_active(self):
        now = time.monotonic()
        s = self.settings.get()
        return (s["detect_always"] or now < self.force_detect_until or self.enroll["running"]
                or self.calib["mode"] != "off" or self.vt["active"]
                or (self.armed and (self.state in ("VERIFY", "LOCKED", "KNOWN", "ALARM") or self.guide["mode"] == "patrol")))

    def objects_active(self):
        return self.vision_active()

    def calib_motion_active(self):
        # calibration, or an active search with the gimbal standing still (frame differencing needs a still camera)
        return self.calib["mode"] == "points" or (self.search["active"] and self.pan_tilt_at_rest(0.6, 0.3))

    # ------------------------------------------------------------------ active search around the radar bearing
    def search_pattern(self, s):
        d, t = s["search_pan_deg"], s["search_tilt_deg"]
        return [(0, 0), (-d / 2, 0), (d / 2, 0), (-d, 0), (d, 0), (-d / 2, t), (d / 2, t), (0, t), (0, -t / 2)]

    def radar_goal(self, now, p, cmd, s):
        """Radar pointing with Kalman lead; when the camera is on the radar bearing but vision sees nobody for
        search_after_s, step through a small pattern around it (dwell at each point for the detectors), and
        jump to frame-difference motion seen while the gimbal stands still."""
        base = (cmd.pan_deg, cmd.tilt_deg)
        if s["lead_s"] > 0 and p.confirmed:
            c2 = pointing.command_for_target(p.x + p.kx.v * s["lead_s"], p.y + p.ky.v * s["lead_s"], s)
            base = (c2.pan_deg, c2.tilt_deg)
        S, g = self.search, self.guide
        if S["tid"] != p.id:
            S.update(tid=p.id, aim_t=None, i=0, step_t=None, active=False, motion=None, base=base)
        S["base"] = base
        if not s["search_enabled"] or self.vision is None or not s["vision_enabled"] or p.kind == "object":
            return "radar", base
        if S["aim_t"] is None:
            if abs(g["pan_cmd"] - base[0]) < 4 and abs(g["tilt_cmd"] - base[1]) < 4:
                S["aim_t"] = now
            return "radar", base
        if now - max(S["aim_t"], self.last_detect_t) < s["search_after_s"]:
            return "radar", base
        if not S["active"]:
            S.update(active=True, step_t=None, motion=None)
            self.event("主动搜索", f"T{p.id} r={p.r_m:.2f} m 画面里没人，在雷达方位 {base[0]:+.1f}° 附近搜索")
        m = S.get("motion")
        if m and now - m[2] < 1.2:
            return "search", (m[0], m[1])
        pat = self.search_pattern(s)
        off = pat[S["i"] % len(pat)]
        goal = (pointing.limit_pan(base[0] + off[0]), pointing.limit_tilt(base[1] + off[1]))
        if abs(g["pan_cmd"] - goal[0]) < 1.5 and abs(g["tilt_cmd"] - goal[1]) < 1.5:
            if S["step_t"] is None:
                S["step_t"] = now
            elif now - S["step_t"] >= s["search_dwell_s"]:
                S["i"] += 1
                S["step_t"] = None
        return "search", goal

    def image_angles(self, u, v, s):
        """normalised image point -> (pan, tilt) offset in degrees from the optical axis (pinhole)."""
        du = math.degrees(math.atan((u - 0.5) * 2 * math.tan(math.radians(s["cam_hfov_deg"] / 2))))
        dv = math.degrees(math.atan((v - 0.5) * 2 * math.tan(math.radians(s["cam_vfov_deg"] / 2))))
        return du, dv

    def project_track(self, tr, s, pan, tilt):
        """Where the radar says a track is in the current camera image (normalised u, v; may be outside 0..1)."""
        c = pointing.command_for_target(tr.x, tr.y, s)
        dp = self.mapped_pan(tr, s) - pan
        dt = c.tilt_deg - tilt
        dp, dt = max(-88.0, min(88.0, dp)), max(-88.0, min(88.0, dt))     # far outside: still an edge arrow
        u = 0.5 + math.tan(math.radians(dp)) / (2 * math.tan(math.radians(s["cam_hfov_deg"] / 2)))
        v = 0.5 - math.tan(math.radians(dt)) / (2 * math.tan(math.radians(s["cam_vfov_deg"] / 2)))
        return round(u, 3), round(v, 3)

    def on_vision(self, res):
        s = self.settings.get()
        now = time.monotonic()
        with self.lock:
            self.vision_res = res
            self.vis_count += 1
            if self.wake_req_t is not None and (res["faces"] or res["persons"] or res["target"]):
                w = self.wake_detect_log[-1] if self.wake_detect_log else None
                if w is not None and w.get("detect_ms") is None:
                    w["detect_ms"] = int((now - self.wake_req_t) * 1000)
                self.wake_req_t = None
            t = res["target"]
            vt = self.vt
            pan0, tilt0 = self.pose_at(res["frame_t"])
            if self.calib["mode"] == "points":
                self.calib_candidate(res, pan0, tilt0, s, now)
                return
            S = self.search
            if S["active"] and t is None and res.get("motion") and self.pan_tilt_at_rest(0.6, 0.3):
                mb = max(res["motion"], key=lambda m: m["energy"])
                du, dv = self.image_angles(mb["x"] + mb["w"] / 2, mb["y"] + mb["h"] * 0.3, s)
                mp, mt = pan0 + du, tilt0 - dv
                mt = max(S["base"][1] - s["search_tilt_deg"], min(S["base"][1] + s["search_tilt_deg"], mt))
                if abs(mp - S["base"][0]) <= s["search_pan_deg"] + 8 and mb["energy"] > 20:
                    if not S.get("motion") or now - S["motion"][2] > 1.2:
                        self.event("主动搜索", f"T{S['tid']} 画面移动 @ {mp:+.1f}°/{mt:+.1f}°，对准后等检测")
                    S["motion"] = (pointing.limit_pan(mp), pointing.limit_tilt(mt), now)
            if t is None or self.manual or not self.armed:
                if t is None and now - vt.get("seen_t", 0.0) > 2.5:
                    vt["seen_n"] = 0
                return
            # person / hand boxes come from the slower object detector: use the pose of THAT frame and act
            # only once per new detection (re-using a stale box every vision frame winds the gimbal up)
            if t["src_id"] == vt.get("src_id"):
                return
            vt["src_id"] = t["src_id"]
            pan0, tilt0 = self.pose_at(t["src_t"])
            bearing = pan0 + t["eu_deg"]
            tr = self.match_radar(bearing, s)
            if t["kind"] == "hand":
                # a hand alone never confirms a person (palm detector has false positives); it only tells
                # which radar target the person is, so that target is preferred
                if tr is not None and tr.kind != "object" and not vt["active"]:
                    self.tracker.prefer_id = tr.id
                    self.tracker.reselect()
                return
            if t["kind"] in ("head", "profile", "face_weak"):
                # the face turned away: a low-score / profile face or the KCF head tracker only CONTINUES an
                # existing vision track, it never starts one (never confirms a person on its own)
                if not vt["active"]:
                    return
                need = 0
            else:
                need = 2 if t["kind"] == "face" else s["person_frames"] + (0 if tr is not None else 1)
            self.last_detect_t = now
            if t["kind"] == "face" and t.get("eye_px", 0) >= s["min_eye_px"] * C.CAM_WIDTH / C.STILL_WIDTH:
                vt["face_seen_t"] = now
                if vt["active"]:
                    vt["face_session"] = vt["session"]
            vt["seen_n"] += 1
            vt["seen_t"] = now
            if vt["seen_n"] < need:
                return
            if tr is not None and tr.kind != "person":
                tr.kind, tr.kind_t = "person", now
            if not vt["active"]:
                if not s["vision_priority"]:
                    return
                vt.update(active=True, since=now, session=vt["session"] + 1)
                lk = self.last_known_track
                if lk and now - lk[2] < s["known_hold_s"] and (lk[1] is None or abs(lk[1] - bearing) < 20):
                    # same place, shortly after a recognition (face turned away / brief loss): keep the identity
                    self.ident[f"V{vt['session']}"] = {"status": "known", "name": lk[0], "score": lk[3], "attempts": 0,
                                                     "last_try": now, "t": time.strftime("%H:%M:%S"), "inherited": True}
                    self.last_known_track = (lk[0], bearing, now, lk[3])
                self.event("视觉锁定", f"看到{KIND_CN[t['kind']]}，"
                                     f"改为视觉跟踪{('（对应雷达 T%d）' % tr.id) if tr else '（雷达没有对应目标）'}")
            vt.update(last=now, kind=t["kind"], track=tr, bearing=round(bearing, 1))
            ki = self.ident.get(f"V{vt['session']}", {})
            if ki.get("status") == "known":
                self.last_known_track = (ki.get("name"), bearing, now, ki.get("score"))
            near = tr
            if near is None and t["kind"] == "face":
                cands = [x for x in self.tracker.tracks if x.confirmed and x.kind != "object"]
                near = min(cands, key=lambda x: abs(self.mapped_pan(x, s) - bearing)) if cands else None
            if near is not None and t["kind"] == "face" and vt.get("assoc_key") != (vt["session"], near.id):
                # radar bearing vs detected face bearing, logged once per lock (session, track); also logged when
                # the nearest track is outside the association gate (then marked "gate")
                tr_matched, tr = tr, near
                vt["assoc_key"] = (vt["session"], tr.id)
                err = bearing - self.mapped_pan(tr, s)
                self.assoc_log.append({"t": time.strftime("%H:%M:%S"), "track": tr.id, "r_m": round(tr.r_m, 2),
                                       "face_deg": round(bearing, 1), "radar_deg": round(self.mapped_pan(tr, s), 1),
                                       "err_deg": round(err, 1), "speed": tr.speed, "gated": tr_matched is None})
                aw = self.assoc_warn
                if tr_matched is None:
                    aw["n"] += 1
                    aw["last_err"] = round(err, 1)
                else:
                    aw["n"] = 0
                if tr_matched is not None or now - aw["t"] > 120:
                    if tr_matched is None:
                        aw["t"] = now
                    self.event("关联", f"V{vt['session']} {'=' if tr_matched else '≠ (超出门限) 最近'} T{tr.id} r={tr.r_m:.2f} m "
                                     f"脸方位 {bearing:+.1f}° 雷达方位 {self.mapped_pan(tr, s):+.1f}° 方位误差 {err:+.1f}°"
                                     + ("（雷达与云台方向不一致，请重新标定）" if tr_matched is None and aw["n"] >= 3 else ""))
                tr = tr_matched
            self.tracker.prefer_id = tr.id if tr else None
            self.tracker.reselect()
            g = vt["goal"] or (self.guide["pan_goal"], self.guide["tilt_goal"])
            k = s["vis_track_gain"]
            db = s["vis_deadband_px"]
            gp = pan0 + k * t["eu_deg"] if abs(t["eu_px"]) > db else g[0]
            gt = tilt0 - k * t["ev_deg"] if abs(t["ev_px"]) > db else g[1]
            vt["goal"] = (pointing.limit_pan(gp), pointing.limit_tilt(gt))

    def pan_tilt_at_rest(self, window=0.25, tol=1.5):
        now = time.monotonic()
        pts = [(p, t) for (ts, p, t) in list(self.cmd_hist) if now - ts <= window]
        if len(pts) < 3:
            return False
        ps, ts_ = [a for a, _ in pts], [b for _, b in pts]
        return max(ps) - min(ps) < tol and max(ts_) - min(ts_) < tol

    # ------------------------------------------------------------------ identity (7.6)
    def identify_once(self, s, roi=None):
        """Full-res still -> SFace feature -> gallery match. Returns (status, info).
        roi = normalised box of the live face (used if the face is too small for the quick full-frame pass)."""
        jpg = self.camera.capture_still()
        feat, info, crop = self.vision.det.feature_from_still(jpg, s["face_score"], s["min_eye_px"],
                                                              s["identify_max_yaw_deg"], roi=roi)
        if feat is None:
            return "noface", info
        name, score = self.gallery.match(feat, info.get("yaw"))
        info.update(best=name, score=round(score, 3))
        thr = s["face_threshold"] + (s["face_side_margin"] if abs(info.get("yaw") or 0) > 30 else 0.0)
        self.score_log.append({"t": time.strftime("%H:%M:%S"), "best": name, "score": round(score, 3), "thr": round(thr, 3),
                               "eye_px": info.get("eye_px"), "yaw": info.get("yaw")})
        if name and score >= thr:
            return "known", info
        return "mismatch", info

    def identity_loop(self):
        """Admission: (radar r <= identify_range or big enough face), face near centre, pan-tilt at
        rest ~250 ms; retry every 0.5 s, max N tries per target key."""
        while self.running:
            time.sleep(0.2)
            if self.vision is None or self.enroll["running"] or self.calib["mode"] != "off":
                continue
            s = self.settings.get()
            now = time.monotonic()
            with self.lock:
                key = self.target_key()
                res = self.vision_res
                p = self.tracker.primary
                if not (self.armed and key and s["vision_enabled"]):
                    continue
                info = self.ident.setdefault(key, {"status": "pending", "attempts": 0, "last_try": 0.0})
                if info["status"] == "known":
                    continue      # "unknown" keeps re-checking (1/s): a later good frontal match still clears it
                if self.is_far(p, s):
                    info["status"] = "far"
                    continue
                if info["status"] != "unknown":
                    info["status"] = "pending"
                t = res["target"] if res and now - res["t"] < 0.5 else None
                # a body without a face on the stream (too small / far): try its upper part at full resolution
                pready = (t is not None and t["kind"] == "person" and t.get("box") and self.pan_tilt_at_rest(tol=3.0)
                          and now - info["last_try"] >= 2.0)
                ready = pready or (t is not None and t["kind"] in ("face", "face_weak") and abs(t["eu_px"]) < 0.3 * C.CAM_WIDTH
                         and abs(t["ev_px"]) < 0.35 * C.CAM_HEIGHT and self.pan_tilt_at_rest(tol=3.0)
                         and now - info["last_try"] >= (1.0 if info["status"] == "unknown" else 0.5))
                # only near-frontal faces are compared (YuNet landmark yaw from the live frame; the still is
                # checked again). A side face just waits - it neither matches nor counts as "unknown".
                side = t is not None and t["kind"] in ("face", "face_weak", "profile", "head") and (
                    t["kind"] in ("profile", "head") or abs(t.get("yaw") or 0) > s["identify_max_yaw_deg"])
                info["side"] = bool(side)
                info.setdefault("wait_since", now)
                if (not ready or side) and info["status"] != "unknown":
                    # someone close who never shows a comparable frontal face (keeps the face turned / back to
                    # the camera): after identify_wait_s count as unknown, unless a household member was just
                    # recognised (known_hold_s)
                    if (s["identify_wait_s"] > 0 and self.person_since is not None
                            and now - max(info["wait_since"], self.person_since) > s["identify_wait_s"]
                            and now - self.last_known_t > s["known_hold_s"]):
                        info["status"] = "unknown"
                        info["soft"] = True       # keeps comparing; a later match still makes it "known"
                        info["error"] = f"{s['identify_wait_s']:.0f} s 内没看到可比对的正脸" + ("（侧脸）" if side else "")
                        self.event("身份未知", f"{key} {info['error']}")
                    continue
                if not ready or side:
                    continue
                info["last_try"] = now
                info["wait_since"] = now          # the "never shows a frontal face" timer restarts at each try
                info["attempts"] += 1
                roi = t.get("box")
                if t["kind"] == "person" and roi:
                    roi = [roi[0], roi[1], roi[2], roi[3] * 0.35]     # head + shoulders
            try:
                if not self.gallery.people:
                    status, finfo = "mismatch", {"error": "白名单为空"}
                else:
                    status, finfo = self.identify_once(s, roi)
            except Exception as exc:
                status, finfo = "noface", {"error": repr(exc)}
            with self.lock:
                info = self.ident.setdefault(key, {"status": "pending", "attempts": 1, "last_try": now})
                info.update({k: finfo.get(k) for k in ("best", "score", "eye_px", "error", "ms") if k in finfo})
                prim = self.tracker.primary
                extra = {**(self.track_summary(prim, s) or {}), "key": key}
                if status == "known":
                    info["status"] = "known"
                    info["name"] = finfo["best"]
                    info["t"] = time.strftime("%H:%M:%S")
                    self.last_known_track = (finfo["best"], self.vt.get("bearing"), time.monotonic(), info.get("score"))
                    self.known_blink_until = time.monotonic() + 0.8
                    self.known_banner = (finfo["best"], time.monotonic() + 10)
                    self.last_known_t = time.monotonic()
                    if self.alarm_until > time.monotonic():
                        self.alarm_until = 0.0
                    self.event("已知人员", f"{key} {finfo['best']} 相似度 {finfo['score']:.2f} 眼距 {finfo.get('eye_px')} px",
                               mqtt_type="known", extra={**extra, "name": finfo["best"], "score": finfo["score"]})
                elif status == "mismatch":
                    sc = finfo.get("score") or 0.0
                    info.setdefault("scores", []).append(round(sc, 3))
                    near = [x for x in info["scores"][-4:] if x >= s["face_threshold"] - s["face_vote_margin"]]
                    if s["face_vote_margin"] > 0 and len(near) >= 2 and finfo.get("best"):
                        status = "known"        # two near-threshold looks at the same best match = vote
                        info.update(status="known", name=finfo["best"])
                        self.last_known_t = time.monotonic()
                        self.known_banner = (finfo["best"], time.monotonic() + 10)
                        if self.alarm_until > time.monotonic():
                            self.alarm_until = 0.0
                        self.event("已知人员", f"{key} {finfo['best']} 多帧投票 {near}", mqtt_type="known",
                                   extra={**extra, "name": finfo["best"], "score": sc})
                    elif (finfo.get("eye_px") or 0) >= s["min_eye_px"] * 1.5 and abs(finfo.get("yaw") or 0) <= 30:
                        info["mismatch"] = info.get("mismatch", 0) + 1   # only good-quality looks count
                # only real comparisons (face found, frontal, big enough, low score) count towards "unknown";
                # "no face / not frontal" just retries (up to 3x as many tries)
                if status == "known" or info.get("soft") or info.get("status") == "unknown":
                    pass
                elif (not self.gallery.people or info.get("mismatch", 0) >= s["identify_attempts"]
                        or info["attempts"] >= 3 * s["identify_attempts"]):
                    info["status"] = "unknown"
                    self.event("身份未知", f"{key} 第 {info['attempts']} 次: "
                                        f"{finfo.get('error') or ('最像 %s %.2f' % (finfo.get('best'), finfo.get('score') or 0))}")
                else:
                    self.event("比对重试", f"{key} 第 {info['attempts']} 次: "
                                        f"{finfo.get('error') or ('最像 %s %.2f' % (finfo.get('best'), finfo.get('score') or 0))}")

    def identify_now(self):
        """Web button: one identification of whatever is in front of the camera (ignores gates)."""
        if self.vision is None:
            return "视觉不可用"
        s = self.settings.get()
        try:
            if not self.gallery.people:
                return "白名单为空，请先录入"
            res = self.vision_res
            t = res["target"] if res and time.monotonic() - res["t"] < 1.0 else None
            status, info = self.identify_once(s, t.get("box") if t and t["kind"] in ("face", "face_weak") else None)
        except Exception as exc:
            return f"失败: {exc}"
        msg = (f"{status} 最像 {info.get('best')} 相似度 {info.get('score')} 眼距 {info.get('eye_px')} px "
               f"{info.get('error') or ''} ({info.get('ms')} ms)")
        self.event("手动比对", msg)
        if status == "known":
            with self.lock:
                key = self.target_key()
                if key:
                    self.ident[key] = {"status": "known", "name": info.get("best"), "score": info.get("score"),
                                       "attempts": 1, "last_try": time.monotonic()}
                self.last_known_t = time.monotonic()
                self.alarm_until = 0.0
        return msg

    # ------------------------------------------------------------------ enrollment
    def enroll_job(self, name, n):
        """Enrollment with the monitoring camera (full-resolution stills)."""
        s = self.settings.get()
        e = self.enroll
        quota = pose_quota(n)
        got = {k: 0 for k in quota}
        e.update(running=True, name=name, done=0, target=n, msg="开始：" + POSE_PROMPT["front"], log=[], poses=got)
        self.event("录入开始", f"{name} 监控摄像头 目标 {n} 张（正面 {quota['front']} / 向左 {quota['right']} / 向右 {quota['left']}）")
        feats, crops, yaws = [], [], []
        tries = 0
        try:
            while len(feats) < n and tries < n * 4 and self.running:
                tries += 1
                jpg = self.camera.capture_still()
                feat, info, crop = self.vision.det.feature_from_still(jpg, s["face_score"], s["min_eye_px"],
                                                                      s["enroll_max_yaw_deg"])
                if feat is None:
                    e["log"].append(f"第 {tries} 次失败: {info.get('error')} 眼距 {info.get('eye_px')}")
                elif got[info["pose"]] >= quota[info["pose"]]:
                    e["log"].append(f"第 {tries} 次跳过: {POSE_CN[info['pose']]} 已够 (yaw {info['yaw']:+.0f}°)")
                else:
                    feats.append(feat)
                    crops.append(crop)
                    yaws.append(info["yaw"])
                    got[info["pose"]] += 1
                    e["log"].append(f"第 {tries} 次 OK: {POSE_CN[info['pose']]} yaw {info['yaw']:+.0f}° "
                                    f"置信 {info['score']} 眼距 {info['eye_px']} px")
                e["done"] = len(feats)
                nxt = next_pose(got, quota)
                e["msg"] = f"已采 {len(feats)}/{n} " + (POSE_PROMPT[nxt] if nxt else "")
                time.sleep(0.8)
            if len(feats) >= max(3, n // 2):
                total = self.gallery.save(name, feats, crops, append=True, yaws=yaws)
                e["msg"] = f"完成: {name} 新增 {len(feats)} 张，共 {total} 张，一致性 {consistency(feats):.2f}"
                self.event("录入完成", e["msg"])
            else:
                e["msg"] = f"失败: 只采到 {len(feats)} 张合格人脸（至少 {max(3, n // 2)}）"
                self.event("录入失败", e["msg"])
        except Exception as exc:
            e["msg"] = f"失败: {exc}"
            self.event("录入失败", str(exc))
        finally:
            e["running"] = False

    def enroll_upload(self, sid, name, n, jpg):
        """Phone / laptop enrollment: one browser camera frame. Returns progress dict."""
        if self.vision is None:
            raise RuntimeError("视觉不可用")
        name = name.strip()
        if not vision.NAME_RE.match(name):
            raise ValueError("名字只能是 1-32 个字母/数字/汉字/空格/-/_")
        now = time.monotonic()
        for k in [k for k, v in self.enroll_sessions.items() if now - v["t"] > 600]:
            del self.enroll_sessions[k]
        ses = self.enroll_sessions.setdefault(sid, {"name": name, "n": n, "feats": [], "crops": [], "yaws": [],
                                                    "t": now, "tries": 0, "quota": pose_quota(n),
                                                    "got": {"front": 0, "left": 0, "right": 0}})
        ses["t"] = now
        ses["tries"] += 1
        if len(ses["feats"]) >= ses["n"]:
            return {"got": len(ses["feats"]), "need": ses["n"], "done": True, "info": {}, "poses": ses["got"]}
        s = self.settings.get()
        feat, info, crop = self.vision.det.feature_from_still(jpg, min(s["face_score"], 0.75), s["enroll_min_eye_px"],
                                                              s["enroll_max_yaw_deg"])
        if feat is not None:
            pose = info["pose"]
            # after many tries the side quotas are relaxed (some people / phones just don't get there)
            relaxed = ses["tries"] > 3 * ses["n"]
            if ses["feats"] and max(float(vision.np.dot(feat, f)) for f in ses["feats"]) > 0.995:
                info["error"] = "和上一张几乎一样，请稍微转动头部"
            elif ses["got"][pose] >= ses["quota"][pose] and not relaxed:
                info["error"] = f"{POSE_CN[pose]}已经够了"
            else:
                ses["feats"].append(feat)
                ses["crops"].append(crop)
                ses["yaws"].append(info["yaw"])
                ses["got"][pose] += 1
        info.pop("lm", None)
        nxt = next_pose(ses["got"], ses["quota"])
        return {"got": len(ses["feats"]), "need": ses["n"], "done": len(ses["feats"]) >= ses["n"], "info": info,
                "tries": ses["tries"], "poses": ses["got"], "quota": ses["quota"],
                "prompt": POSE_PROMPT[nxt] if nxt else "好了"}

    def enroll_finish(self, sid):
        ses = self.enroll_sessions.pop(sid, None)
        if not ses:
            raise ValueError("会话不存在或已过期")
        if len(ses["feats"]) < 3:
            raise ValueError(f"只有 {len(ses['feats'])} 张合格人脸，至少 3 张")
        total = self.gallery.save(ses["name"], ses["feats"], ses["crops"], append=True, yaws=ses["yaws"])
        msg = f"{ses['name']} 新增 {len(ses['feats'])} 张（手机/电脑摄像头），共 {total} 张，一致性 {consistency(ses['feats']):.2f}"
        self.event("录入完成", msg)
        return msg

    # ------------------------------------------------------------------ calibration
    def calib_action(self, data):
        a = data.get("action")
        c = self.calib
        s = self.settings.get()
        with self.lock:
            if c["busy"] and a not in ("stop",):
                raise RuntimeError("方向自检进行中，请等几秒")
            if a == "selftest":
                c.update(mode="selftest", busy=True, msg="方向自检：云台会左右、上下各转 8°", result=None)
                threading.Thread(target=self.selftest_job, daemon=True).start()
            elif a == "start":
                pose = [pointing.limit_pan(float(data.get("pan", 0))), pointing.limit_tilt(float(data.get("tilt", 0)))]
                c.update(mode="points", pose=pose, cand=None,
                         msg="标定中：云台固定不动。站到画面里不同位置，在脸旁边挥手，确认红框是你，然后点“记录这一点”")
                if self.vision:
                    self.vision.motion.reset()
                self.event("标定", f"开始取点，云台固定在 pan={pose[0]:+.0f}° tilt={pose[1]:+.0f}°")
            elif a == "record":
                cand = c["cand"]
                if c["mode"] != "points":
                    raise RuntimeError("先点“开始标定”")
                if not cand or time.monotonic() - cand["t"] > 1.5:
                    raise RuntimeError("现在没有候选（没检测到挥手/人脸）")
                if cand["track"] is None:
                    raise RuntimeError("雷达这时没有对应目标，换个位置再试")
                c["samples"].append({k: v for k, v in cand.items() if k != "t"})
                c["msg"] = f"已记录 {len(c['samples'])} 个点（建议 3-6 个，左中右、远近都要有）"
                self.event("标定", f"记录第 {len(c['samples'])} 点: 雷达 θ={cand['track']['az_raw']:+.1f}° r={cand['track']['r']:.2f} m"
                                 f" → 实际 pan={cand['pan_true']:+.1f}° tilt={cand['tilt_true']:+.1f}° ({cand['kind']})")
            elif a == "undo":
                if c["samples"]:
                    c["samples"].pop()
                c["msg"] = f"剩 {len(c['samples'])} 个点"
            elif a == "clear":
                c["samples"] = []
                c["msg"] = "已清空所有点"
            elif a == "fit":
                c["result"] = self.calib_fit(s, apply=bool(data.get("apply", True)))
                c["msg"] = c["result"]["msg"]
            elif a == "stop":
                c.update(mode="off", cand=None, msg="已退出标定")
                self.event("标定", "退出标定")
            else:
                raise ValueError(f"未知标定命令 {a}")
            return {k: v for k, v in c.items()}

    def calib_candidate(self, res, pan0, tilt0, s, now):
        """Calibration candidate: a face if there is one (gives tilt too), else the strongest motion blob."""
        W, H = C.CAM_WIDTH, C.CAM_HEIGHT
        t = res["target"]
        cand = None
        if t is not None and t["kind"] == "face":
            cand = {"kind": "face", "u": t["u"], "v": t["v"], "box": t["box"]}
        elif res.get("motion"):
            m = max(res["motion"], key=lambda m: m["energy"])
            cand = {"kind": "motion", "u": (m["x"] + m["w"] / 2) * W, "v": (m["y"] + m["h"] / 2) * H,
                    "box": [m["x"], m["y"], m["w"], m["h"]]}
        if cand is None:
            return
        eu = pointing.pixel_to_deg(cand["u"] - W / 2, W, s["cam_hfov_deg"])
        ev = pointing.pixel_to_deg(cand["v"] - H / 2, H, s["cam_vfov_deg"])
        pan_true, tilt_true = pan0 + eu, tilt0 - ev
        tracks = [tr for tr in self.tracker.tracks if tr.confirmed]
        tr = min(tracks, key=lambda tr: abs(self.mapped_pan(tr, s) - pan_true)) if tracks else None
        cand.update(t=now, pan_true=round(pan_true, 2), tilt_true=round(tilt_true, 2),
                    track=None if tr is None else {"id": tr.id, "az_raw": round(math.degrees(math.atan2(tr.x, tr.y)), 2),
                                                   "r": round(tr.r_m, 3), "x": round(tr.x), "y": round(tr.y),
                                                   "pred_pan": round(self.mapped_pan(tr, s), 1)})
        self.calib["cand"] = cand

    def calib_fit(self, s, apply=True):
        sm = self.calib["samples"]
        if not sm:
            return {"ok": False, "msg": "没有标定点"}
        az = [p["track"]["az_raw"] for p in sm]
        pt = [p["pan_true"] for p in sm]
        n = len(sm)
        new = {}
        notes = []
        if n >= 2 and max(az) - min(az) >= 8.0:
            ma, mp = sum(az) / n, sum(pt) / n
            sxx = sum((a - ma) ** 2 for a in az)
            a = sum((x - ma) * (y - mp) for x, y in zip(az, pt)) / sxx
            b = mp - a * ma
            if 0.5 <= abs(a) <= 1.5:
                new.update(x_sign=1 if a > 0 else -1, az_scale=round(abs(a), 3), theta0_deg=round(b, 2))
            else:
                notes.append(f"斜率 {a:.2f} 不合理（点太少或配错了雷达目标），只改偏角")
                a = s["x_sign"] * s["az_scale"]
                new["theta0_deg"] = round(sum(y - a * x for x, y in zip(az, pt)) / n, 2)
        else:
            # not enough spread for a slope: still decide LEFT/RIGHT. Try both x_sign values with the current
            # scale; a real mounting offset is small, so a fit that needs a big theta0 is the wrong sign.
            best = None
            for sg in (1, -1):
                a = sg * s["az_scale"]
                th = sum(y - a * x for x, y in zip(az, pt)) / n
                rms = math.sqrt(sum((a * x + th - y) ** 2 for x, y in zip(az, pt)) / n)
                cost = rms + 0.5 * max(0.0, abs(th) - 10.0)
                if best is None or cost < best[0]:
                    best = (cost, sg, th)
            _, sg, th = best
            new["theta0_deg"] = round(th, 2)
            if sg != s["x_sign"]:
                new["x_sign"] = sg
                notes.append(f"按点的左右关系把 x_sign 改为 {sg:+d}（另一种方向需要 θ0 偏很大，不合理）")
            notes.append("点的左右跨度不够 8°，比例不变，只定方向和水平偏角 θ0")
        a = new.get("x_sign", s["x_sign"]) * new.get("az_scale", s["az_scale"])
        b = new["theta0_deg"]
        pan_rms = math.sqrt(sum((a * x + b - y) ** 2 for x, y in zip(az, pt)) / n)
        faces = [p for p in sm if p["kind"] == "face"]
        tilt_rms = None
        if faces:
            rs = [p["track"]["r"] for p in faces]
            tt = [p["tilt_true"] for p in faces]

            def fit_dh(phi0):
                dh = sum(r * math.tan(math.radians(t - phi0)) for r, t in zip(rs, tt)) / len(rs)
                err = sum((math.degrees(math.atan2(dh, r)) + phi0 - t) ** 2 for r, t in zip(rs, tt))
                return dh, err
            if len(faces) >= 2 and max(rs) - min(rs) >= 0.5:
                phi0 = min((x * 0.25 for x in range(-80, 81)), key=lambda p: fit_dh(p)[1])
            else:
                phi0 = s["phi0_deg"]
                notes.append("人脸点的远近差不到 0.5 m，俯仰只拟合高度差")
            dh, err = fit_dh(phi0)
            ht = s["sensor_height_m"] + dh
            if 0.1 <= ht <= 2.5:
                new.update(phi0_deg=round(phi0, 2), target_height_m=round(ht, 2))
            else:
                new.update(phi0_deg=round(phi0, 2), sensor_height_m=round(max(0.1, min(3.0, 1.3 - dh)), 2),
                           target_height_m=1.3)
            tilt_rms = math.sqrt(err / len(faces))
        else:
            notes.append("没有人脸点，俯仰不变（挥手点只用来标水平）")
        msg = (f"{n} 个点：水平残差 {pan_rms:.1f}°" + (f"，俯仰残差 {tilt_rms:.1f}°" if tilt_rms is not None else "")
               + "；新参数 " + ", ".join(f"{k}={v}" for k, v in new.items()) + ("；" + "；".join(notes) if notes else ""))
        if apply:
            self.settings.update(new)
            self.event("标定完成", msg)
        return {"ok": True, "msg": msg, "new": new, "pan_rms": round(pan_rms, 2),
                "tilt_rms": None if tilt_rms is None else round(tilt_rms, 2), "applied": apply}

    def _grab_gray(self, after_t, timeout=3.0):
        cv2, np = vision.cv2, vision.np
        t_end = time.monotonic() + timeout
        while time.monotonic() < t_end:
            if self.camera.frame_t > after_t and self.camera.frame:
                img = cv2.imdecode(np.frombuffer(self.camera.frame, np.uint8), cv2.IMREAD_REDUCED_GRAYSCALE_4)
                if img is not None:
                    return cv2.resize(img, (320, 180)).astype(np.float32)
            time.sleep(0.05)
        raise RuntimeError("摄像头没有新画面")

    def selftest_job(self):
        """Direction self-test: step pan +8° and tilt +8°, measure the image shift with phase
        correlation. Camera turning right -> scene moves left (dx < 0); camera tilting up -> scene
        moves down (dy > 0). Wrong direction -> flip pan_sign / tilt_sign. Shift size -> FOV."""
        c = self.calib
        cv2 = vision.cv2
        step = 8.0
        try:
            if not vision.available() or not self.camera.ok:
                raise RuntimeError("摄像头/OpenCV 不可用")
            win = cv2.createHanningWindow((320, 180), cv2.CV_32F)

            def measure(pose):
                c["pose"] = [0.0, 0.0]
                time.sleep(1.5)
                a = self._grab_gray(time.monotonic() + 0.3)
                c["pose"] = pose
                time.sleep(1.5)
                b = self._grab_gray(time.monotonic() + 0.3)
                (dx, dy), resp = cv2.phaseCorrelate(a, b, win)
                return dx, dy, resp
            pdx, pdy, presp = measure([step, 0.0])
            tdx, tdy, tresp = measure([0.0, step])
            c["pose"] = [0.0, 0.0]
            s = self.settings.get()
            new, notes = {}, []
            if abs(pdx) >= 3 and presp > 0.03:
                if pdx > 0:
                    new["pan_sign"] = -s["pan_sign"]
                f = abs(pdx) / math.tan(math.radians(step))
                hfov = math.degrees(2 * math.atan(160 / f))
                if 40 <= hfov <= 150:
                    new["cam_hfov_deg"] = round(hfov, 1)
                notes.append(f"水平: 画面移动 {pdx:+.1f} px（{'正确' if pdx < 0 else '反了→已改 pan_sign'}），估计水平视场 {hfov:.0f}°")
            else:
                notes.append(f"水平: 画面移动太小/不可信（{pdx:+.1f} px, 置信 {presp:.2f}），没改")
            if abs(tdy) >= 3 and tresp > 0.03:
                if tdy < 0:
                    new["tilt_sign"] = -s["tilt_sign"]
                f = abs(tdy) / math.tan(math.radians(step))
                vfov = math.degrees(2 * math.atan(90 / f))
                if 25 <= vfov <= 120:
                    new["cam_vfov_deg"] = round(vfov, 1)
                notes.append(f"俯仰: 画面移动 {tdy:+.1f} px（{'正确' if tdy > 0 else '反了→已改 tilt_sign'}），估计垂直视场 {vfov:.0f}°")
            else:
                notes.append(f"俯仰: 画面移动太小/不可信（{tdy:+.1f} px, 置信 {tresp:.2f}），没改")
            if new:
                self.settings.update(new)
            msg = "方向自检完成：" + "；".join(notes) + (f"；写入 {new}" if new else "；参数不用改")
            c["result"] = {"ok": True, "msg": msg, "new": new, "pan_shift": [round(pdx, 1), round(pdy, 1), round(presp, 2)],
                           "tilt_shift": [round(tdx, 1), round(tdy, 1), round(tresp, 2)]}
            c["msg"] = msg
            self.event("方向自检", msg)
        except Exception as exc:
            c["msg"] = f"方向自检失败: {exc}"
            self.event("方向自检", c["msg"])
        finally:
            c["pose"] = [0.0, 0.0]
            time.sleep(0.8)
            c.update(busy=False, mode="off")

    # ------------------------------------------------------------------ commands
    def command(self, action):
        now = time.monotonic()
        with self.lock:
            if action in ("arm", "disarm", "toggle_arm"):
                self.armed = (not self.armed) if action == "toggle_arm" else (action == "arm")
                if not self.armed:
                    self.alarm_until = 0.0
                self.event("布防" if self.armed else "撤防", "", mqtt_type="arm", extra={"armed": self.armed})
                self.update_state(now, self.settings.get())
            elif action == "center":
                self.center_until = now + 3.0
                self.vt.update(active=False, goal=None, track=None, seen_n=0)
                self.event("归中", "CH4/CH8 -> 1500 µs(+微调)，保持 3 s")
            elif action == "pause":
                self.paused = True
                self.event("暂停跟踪", "云台保持当前位置")
            elif action == "resume":
                self.paused = False
                self.manual = None
                self.event("恢复跟踪", "")
            elif action == "snapshot":
                self.wake_camera("snapshot", now)
                self.red_blink_until = now + 0.3
                self.force_detect_until = now + 3.0
            elif action == "clear_alarm":
                self.alarm_until = 0.0
                self.update_state(now, self.settings.get())
            elif action == "identify":
                self.force_detect_until = now + 3.0
                self.wake_camera("identify", now)
            elif action == "wake_camera":
                self.wake_camera("web", now)
            elif action == "sleep_camera":
                self.last_activity_t = -1e9
            elif action == "wifi_setup":
                threading.Thread(target=self.start_wifi_setup, args=("web",), daemon=True).start()
            elif action == "reset_identity":
                self.ident.clear()
                for tr in self.tracker.tracks:
                    tr.kind = "verify"
                self.clutter.clear()
                self.event("身份", "清空比对结果和“非人目标”记录")
            else:
                raise ValueError(f"未知命令 {action}")
        if action == "snapshot":
            s = self.settings.get()
            return self.snapshot("manual", upload=("抓拍上报", "K4/网页抓拍", "snapshot"),
                                 extra=self.track_summary(self.tracker.primary, s))
        if action == "identify":
            return self.identify_now()
        return True

    def servo_manual(self, data):
        """T2 test / centre trim: pause tracking and step CH4/CH8 by whole 12-bit counts."""
        if self.pca is None:
            raise RuntimeError("PCA9685 不可用")
        with self.lock:
            if data.get("action") == "exit":
                if self.manual:
                    # continue from where the servos physically are, so the rate limiter walks back smoothly
                    st = self.settings.get()
                    g = self.guide
                    for ch, key, off, sg in ((C.PAN_CH, "pan", "pan_offset_us", "pan_sign"),
                                             (C.TILT_CH, "tilt", "tilt_offset_us", "tilt_sign")):
                        us = pointing.ticks_to_us(self.manual[str(ch)])
                        a = (C.SERVO_CENTER_US + st[off] - us) / (st[sg] * C.US_PER_DEG)
                        a = pointing.limit_pan(a) if key == "pan" else pointing.limit_tilt(a)
                        g[f"{key}_cmd"] = g[f"{key}_f"] = g[f"{key}_goal"] = a
                self.manual = None
                self.paused = getattr(self, "paused_before_manual", False)   # resume unless it was paused before
                self.event("舵机手动", "退出手动" + ("（保持暂停）" if self.paused else "，恢复跟踪"))
                return None
            if self.manual is None:
                self.manual = {str(C.PAN_CH): self.pca.ticks.get(C.PAN_CH, 307),
                               str(C.TILT_CH): self.pca.ticks.get(C.TILT_CH, 307)}
                self.paused_before_manual = self.paused
                self.paused = True
            ch = str(int(data.get("ch", 4)))
            if ch not in self.manual:
                raise ValueError("只能是 CH4 或 CH8")
            if data.get("action") == "center":
                self.manual[ch] = pointing.pulse_to_ticks(1500)
            elif data.get("action") == "pulse":          # absolute pulse (range test)
                self.manual[ch] = pointing.pulse_to_ticks(float(data["us"]))
            else:
                self.manual[ch] += int(data.get("ticks", 1))
            lo, hi = (pointing.pulse_to_ticks(v) for v in pointing.channel_limits_us(int(ch)))
            self.manual[ch] = max(lo, min(hi, self.manual[ch]))
            us = pointing.ticks_to_us(self.manual[ch])
            if not data.get("quiet"):
                self.event("舵机手动", f"CH{ch} = {self.manual[ch]} 计数 = {us:.1f} µs")
            out = dict(self.manual)
        time.sleep(0.06)                                   # guidance loop (50 Hz) writes it, then read back
        try:
            out["readback"] = self.pca.readback(int(ch))
        except Exception as exc:
            out["readback"] = {"error": str(exc)}
        return out

    # ------------------------------------------------------------------ camera power saving
    def wake_camera(self, why, now=None):
        now = now or time.monotonic()
        self.last_activity_t = now
        if self.camera.sleeping.is_set():
            self.camera.wake()
            self.wake_req_t = now
            self.wake_detect_log.append({"t": datetime.now().strftime("%H:%M:%S"), "why": why, "detect_ms": None})
            self.event("摄像头", f"唤醒（{why}）")

    def camera_needed(self, now, s):
        """Return the reason the camera must stay awake, or '' if it may sleep."""
        for why, c in (("disarmed", not self.armed), ("radar_track", bool(self.tracker.tracks)),
                       ("vision_target", self.vt["active"]), ("enroll", self.enroll["running"]),
                       ("calib", self.calib["mode"] != "off"), ("manual", self.manual),
                       ("detect_request", now < self.force_detect_until),
                       ("patrol", self.guide["mode"] == "patrol" and s["patrol_while_sleep"]),
                       ("wifi_setup", self.wifi_setup_active())):
            if c:
                return why
        return ""

    def power_loop(self):
        while self.running:
            time.sleep(0.2)
            s = self.settings.get()
            now = time.monotonic()
            with self.lock:
                need = self.camera_needed(now, s)
                self.camera_need_reason = need
                if need:
                    self.last_activity_t = now
                    if self.camera.sleeping.is_set():
                        self.wake_camera("需要", now)
                elif (s["camera_sleep_s"] > 0 and not self.camera.sleeping.is_set()
                      and now - self.last_activity_t > s["camera_sleep_s"]):
                    self.camera.sleep()
                    self.event("摄像头", f"休眠：{s['camera_sleep_s']:.0f} s 无目标（只靠雷达，有目标立即唤醒）")
            if self.wake_detect_log and self.wake_detect_log[-1].get("frame_ms") is None and self.camera.wake_log:
                self.wake_detect_log[-1]["frame_ms"] = self.camera.wake_log[-1]["first_frame_ms"]
            if self.wifi_setup_active():
                self.wifi_qr_scan(now)

    # ------------------------------------------------------------------ Wi-Fi setup (AP fallback portal)
    WIFI_STATUS = Path("/run/etec306-wifi/status.json")

    def wifi_status(self):
        try:
            st = json.loads(self.WIFI_STATUS.read_text())
        except Exception:
            return {"active": False}
        return st

    def wifi_setup_active(self):
        return bool(self.wifi_status().get("active"))

    def start_wifi_setup(self, why):
        r = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "start", "--no-block", "etec306-wifi-setup.service"],
                           capture_output=True, text=True, timeout=10)
        self.event("Wi-Fi", f"进入配网模式（{why}）" + ("" if r.returncode == 0 else f" 失败: {r.stderr.strip()[:120]}"))
        return r.returncode == 0

    def wifi_qr_scan(self, now):
        """Setup mode: look for a Wi-Fi QR (WIFI:T:WPA;S:ssid;P:pass;;) shown to the camera; 2 scans/s."""
        if now - self.wifi_qr_t < 0.5 or not self.camera.frame:
            return
        self.wifi_qr_t = now
        try:
            import cv2
            import numpy as np
            if self.qr_det is None:
                self.qr_det = cv2.QRCodeDetector()
            img = cv2.imdecode(np.frombuffer(self.camera.frame, np.uint8), cv2.IMREAD_GRAYSCALE)
            txt, _, _ = self.qr_det.detectAndDecode(img)
        except Exception:
            return
        if not txt or not txt.startswith("WIFI:"):
            return
        f = parse_wifi_qr(txt)
        if not f.get("S"):
            return
        import urllib.request
        try:
            req = urllib.request.Request("http://127.0.0.1:80/api/connect", method="POST",
                                         data=json.dumps({"ssid": f["S"], "password": f.get("P", ""), "src": "camera-qr"}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
            self.event("Wi-Fi", f"摄像头读到 Wi-Fi 二维码：{f['S']}（密码不记录），正在连接")
            self.wifi_qr_t = now + 30          # don't resubmit while it switches
        except Exception as exc:
            self.event("Wi-Fi", f"二维码已读到，但提交失败: {exc}")

    def on_button(self, name):
        self.event("按键", {"K1": "K1 布防/撤防", "K2": "K2 翻页", "K3": "K3 归中", "K4": "K4 抓拍"}[name])
        if name == "K1":
            self.command("toggle_arm")
        elif name == "K2":
            self.oled_page = (self.oled_page + 1) % 4
        elif name == "K3":
            self.command("center")
        elif name == "K4":
            threading.Thread(target=self.command, args=("snapshot",), daemon=True).start()

    # ------------------------------------------------------------------ patrol (step and stare)
    def patrol_goal(self, now, s):
        pt = self.patrol
        rng, step = s["patrol_range_deg"], s["patrol_step_deg"]
        n = max(1, int(round(2 * rng / step)))
        pos = [-rng + 2 * rng * i / n for i in range(n + 1)]
        g = self.guide
        if not pt["active"]:
            pt.update(active=True, idx=min(range(len(pos)), key=lambda i: abs(pos[i] - g["pan_cmd"])), arrived=None)
            self.event("巡逻", f"{s['patrol_idle_s']:.0f} s 没有目标，开始巡逻 ±{rng:.0f}°，每 {step:.0f}° 停 {s['patrol_dwell_s']:.1f} s")
        pt["idx"] = max(0, min(len(pos) - 1, pt["idx"]))
        goal = (pos[pt["idx"]], s["patrol_tilt_deg"])
        if abs(g["pan_cmd"] - goal[0]) < 1.0 and abs(g["tilt_cmd"] - goal[1]) < 1.0:
            if pt["arrived"] is None:
                pt["arrived"] = now
            elif now - pt["arrived"] >= s["patrol_dwell_s"]:
                if not 0 <= pt["idx"] + pt["dir"] < len(pos):
                    pt["dir"] = -pt["dir"]
                pt["idx"] += pt["dir"]
                pt["arrived"] = None
        return pointing.limit_pan(goal[0]), pointing.limit_tilt(goal[1])

    # ------------------------------------------------------------------ guidance 50 Hz
    def guidance_loop(self):
        dt = 1.0 / C.GUIDE_HZ
        g = self.guide
        next_t = time.monotonic()
        last_check = 0.0
        while self.running:
            next_t += dt
            now = time.monotonic()
            s = self.settings.get()
            with self.lock:
                self.update_state(now, s)
                cmd = self.primary_command(s)
                p = self.tracker.primary
                due = [x for x in self.pending_snaps if x[0] <= now]
                self.pending_snaps = [x for x in self.pending_snaps if x[0] > now]
                manual = dict(self.manual) if self.manual else None
                if manual:
                    mode, desired = "manual", None
                elif now < self.center_until:
                    mode, desired = "center", (0.0, 0.0)
                elif self.calib["mode"] != "off":
                    mode, desired = "calib", tuple(self.calib["pose"])
                elif not self.armed:
                    mode, desired = "disarmed-hold", None
                elif self.paused:
                    mode, desired = "paused", None
                elif self.vt["active"] and self.vt["goal"] is not None:
                    mode, desired = "vision", self.vt["goal"]
                elif cmd is not None and p.kind != "object":
                    mode, desired = self.radar_goal(now, p, cmd, s)
                elif (s["patrol_enabled"] and now - self.last_target_t >= s["patrol_idle_s"] and self.last_target_t >= 0
                      and (not self.camera.sleeping.is_set() or s["patrol_while_sleep"])):
                    mode, desired = "patrol", self.patrol_goal(now, s)
                elif now - self.last_target_t >= s["return_center_s"]:
                    mode, desired = "return-center", (0.0, 0.0)
                else:
                    mode, desired = "hold", None
                if mode != "search" and self.search["active"]:
                    self.search["active"] = False
                if mode != "patrol" and self.patrol["active"]:
                    self.patrol["active"] = False
                    if mode in ("vision", "radar", "search"):
                        self.event("巡逻", f"发现目标，停止巡逻（{MODE_CN[mode]}）")
                frame_t = self.last_frame_t
            for _, reason, upload, extra in due:
                threading.Thread(target=self.snapshot, args=(reason, upload, extra, upload is None), daemon=True).start()
            if desired is not None:
                dp, dtl = desired
                db = s["deadband_deg"] if mode == "radar" else 0.0
                if abs(dp - g["pan_goal"]) > db:
                    g["pan_goal"] = dp
                if abs(dtl - g["tilt_goal"]) > db:
                    g["tilt_goal"] = dtl
            g["desired_pan"], g["desired_tilt"] = (desired if desired else (None, None))
            k = 1.0 - math.exp(-2 * math.pi * s["lowpass_hz"] * dt)
            if mode == "vision":
                k = max(k, 1.0 - math.exp(-2 * math.pi * 5.0 * dt))     # vision goal is already smoothed
            g["pan_f"] += k * (g["pan_goal"] - g["pan_f"])
            g["tilt_f"] += k * (g["tilt_goal"] - g["tilt_f"])
            rate = s["max_rate_dps"] * (0.5 if mode == "patrol" else 1.0)
            step = rate * dt
            g["pan_cmd"] = pointing.limit_pan(g["pan_cmd"] + pointing.clamp(g["pan_f"] - g["pan_cmd"], -step, step))
            g["tilt_cmd"] = pointing.limit_tilt(g["tilt_cmd"] + pointing.clamp(g["tilt_f"] - g["tilt_cmd"], -step, step))
            g["mode"] = mode
            self.cmd_hist.append((now, g["pan_cmd"], g["tilt_cmd"]))
            try:
                if self.pca is not None:
                    if now - last_check > 2.0:
                        last_check = now
                        if self.pca.check_awake():
                            self.event("PCA9685", "芯片处于睡眠，已重新初始化")
                    before = dict(self.pca.ticks)
                    if manual:
                        g["pan_us"] = self.pca.write_us(C.PAN_CH, pointing.ticks_to_us(manual[str(C.PAN_CH)]))
                        g["tilt_us"] = self.pca.write_us(C.TILT_CH, pointing.ticks_to_us(manual[str(C.TILT_CH)]))
                    else:
                        g["pan_us"], g["tilt_us"] = self.write_servos(g["pan_cmd"], g["tilt_cmd"])
                    if self.pca.ticks != before:
                        g["writes"] += 1
                        if mode == "radar" and frame_t:
                            self.latency_ms.append((time.monotonic() - frame_t) * 1000)
                    g["error"] = ""
            except Exception as exc:
                g["error"] = str(exc)
            time.sleep(max(0.0, next_t - time.monotonic()))
            if time.monotonic() - next_t > 0.5:
                next_t = time.monotonic()

    def known_locked(self):
        key = self.target_key()
        info = self.ident.get(key, {}) if key else {}
        if info.get("status") == "known":
            self.known_banner = (info.get("name") or "", time.monotonic() + 3)   # OLED keeps "Hi name"
            return True
        return False

    # ------------------------------------------------------------------ HMI 5 Hz
    def hmi_loop(self):
        n = 0
        while self.running:
            n += 1
            now = time.monotonic()
            st = self.state
            if self.gpio and self.gpio.pressed.get("K2") and self.gpio.pressed.get("K3"):
                if self.wifi_combo_since is None:
                    self.wifi_combo_since = now
                elif now - self.wifi_combo_since >= 5.0:
                    self.wifi_combo_since = 1e18        # once per hold
                    threading.Thread(target=self.start_wifi_setup, args=("K2+K3 长按 5 s",), daemon=True).start()
            else:
                self.wifi_combo_since = None
            if self.gpio:
                try:
                    if now < self.known_blink_until:      # 7.5: known person -> green blinks once
                        self.gpio.led("green", int((self.known_blink_until - now) / 0.4) % 2 == 0)
                        self.gpio.led("yellow", False)
                    elif st == "KNOWN" and self.known_locked():
                        # a recognised household member is locked: green blinks slowly (1 Hz), yellow off
                        self.gpio.led("green", n % 5 < 3)
                        self.gpio.led("yellow", False)
                    else:
                        self.gpio.led("green", st == "ARMED")
                        self.gpio.led("yellow", st == "LOCKED" or (st == "VERIFY" and n % 4 < 2))
                    self.gpio.led("red", st == "ALARM" or now < self.red_blink_until)
                except Exception as exc:
                    self.hw_error["gpio"] = str(exc)
            if self.oled and n % 2 == 0:
                try:
                    self.oled.show([l.encode("ascii", "replace").decode() for l in self.oled_lines()])
                except Exception as exc:
                    self.hw_error["oled"] = str(exc)
            if n % 150 == 0:
                self.ip = ip_address()
            time.sleep(0.2)

    def oled_lines(self):
        w = self.wifi_status()
        if w.get("active"):
            return [f"SETUP {w.get('ssid', '')}"[:21], f"pw {w.get('password', '')} {w.get('ip', '')}"[:21]]
        if w.get("result_ip") and time.time() - w.get("result_t", 0) < 60:
            return [f"WiFi {w.get('result_ssid', '')}"[:21], f"IP {w.get('result_ip')}"[:21]]
        up = int(time.monotonic() - self.t0)
        g = self.guide
        with self.lock:
            p = self.tracker.primary
            s = self.settings.get()
            cmd = self.primary_command(s) if p else None
            ntr = len(self.tracker.tracks)
        page = self.oled_page
        name, until = self.known_banner
        if page == 0:
            second = f"Hi {name}" if name and time.monotonic() < until else \
                f"{self.state[:6]} up{up // 3600}:{up // 60 % 60:02d}"
            return [f"{self.ip}", second]
        if page == 1:
            m = self.mqtt.status()
            if not m["enabled"]:
                return ["MQTT: off", (m["error"] or "")[:21]]
            return [f"MQTT {'ONLINE' if m['connected'] else 'OFFLINE'}", f"hb {m['last_heartbeat'] or '-'}"]
        if page == 2:
            if p is None:
                return ["no target", f"tracks {ntr}"]
            return [f"X{s['x_sign'] * p.x / 1000:+.2f} Y{p.y / 1000:.2f}m", f"v{p.speed:+d} th{cmd.azimuth_deg:+.1f}"]
        return [f"P{g['pan_cmd']:+.1f} T{g['tilt_cmd']:+.1f}", f"{g['mode'][:10]} {self.vt['kind'][:5]}"]

    # ------------------------------------------------------------------ MQTT heartbeat / track
    def mqtt_loop(self):
        last_hb, last_track = 0.0, 0.0
        while self.running:
            time.sleep(0.1)
            if not self.mqtt.enabled:
                continue
            now = time.monotonic()
            s = self.settings.get()
            if now - last_hb >= C.HEARTBEAT_S:
                last_hb = now
                g = self.guide
                ok = self.mqtt.publish("status", {
                    "online": True, "ts": datetime.now().isoformat(timespec="seconds"), "ip": self.ip,
                    "armed": self.armed, "state": self.state, "mode": g["mode"], "pan_deg": round(g["pan_cmd"], 1),
                    "tilt_deg": round(g["tilt_cmd"], 1), "uptime_s": int(now - self.t0),
                    "cpu_temp_c": round(self.cpu_temp(), 1), "radar_fps": round(self.radar_fps(), 1)},
                    qos=1, retain=True)
                if ok:
                    self.mqtt.last_heartbeat = datetime.now().strftime("%H:%M:%S")
            hz = s["mqtt_track_hz"]
            if hz > 0 and now - last_track >= 1.0 / hz:
                with self.lock:
                    p = self.tracker.primary
                    summ = self.track_summary(p, s) if p is not None and p.kind != "object" else None
                    if summ:
                        summ["source"] = "vision" if self.vt["active"] else "radar"
                if summ:
                    last_track = now
                    self.mqtt.publish("track", {"ts": datetime.now().isoformat(timespec="milliseconds"), **summ})

    # ------------------------------------------------------------------ journal log 1 Hz
    def log_loop(self):
        n = 0
        while self.running:
            time.sleep(1.0)
            n += 1
            g = self.guide
            with self.lock:
                p = self.tracker.primary
                s = self.settings.get()
                cmd = self.primary_command(s) if p else None
                ntr = len(self.tracker.tracks)
                fps = self.radar_fps()
                vr = self.vision_res
                key = self.target_key()
                idi = dict(self.ident.get(key, {})) if key else {}
            vis = ""
            if vr and time.monotonic() - vr["t"] < 1.5:
                tg = vr["target"]
                vis = (f" vis faces={len(vr['faces'])} persons={len(vr['persons'])} hands={len(vr['hands'])}"
                       + (f" {tg['kind']} eu={tg['eu_px']:+.0f}px ev={tg['ev_px']:+.0f}px" if tg else ""))
            objs = ",".join(o["label"] for o in self.objects_result(1.5))
            base = (f"state={self.state} mode={g['mode']} radar={fps:.1f}fps cam={self.camera.fps:.1f}fps "
                    f"tracks={ntr} cmd pan={g['pan_cmd']:+.1f} tilt={g['tilt_cmd']:+.1f} "
                    f"CH4={g['pan_us']}us CH8={g['tilt_us']}us T={self.cpu_temp():.0f}C")
            if p is not None or self.vt["active"]:
                rp = (f"T{p.id}({p.kind}) X={s['x_sign'] * p.x:+.0f} Y={p.y:.0f}mm r={p.r_m:.2f}m v={p.speed:+d}cm/s "
                      f"θ={cmd.azimuth_deg:+.1f}" if p is not None else "no-radar")
                print(f"[track] {rp} key={key} id={idi.get('status', '-')}{(':' + idi['name']) if idi.get('name') else ''}"
                      f"{vis} objs=[{objs}] | {base}", flush=True)
            elif n % 10 == 0:
                print(f"[idle] {base} objs=[{objs}] radar_err={self.radar.error!r} cam_err={self.camera.error!r} "
                      f"mqtt={'on' if self.mqtt.connected else 'off'}", flush=True)

    def radar_fps(self):
        t = self.radar_times
        if len(t) < 2 or time.monotonic() - t[-1] > 1.0:
            return 0.0
        return (len(t) - 1) / (t[-1] - t[0])

    # ------------------------------------------------------------------ status
    def status(self, full=True):
        now = time.monotonic()
        s = self.settings.get()
        xs = s["x_sign"]
        g = dict(self.guide)
        with self.lock:
            f = self.last_frame
            targets = []
            if f is not None and full:
                for t in f.targets:
                    c = pointing.command_for_target(t.x_mm, t.y_mm, s)
                    targets.append({"slot": t.index, "x_mm": t.x_mm, "xr_mm": xs * t.x_mm, "y_mm": t.y_mm,
                                    "speed_cms": t.speed_cms, "res_mm": t.res_mm, "dist_mm": round(t.dist_mm),
                                    "valid": t.valid, "az_deg": round(c.azimuth_deg, 1)})
            tracks = []
            for t in self.tracker.tracks:
                d = t.as_dict(xs)
                d["proj"] = self.project_track(t, s, g["pan_cmd"], g["tilt_cmd"])
                tracks.append(d)
            search = {k: v for k, v in self.search.items() if k not in ("aim_t", "step_t")}
            search["n"] = len(self.search_pattern(s))
            assoc = list(self.assoc_log)
            errs = [a["err_deg"] for a in assoc]
            assoc_sum = {"n": len(errs), "mean": round(statistics.mean(errs), 2) if errs else None,
                         "median_abs": round(statistics.median([abs(e) for e in errs]), 2) if errs else None,
                         "last": assoc[-5:]}
            scores = list(self.score_log)[-10:]
            p = self.tracker.primary
            key = self.target_key()
            prim = None
            if p is not None:
                c = pointing.command_for_target(p.x, p.y, s)
                prim = {**p.as_dict(xs), "az_deg": round(c.azimuth_deg, 1), "pan_deg": round(c.pan_deg, 1),
                        "tilt_deg": round(c.tilt_deg, 1), "pan_us": c.pan_us, "tilt_us": c.tilt_us,
                        "pan_limited": c.pan_limited, "tilt_limited": c.tilt_limited}
            ident = {k: v for k, v in self.ident.get(key, {}).items() if k not in ("last_try", "wait_since")} if key else {}
            events = list(self.events)[:80 if full else 30]
            radar_info = {"open": self.radar.is_open, "error": self.radar.error, "fps": round(self.radar_fps(), 1),
                          "frames": self.frames, "dropped_bytes": self.radar.parser.dropped,
                          "age_ms": int((now - self.last_frame_t) * 1000) if self.last_frame_t else None,
                          "raw": f.raw.hex(" ") if (f and full) else "", "targets": targets}
            vr = self.vision_res
            vt = {k: v for k, v in self.vt.items() if k != "track"}
            vt["track_id"] = self.vt["track"].id if self.vt["track"] is not None else None
            calib = {k: v for k, v in self.calib.items()}
            clutter = [{"x_mm": round(xs * c[0]), "y_mm": round(c[1]), "left_s": round(c[2] - now), "label": c[3]}
                       for c in self.clutter]
        for k in ("pan_goal", "tilt_goal", "pan_f", "tilt_f", "pan_cmd", "tilt_cmd", "desired_pan", "desired_tilt"):
            if g[k] is not None:
                g[k] = round(g[k], 2)
        g["mode_cn"] = MODE_CN.get(g["mode"], g["mode"])
        lat = list(self.latency_ms)
        vis = {"available": self.vision is not None, "enabled": s["vision_enabled"], "active": self.vision_active(),
               "rate": round(self.vision.rate, 1) if self.vision else 0, "fresh": False}
        if vr and now - vr["t"] < 2.0:
            vis.update(fresh=now - vr["t"] < 1.0, age_ms=int((now - vr["t"]) * 1000), ms=vr["ms"],
                       faces=vr["faces"], persons=vr["persons"], hands=vr["hands"], target=vr["target"],
                       motion=vr.get("motion", []))
        ob = {"available": self.objects is not None, "items": self.objects_result(1.5),
              "ms": self.objects.result["ms"] if self.objects else None,
              "hand_ms": self.objects.result.get("hand_ms") if self.objects else None,
              "rate": round(self.objects.rate, 2) if self.objects else 0}
        out = {
            "time": datetime.now().strftime("%H:%M:%S"), "uptime_s": int(now - self.t0), "ip": self.ip,
            "state": self.state,
            "state_cn": STATE_CN[self.state] + ("：" + str(ident.get("name")) if self.state == "KNOWN" and ident.get("name") else ""), "armed": self.armed, "paused": self.paused,
            "alarm_left_s": round(max(0.0, self.alarm_until - now), 1), "radar": radar_info, "tracks": tracks,
            "primary": prim, "key": key, "identity": ident, "guide": g, "manual": self.manual, "vt": vt,
            "patrol": dict(self.patrol), "calib": calib, "clutter": clutter, "search": search, "assoc": assoc_sum,
            "needs_calib": self.assoc_warn["n"] >= 3, "assoc_err": self.assoc_warn["last_err"],
            "id_scores": scores,
            "latency_ms": {"n": len(lat), "median": round(statistics.median(lat), 1) if lat else None,
                           "max": round(max(lat), 1) if lat else None},
            "camera": {"ok": self.camera.ok, "fps": round(self.camera.fps, 1), "error": self.camera.error,
                       "sleeping": self.camera.sleeping.is_set(), "awake_reason": getattr(self, "camera_need_reason", ""), "idle_s": round(time.monotonic() - self.last_activity_t, 1),
                       "size": f"{self.camera.w}x{self.camera.h}", "still": self.camera.last_still},
            "vision": vis, "objects": ob, "enroll": self.enroll,
            "gallery": self.gallery.listing() if self.gallery else [],
            "known_banner": self.known_banner[0] if now < self.known_banner[1] else "",
            "mqtt": self.mqtt.status(),
            "metrics": getattr(self, "metrics", None) and self.metrics.last, "wifi_setup": self.wifi_setup_active(),
            "sys": {"cpu_temp_c": round(self.cpu_temp(), 1), "load": open("/proc/loadavg").read().split()[:3]
                    if Path("/proc/loadavg").exists() else []},
            "hw": {"i2c": [f"0x{a:02x}" for a in self.i2c_found], "pca9685": self.pca is not None,
                   "oled": self.oled is not None, "gpio": self.gpio is not None, "errors": self.hw_error,
                   "leds": self.gpio.led_state if self.gpio else {}, "buttons": self.gpio.pressed if self.gpio else {},
                   "oled_page": self.oled_page, "pca_inits": self.pca.inits if self.pca else 0},
            "limits": {"pan_deg": C.PAN_LIMIT_DEG, "tilt_deg": C.TILT_LIMIT_DEG},
            "settings": s, "events": events, "snapshots": self.snaps.recent(12 if full else 6),
            "https_port": HTTPS_PORT if TLS_DIR.joinpath("cert.pem").exists() else None,
        }
        return out

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        self.radar.start()
        self.camera.start()
        if self.vision:
            self.vision.start()
        if self.objects:
            self.objects.start()
        self.mqtt.start()
        for fn in (self.guidance_loop, self.hmi_loop, self.log_loop, self.identity_loop, self.mqtt_loop,
                   self.power_loop):
            threading.Thread(target=fn, daemon=True, name=fn.__name__).start()
        self.metrics = metrics.Metrics(self)

    def shutdown(self):
        self.running = False
        self.radar.stop()
        self.camera.stop()
        time.sleep(0.1)
        try:
            self.write_servos(0.0, 0.0)       # back to centre (1500 us + trim); chip keeps running
        except Exception as exc:
            print("centre on shutdown failed:", exc)
        if self.gpio:
            self.gpio.close()
        if self.oled:
            try:
                self.oled.show(["ETEC306 stopped", "servos centred"])
            except Exception:
                pass
        self.event("系统停止", "舵机已归中")
        self.mqtt.stop({"online": False, "ts": datetime.now().isoformat(timespec="seconds"), "reason": "shutdown"})


def consistency(feats):
    np = vision.np
    m = sum(feats) / len(feats)
    m = m / (np.linalg.norm(m) + 1e-9)
    return min(float(np.dot(f, m)) for f in feats)


APP: App | None = None
HTTPS_PORT = 8443
STREAMS, STREAM_LOCK, MAX_STREAMS = [], threading.Lock(), 3
FILE_RE = re.compile(r"^[\w.\- ]+\.jpg$")
PAGES = {"/": "index.html", "/index.html": "index.html", "/settings.html": "settings.html", "/enroll.html": "enroll.html",
         "/common.js": "common.js", "/i18n.js": "i18n.js", "/wa_qr.html": "wa_qr.html"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, payload):
        self._send(code, json.dumps(payload, ensure_ascii=False, default=json_default).encode(),
                   "application/json; charset=utf-8")

    def _file(self, folder, name):
        f = ROOT / folder / name
        if not FILE_RE.match(name) or not f.is_file():
            return self._json(404, {"ok": False, "error": "not found"})
        self._send(200, f.read_bytes(), "image/jpeg")

    def do_GET(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        try:
            if path in PAGES:
                ctype = "application/javascript; charset=utf-8" if path.endswith(".js") else "text/html; charset=utf-8"
                self._send(200, (ROOT / PAGES[path]).read_bytes(), ctype)
            elif path == "/api/state":
                self._json(200, APP.status(full="lite" not in u.query))
            elif path == "/api/settings":
                self._json(200, {"settings": APP.settings.get(), "spec": Settings.spec_for_ui()})
            elif path == "/api/notify":
                self._json(200, {"ok": True, "notify": APP.notifier.public()})
            elif path == "/api/metrics":
                m = APP.metrics
                with m.lock:
                    self._json(200, {"ok": True, "metrics": m.last, "history": m.history[-360:] if "hist" in u.query else [],
                                     "model": {k: getattr(metrics, k) for k in dir(metrics) if k.startswith("P_")}})
            elif path == "/api/wifi":
                w = APP.wifi_status()
                w.pop("password", None)            # shown on the OLED only
                self._json(200, {"ok": True, "wifi": w})
            elif path == "/api/wa_status":
                wa = APP.mqtt.wa
                q = wa.get("qr") or {}
                self._json(200, {"ok": True, "status": wa.get("status") or {}, "qr_t": q.get("t"), "has_qr": bool(q.get("png"))})
            elif path == "/api/wa_qr.png":
                import base64
                q = APP.mqtt.wa.get("qr") or {}
                if not q.get("png"):
                    return self._json(404, {"ok": False, "error": "no QR (already paired or bot offline)"})
                self._send(200, base64.b64decode(q["png"]), "image/png")
            elif path == "/api/frame.jpg" and APP.camera.sleeping.is_set():
                self._send(200, sleep_placeholder(), "image/jpeg")
            elif path == "/api/frame.jpg":
                if not APP.camera.frame:
                    return self._json(503, {"ok": False, "error": APP.camera.error or "no frame"})
                self._send(200, APP.camera.frame, "image/jpeg")
            elif path == "/api/stream.mjpg":
                self.stream()
            elif path.startswith("/snapshots/"):
                self._file("snapshots", path.rsplit("/", 1)[-1])
            elif path.startswith("/faces/"):
                self._file(C.FACES_DIR, path.rsplit("/", 1)[-1])
            else:
                self._json(404, {"ok": False, "error": "not found"})
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass
        except Exception as exc:          # a bug in status() etc. used to just drop the connection silently
            log_http_error(self.path, exc)
            try:
                self._json(500, {"ok": False, "error": repr(exc)})
            except Exception:
                pass

    def stream(self):
        me = {"stop": False}
        with STREAM_LOCK:
            STREAMS.append(me)
            while len(STREAMS) > MAX_STREAMS:     # a forgotten tab must not use up the browser's 6 connections
                STREAMS.pop(0)["stop"] = True
        try:
            self._stream(me)
        finally:
            with STREAM_LOCK:
                if me in STREAMS:
                    STREAMS.remove(me)

    def _stream(self, me):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        seq, last_sent = -1, 0.0
        while APP.running and not me["stop"]:
            if APP.camera.sleeping.is_set():
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                 + str(len(sleep_placeholder())).encode() + b"\r\n\r\n" + sleep_placeholder() + b"\r\n")
                time.sleep(2.0)
                continue
            seq, jpg = APP.camera.wait_frame(seq, 1.0)
            if jpg is None:
                continue
            now = time.monotonic()
            if now - last_sent < 1.0 / APP.settings.get()["stream_fps"]:
                continue
            last_sent = now
            self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                             + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 8 * 1024 * 1024:
                raise ValueError("请求太大")
            raw = self.rfile.read(n) if n else b""
            if path == "/api/enroll/upload":          # body = JPEG from the browser camera
                q = parse_qs(u.query)
                sid = q.get("sid", [""])[0]
                if not re.match(r"^[\w-]{6,40}$", sid):
                    raise ValueError("bad sid")
                res = APP.enroll_upload(sid, q.get("name", [""])[0], max(3, min(15, int(q.get("n", ["5"])[0]))), raw)
                return self._json(200, {"ok": True, **res})
            data = json.loads(raw or b"{}") if raw else {}
            if path == "/api/settings":
                self._json(200, {"ok": True, "settings": APP.settings.update(data)})
                APP.event("设置", json.dumps(data, ensure_ascii=False))
            elif path == "/api/settings/reset":
                self._json(200, {"ok": True, "settings": APP.settings.reset()})
                APP.event("设置", "恢复默认")
            elif path == "/api/cmd":
                self._json(200, {"ok": True, "result": APP.command(str(data.get("action", "")))})
            elif path == "/api/servo":
                self._json(200, {"ok": True, "manual": APP.servo_manual(data)})
            elif path == "/api/calib":
                self._json(200, {"ok": True, "calib": APP.calib_action(data)})
            elif path == "/api/enroll":
                if APP.vision is None:
                    raise RuntimeError("视觉不可用")
                if APP.enroll["running"]:
                    raise RuntimeError("正在录入")
                name = str(data.get("name", "")).strip()
                if not vision.NAME_RE.match(name):
                    raise ValueError("名字只能是 1-32 个字母/数字/汉字/空格/-/_")
                k = max(3, min(15, int(data.get("samples", 9))))
                threading.Thread(target=APP.enroll_job, args=(name, k), daemon=True).start()
                self._json(200, {"ok": True})
            elif path == "/api/enroll/finish":
                self._json(200, {"ok": True, "msg": APP.enroll_finish(str(data.get("sid", "")))})
            elif path == "/api/enroll/cancel":
                APP.enroll_sessions.pop(str(data.get("sid", "")), None)
                self._json(200, {"ok": True})
            elif path == "/api/notify":            # save notification settings (secrets never echoed)
                self._json(200, {"ok": True, "notify": APP.notifier.update(data)})
                APP.event("设置", "通知设置已保存")
            elif path == "/api/notify/test":       # one real test message to ONE channel (button)
                ch = str(data.get("channel", ""))
                jpg = APP.camera.frame if data.get("photo") and APP.camera.ok else None
                ok, msg = APP.notifier.test(ch, jpg)
                APP.event("通知测试", f"{notify.CN.get(ch, ch)}: {msg}")
                self._json(200, {"ok": ok, "msg": msg, "error": None if ok else msg})
            elif path == "/api/notify/telegram_chats":
                ok, res = APP.notifier.telegram_chats()
                self._json(200, {"ok": ok, "chats": res if ok else [], "error": None if ok else res})
            elif path == "/api/faces/delete":
                APP.gallery.delete(str(data.get("name", "")))
                APP.event("白名单", f"删除 {data.get('name')}")
                self._json(200, {"ok": True})
            else:
                self._json(404, {"ok": False, "error": "not found"})
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass
        except Exception as exc:
            self._json(400, {"ok": False, "error": str(exc)})


_json_seen = set()


def json_default(o):
    """numpy scalars / arrays (OpenCV results) -> plain Python; anything else -> str. Logs each new type once."""
    t = type(o).__name__
    if t not in _json_seen:
        _json_seen.add(t)
        print(f"[http] json: converting {t}", flush=True)
    if hasattr(o, "item") and getattr(o, "shape", None) == ():
        return o.item()
    if hasattr(o, "tolist"):
        return o.tolist()
    return str(o)


_http_err_t = {}


def log_http_error(path, exc):
    """Print the traceback of an unexpected handler error (at most once a minute per error type)."""
    import traceback
    k = (path.split("?")[0], type(exc).__name__)
    now = time.monotonic()
    if now - _http_err_t.get(k, -1e9) > 60:
        _http_err_t[k] = now
        print(f"[http] error on {path}: {exc!r}\n" + traceback.format_exc(), flush=True)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass                                       # browsers drop streams / reject the self-signed cert


def make_https_server():
    """Same handler over TLS on :8443 (self-signed cert in tls/), needed for getUserMedia on phones."""
    cert, key = TLS_DIR / "cert.pem", TLS_DIR / "key.pem"
    if not (cert.exists() and key.exists()):
        print("no tls/cert.pem -> HTTPS disabled", flush=True)
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    srv = Server(("0.0.0.0", HTTPS_PORT), Handler)
    # handshake happens lazily in the handler thread, so a slow / rejecting client cannot block accept()
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    return srv


def main():
    global APP
    APP = App()
    APP.start()
    server = Server(("0.0.0.0", C.HTTP_PORT), Handler)
    https = None
    try:
        https = make_https_server()
    except Exception as exc:
        print("HTTPS failed:", exc, flush=True)
    if https:
        threading.Thread(target=https.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True).start()

    def stop(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"ETEC306 app http://{APP.ip}:{C.HTTP_PORT}/" + (f"  https://{APP.ip}:{HTTPS_PORT}/" if https else ""), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        APP.shutdown()
        server.server_close()
        if https:
            https.shutdown()


def parse_wifi_qr(txt):
    """WIFI:T:WPA;S:my net;P:pa\\;ss;H:false;;  -> {"T", "S", "P", "H"} (backslash escapes)."""
    out, key, esc, i = {}, None, False, txt[5:]
    buf = ""
    for ch in i:
        if esc:
            buf += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":" and key is None:
            key, buf = buf, ""
        elif ch == ";":
            if key is not None:
                out[key] = buf
            key, buf = None, ""
        else:
            buf += ch
    return out


_PLACEHOLDER = None


def sleep_placeholder():
    """Grey 640x360 JPEG shown while the camera sleeps."""
    global _PLACEHOLDER
    if _PLACEHOLDER is None:
        import cv2
        import numpy as np
        img = np.full((360, 640, 3), 60, np.uint8)
        cv2.putText(img, "Camera: sleeping", (150, 170), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (230, 230, 230), 2)
        cv2.putText(img, "radar only - wakes on a radar track or 'Wake camera'", (60, 215),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (200, 200, 200), 1)
        _PLACEHOLDER = cv2.imencode(".jpg", img)[1].tobytes()
    return _PLACEHOLDER


if __name__ == "__main__":
    main()


