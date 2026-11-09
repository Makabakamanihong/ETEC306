"""Vision (技术方案.md 7.4 / 7.6): YuNet face detection, SFace features for the household whitelist,
motion blobs for calibration. Person / hand / object boxes: objects.py (NanoDet + palm detector).

Detection path: latest 720p MJPEG frame decoded at half size (640x360, IMREAD_REDUCED_COLOR_2),
YuNet only, up to vision_hz. Output = face boxes + pixel error of the chosen face from the image
centre (in 1280x720 pixel units) and the same error in degrees (camera FOV settings). The
guidance thread turns that into a small pan/tilt correction; vision never writes PWM itself.

Identity path: one full-resolution still (2960x1664), YuNet on a 1/4 decode, landmarks scaled
up, alignCrop on the full image, SFace 128-d feature, cosine similarity with the gallery means.
"""
from __future__ import annotations

import math
import re
import threading
import time
from pathlib import Path

import config as C

try:
    import cv2
    import numpy as np
except ImportError:      # the rest of the app still runs without OpenCV
    cv2 = None
    np = None

ROOT = Path(__file__).resolve().parent
NAME_RE = re.compile(r"^[\w\- ]{1,32}$")


def available():
    return cv2 is not None


def face_pose(row):
    """Rough head pose from the 5 YuNet landmarks (re, le, nose, rm, lm).
    yaw: nose offset from the eye midpoint / eye distance. For a head turned by psi the nose tip
    (about 0.7 x half the eye distance in front of the eye plane) moves by d*sin(psi) while the eye
    distance shrinks by cos(psi) -> ratio ~= 0.35*tan(psi) -> psi = atan(ratio / 0.35).
    Sign: + = nose towards image right. pitch: nose height between eye line and mouth line
    (frontal ~0.5..0.6), mapped roughly to degrees. roll: eye line angle."""
    rex, rey, lex, ley, nx, ny, rmx, rmy, lmx, lmy = [float(v) for v in row[4:14]]
    ex, ey = (rex + lex) / 2, (rey + ley) / 2
    eye_d = max(1.0, math.hypot(lex - rex, ley - rey))
    yaw = math.degrees(math.atan2((nx - ex) / eye_d, 0.35))
    my = (rmy + lmy) / 2
    span = max(1.0, my - ey)
    pitch = ((ny - ey) / span - 0.55) * 90.0
    roll = math.degrees(math.atan2(ley - rey, lex - rex))
    if roll > 90:
        roll -= 180
    elif roll < -90:
        roll += 180
    return round(yaw, 1), round(pitch, 1), round(roll, 1)


def face_row_to_dict(f, sx, sy, W, H):
    """YuNet row (x,y,w,h, re(x,y), le(x,y), nose, rm, lm, score) -> normalised dict."""
    x, y, w, h = [float(v) for v in f[:4]]
    lm = [(float(f[4 + 2 * i]) * sx, float(f[5 + 2 * i]) * sy) for i in range(5)]
    yaw, pitch, roll = face_pose(f)
    return {"x": x * sx / W, "y": y * sy / H, "w": w * sx / W, "h": h * sy / H,
            "cx": (x + w / 2) * sx, "cy": (y + h / 2) * sy, "score": round(float(f[14]), 3),
            "eye_px": round(math.dist(lm[0], lm[1]), 1), "lm": [(round(a), round(b)) for a, b in lm],
            "yaw": yaw, "pitch": pitch, "roll": roll}


def frontal_ok(row, max_yaw=25.0) -> bool:
    """Near-frontal check from the landmarks: |yaw| <= max_yaw, eye line roughly level, not looking
    far up/down. Identification only uses faces that pass this (SFace drops a lot on profiles)."""
    yaw, pitch, roll = face_pose(row)
    return abs(yaw) <= max_yaw and abs(roll) <= 25.0 and abs(pitch) <= 35.0


def make_tracker():
    """KCF (fast, opencv-contrib, present on the Pi), else MIL (main opencv). None if neither exists."""
    for name in ("TrackerKCF_create", "TrackerMIL_create"):
        f = getattr(cv2, name, None) or getattr(getattr(cv2, "legacy", None), name, None)
        if f is not None:
            return f()
    for name in ("TrackerKCF", "TrackerMIL"):
        k = getattr(cv2, name, None)
        if k is not None and hasattr(k, "create"):
            return k.create()
    return None


def pose_bin(yaw, frontal=12.0):
    """Enrollment pose bins (image-space yaw: + = nose to image right)."""
    return "front" if abs(yaw) < frontal else ("right" if yaw > 0 else "left")


class Detector:
    def __init__(self):
        mdir = ROOT / C.MODEL_DIR
        self.yunet = cv2.FaceDetectorYN.create(str(mdir / C.YUNET_MODEL), "", (640, 360), 0.7, 0.3, 50)
        self.sface = None
        self.ssd = None
        self.lock = threading.Lock()          # YuNet object is shared by both paths
        if (mdir / C.SFACE_MODEL).exists():
            self.sface = cv2.FaceRecognizerSF.create(str(mdir / C.SFACE_MODEL), "")
        # person boxes now come from NanoDet (objects.py); MobileNet-SSD is no longer loaded

    def faces(self, img, score):
        with self.lock:
            h, w = img.shape[:2]
            self.yunet.setInputSize((w, h))
            self.yunet.setScoreThreshold(score)
            _, out = self.yunet.detect(img)
        return [] if out is None else list(out)

    def persons(self, img, conf=0.5):
        if self.ssd is None:
            return []
        h, w = img.shape[:2]
        blob = cv2.dnn.blobFromImage(cv2.resize(img, (300, 300)), 0.007843, (300, 300), 127.5)
        self.ssd.setInput(blob)
        det = self.ssd.forward()
        out = []
        for i in range(det.shape[2]):
            c, cls = float(det[0, 0, i, 2]), int(det[0, 0, i, 1])
            if cls == 15 and c >= conf:              # VOC class 15 = person
                x1, y1, x2, y2 = det[0, 0, i, 3:7]
                out.append({"x": float(x1), "y": float(y1), "w": float(x2 - x1), "h": float(y2 - y1),
                            "score": round(c, 3)})
        return out

    def feature_from_still(self, jpg: bytes, score=0.8, min_eye_px=0.0, max_yaw=25.0, roi=None):
        """Returns (feature or None, info dict, aligned 112x112 crop jpg or None)."""
        if self.sface is None:
            return None, {"error": "SFace model missing"}, None
        t0 = time.monotonic()
        full = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
        if full is None:
            return None, {"error": "decode failed"}, None
        H, W = full.shape[:2]
        k = max(1.0, max(W, H) / 740.0)        # detect on ~740 px long side (2960 -> /4, phone 1280 -> /1.7)
        small = cv2.resize(full, (int(W / k), int(H / k)), interpolation=cv2.INTER_AREA) if k > 1 else full
        ox = oy = 0
        used_roi = False
        # a small (mid-distance) face: skip the /4 pass and go straight to a native-resolution crop around it
        far = roi is not None and float(roi[2]) * W < 220
        rows = [] if far else self.faces(small, score)
        if not rows and roi is not None:
            # far face (2-3 m): too small after the /4 decode -> detect again on a full-resolution crop around
            # where the live 720p detector saw it (the pan-tilt is at rest, so the still shows the same place)
            rx, ry, rw, rh = [float(v) for v in roi]
            big = max(rw * W, rh * H)
            side = max(400.0, (4.0 if big < 300 else 1.4) * big)      # face box x4; body/head box x1.4
            cx, cy = (rx + rw / 2) * W, (ry + rh / 2) * H
            ox, oy = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
            x1, y1 = int(min(W, cx + side / 2)), int(min(H, cy + side / 2))
            crop = full[oy:y1, ox:x1]
            k = max(1.0, max(crop.shape[:2]) / 740.0)
            cs = cv2.resize(crop, (int(crop.shape[1] / k), int(crop.shape[0] / k)), interpolation=cv2.INTER_AREA) if k > 1 else crop
            rows = self.faces(cs, score)
            used_roi = True
        if not rows:
            return None, {"error": "no face" + (" (also in ROI)" if used_roi else ""), "size": f"{W}x{H}",
                          "ms": int((time.monotonic() - t0) * 1000)}, None
        row = max(rows, key=lambda r: r[2] * r[3]).copy()      # largest face
        row[:14] *= k
        row[0:14:2] += ox
        row[1:14:2] += oy
        eye_px = math.dist((row[4], row[5]), (row[6], row[7]))
        yaw, pitch, roll = face_pose(row)
        info = {"score": round(float(row[14]), 3), "eye_px": round(eye_px, 1), "frontal": frontal_ok(row, max_yaw),
                "yaw": yaw, "pitch": pitch, "roll": roll, "pose": pose_bin(yaw),
                "size": f"{W}x{H}", "faces": len(rows), "roi": used_roi,
                "box": [round(float(row[0]) / W, 4), round(float(row[1]) / H, 4),
                        round(float(row[2]) / W, 4), round(float(row[3]) / H, 4)]}
        if eye_px < min_eye_px:
            info.update(error=f"face too small (eye {eye_px:.0f}px < {min_eye_px:.0f})",
                        ms=int((time.monotonic() - t0) * 1000))
            return None, info, None
        if not info["frontal"]:
            info.update(error=f"face turned too far (yaw {yaw:+.0f}°, pitch {pitch:+.0f}°, max {max_yaw:.0f}°)",
                        ms=int((time.monotonic() - t0) * 1000))
            return None, info, None
        with self.lock:
            crop = self.sface.alignCrop(full, row)
        info["sharp"] = round(sharpness(crop), 1)
        if info["sharp"] < BLUR_MIN:
            info.update(error=f"blurry (sharpness {info['sharp']:.0f} < {BLUR_MIN:.0f})", ms=int((time.monotonic() - t0) * 1000))
            return None, info, None
        with self.lock:
            feat = self.sface.feature(crop).flatten().astype(np.float32)
        feat /= (np.linalg.norm(feat) + 1e-9)
        ok, enc = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
        info["ms"] = int((time.monotonic() - t0) * 1000)
        return feat, info, (enc.tobytes() if ok else None)


POSE_BIN_DEG = 20.0
BLUR_MIN = 120.0         # offline: sharp live stills give 680-1250; motion blur drops far below (Laplacian variance of the aligned 112x112 crop)      # pose-binned matching: same-side samples within this yaw difference


def sharpness(img):
    """Laplacian variance of a grey image (higher = sharper); used to skip motion-blurred stills."""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    return float(cv2.Laplacian(g, cv2.CV_64F).var())


class Gallery:
    """Household whitelist: faces/<name>.npz (feats N x 128, mean) + faces/<name>_<k>.jpg crops."""

    def __init__(self, folder=None):
        self.folder = Path(folder or ROOT / C.FACES_DIR)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.people = {}
        self.reload()

    def reload(self):
        people = {}
        for f in sorted(self.folder.glob("*.npz")):
            try:
                d = np.load(f)
                m = d["mean"].astype(np.float32)
                F = d["feats"].astype(np.float32)
                F = F / (np.linalg.norm(F, axis=1, keepdims=True) + 1e-9)
                Y = d["yaw"] if "yaw" in d else np.full(len(F), np.nan)
                poses = {"front": int(np.sum(~(np.abs(Y) >= 12))), "left": int(np.sum(Y <= -12)),
                         "right": int(np.sum(Y >= 12))}
                people[f.stem] = {"mean": m / (np.linalg.norm(m) + 1e-9), "n": int(F.shape[0]), "feats": F, "yaw": Y,
                                  "poses": poses,
                                  "t": time.strftime("%Y-%m-%d %H:%M", time.localtime(f.stat().st_mtime))}
            except Exception as exc:
                print("bad gallery file", f, exc)
        with self.lock:
            self.people = people

    def save(self, name, feats, crops, append=False, yaws=None):
        """append=True adds the new samples to an existing person (e.g. phone + monitoring camera)."""
        if not NAME_RE.match(name) or name.strip(". ") != name.strip():
            raise ValueError("名字只能是 1-32 个字母/数字/汉字/空格/-/_")
        name = name.strip()
        F = np.stack(feats).astype(np.float32)
        Y = np.array(yaws if yaws is not None else [np.nan] * len(feats), np.float32)
        npz = self.folder / f"{name}.npz"
        start = 0
        if append and npz.exists():
            d = np.load(npz)
            old = d["feats"].astype(np.float32)
            oy = d["yaw"].astype(np.float32) if "yaw" in d else np.full(len(old), np.nan, np.float32)
            F, Y = np.concatenate([old, F])[-60:], np.concatenate([oy, Y])[-60:]
            start = len(list(self.folder.glob(f"{glob_escape(name)}_*.jpg")))
        else:
            for old in self.folder.glob(f"{glob_escape(name)}_*.jpg"):
                old.unlink()
        # mean template from the near-frontal samples (if any), all samples are kept for matching
        fr = ~(np.abs(Y) >= 12.0)            # NaN (old samples, pose unknown) counts as frontal
        np.savez(npz, feats=F, mean=F[fr].mean(axis=0) if fr.any() else F.mean(axis=0), yaw=Y)
        for k, c in enumerate(crops):
            if c:
                (self.folder / f"{name}_{start + k}.jpg").write_bytes(c)
        self.reload()
        return int(F.shape[0])

    def delete(self, name):
        if name not in self.people:
            raise KeyError(f"没有这个人: {name}")
        (self.folder / f"{name}.npz").unlink(missing_ok=True)
        for old in self.folder.glob(f"{glob_escape(name)}_*.jpg"):
            old.unlink()
        self.reload()

    def match(self, feat, yaw=None):
        """Best (name, cosine). (None, 0) if the gallery is empty. yaw (deg) of the query face enables
        pose-binned matching: a turned face is also compared with the samples enrolled at a similar yaw."""
        with self.lock:
            best, score = None, -1.0
            for name, p in self.people.items():
                # mean template, or the average of the 3 closest samples (helps when a person was enrolled
                # with two different cameras: phone + monitoring camera)
                sims = p["feats"] @ feat
                top = np.sort(sims)[-3:]
                s = max(float(np.dot(feat, p["mean"])), float(top.mean()))
                if yaw is not None and abs(yaw) >= 12:
                    Y = p["yaw"]
                    near = sims[np.abs(np.nan_to_num(Y, nan=0.0) - yaw) <= POSE_BIN_DEG]
                    if len(near):
                        s = max(s, float(np.sort(near)[-2:].mean()))
                if s > score:
                    best, score = name, s
        return (best, score) if best else (None, 0.0)

    def listing(self):
        with self.lock:
            out = []
            for name, p in sorted(self.people.items()):
                crops = sorted(x.name for x in self.folder.glob(f"{glob_escape(name)}_*.jpg"))
                out.append({"name": name, "samples": p["n"], "time": p["t"], "crops": crops, "poses": p["poses"]})
            return out


def glob_escape(s):
    return re.sub(r"([*?\[\]])", r"[\1]", s)


class Motion:
    """Frame differencing on a 320x180 grey image with a decaying energy map (calibration:
    the camera holds still and the user waves). Returns the strongest blobs, normalised boxes."""

    def __init__(self):
        self.prev = None
        self.energy = None

    def reset(self):
        self.prev = self.energy = None

    def update(self, img):
        g = cv2.GaussianBlur(cv2.cvtColor(cv2.resize(img, (320, 180), interpolation=cv2.INTER_AREA),
                                          cv2.COLOR_BGR2GRAY), (5, 5), 0)
        if self.prev is None:
            self.prev, self.energy = g, np.zeros(g.shape, np.float32)
            return []
        d = (cv2.absdiff(g, self.prev) > 18).astype(np.float32)
        self.prev = g
        self.energy = 0.7 * self.energy + 0.3 * d
        m = cv2.dilate((self.energy > 0.2).astype(np.uint8), np.ones((5, 5), np.uint8), iterations=2)
        cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        out = []
        for c in sorted(cnts, key=cv2.contourArea, reverse=True)[:3]:
            a = cv2.contourArea(c)
            if a < 40:
                break
            x, y, w, h = cv2.boundingRect(c)
            e = float(self.energy[y:y + h, x:x + w].sum())
            out.append({"x": x / 320, "y": y / 180, "w": w / 320, "h": h / 180, "area": int(a), "energy": round(e, 1)})
        return out


class VisionThread(threading.Thread):
    """Runs YuNet on the newest 720p frame while app.vision_active() is true. Person / hand boxes
    come from the object thread (objects.py); motion blobs only in calibration mode."""

    def __init__(self, app):
        super().__init__(daemon=True, name="vision")
        self.app = app
        self.det = Detector()
        self.motion = Motion()
        self.running = True
        self.result = {"t": 0.0, "faces": [], "persons": [], "hands": [], "target": None, "ms": 0}
        self.rate = 0.0
        self._times = []
        self.head = None            # last good face box (half-res px) + continuation tracker
        self.profile = None
        f = ROOT / C.MODEL_DIR / "haarcascade_profileface.xml"
        if f.exists():
            c = cv2.CascadeClassifier(str(f))
            self.profile = None if c.empty() else c

    def continue_head(self, img, weak_rows, now, s):
        """Face lost (turned away / profile): keep following the same head for head_track_s.
        1) a low-score YuNet face overlapping the last head box (YuNet still sees ~45-60 deg turns
           at low score), 2) the OpenCV profile Haar cascade around the last box (both directions),
        3) a KCF tracker on the last box. 1/2 refresh the timer, 3 does not."""
        hd = self.head
        if hd is None or not s["head_track"] or now - hd["t_face"] > s["head_track_s"]:
            self.head = None
            return None, None, None
        x, y, w, h = hd["box"]
        cx, cy = x + w / 2, y + h / 2
        for r in weak_rows:
            rx, ry, rw, rh = [float(v) for v in r[:4]]
            if abs(rx + rw / 2 - cx) < 1.2 * w and abs(ry + rh / 2 - cy) < 1.2 * h:
                hd.update(box=(rx, ry, rw, rh), t_face=now, tracker=None)
                yaw = face_pose(r)[0]
                # low-score but near-frontal = small / dim face ("face_weak"), otherwise a turned face
                return ("face_weak" if abs(yaw) <= s["identify_max_yaw_deg"] else "profile"), (rx, ry, rw, rh), yaw
        H, W = img.shape[:2]
        if self.profile is not None:
            x0, y0 = int(max(0, cx - 1.5 * w)), int(max(0, cy - 1.5 * h))
            x1, y1 = int(min(W, cx + 1.5 * w)), int(min(H, cy + 1.5 * h))
            if x1 - x0 > 24 and y1 - y0 > 24:
                g = cv2.equalizeHist(cv2.cvtColor(img[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY))
                mn = max(20, int(0.6 * min(w, h)))
                for flip in (False, True):
                    gg = cv2.flip(g, 1) if flip else g
                    found = self.profile.detectMultiScale(gg, 1.15, 4, minSize=(mn, mn))
                    if len(found):
                        fx, fy, fw, fh = max(found, key=lambda b: b[2] * b[3])
                        if flip:
                            fx = gg.shape[1] - fx - fw
                        box = (float(x0 + fx), float(y0 + fy), float(fw), float(fh))
                        hd.update(box=box, t_face=now, tracker=None)
                        return "profile", box, None
        try:
            if hd.get("tracker") is None:
                hd["tracker"] = make_tracker()
                hd["tracker"].init(img, tuple(int(v) for v in (x, y, w, h)))
                return "head", (x, y, w, h), None
            ok, bb = hd["tracker"].update(img)
        except Exception:
            ok = False
        if not ok:
            self.head = None
            return None, None, None
        hd["box"] = tuple(float(v) for v in bb)
        return "head", hd["box"], None

    def run(self):
        import pointing
        last_seq = -1
        while self.running:
            s = self.app.settings.get()
            calib = self.app.calib_motion_active()
            if not (s["vision_enabled"] and (self.app.vision_active() or calib)):
                self.motion.reset()
                time.sleep(0.1)
                continue
            hz = s["vision_hz"] if self.app.cpu_temp() < C.TEMP_THROTTLE_C else min(2.0, s["vision_hz"])
            t0 = time.monotonic()
            seq, jpg = self.app.camera.wait_frame(last_seq, 1.0)
            if jpg is None or seq == last_seq:
                continue
            last_seq = seq
            frame_t = self.app.camera.frame_t
            try:
                img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_REDUCED_COLOR_2)
                h, w = img.shape[:2]
                sx, sy = C.CAM_WIDTH / w, C.CAM_HEIGHT / h
                rows_all = self.det.faces(img, min(s["face_score"], s["face_weak_score"]))
                rows = [r for r in rows_all if r[14] >= s["face_score"]]
                weak = [r for r in rows_all if r[14] < s["face_score"]]
                faces = [face_row_to_dict(r, sx, sy, C.CAM_WIDTH, C.CAM_HEIGHT) for r in rows]
                now0 = time.monotonic()
                motion = self.motion.update(img) if calib else []
                objs = self.app.objects_result(max_age=1.2)
                ores = self.app.objects.result if self.app.objects else {}
                persons = [o for o in objs if o["label"] == "person" and o["score"] >= s["person_conf"]]
                hands = [o for o in objs if o["label"] == "hand"]
                target = None
                W, H = C.CAM_WIDTH, C.CAM_HEIGHT
                kind_c, box_c, yaw_c = (None, None, None) if faces else self.continue_head(img, weak, now0, s)
                if faces:   # face closest to the image centre = the one the pan-tilt is pointing at
                    f = min(faces, key=lambda f: (f["cx"] - W / 2) ** 2 + (f["cy"] - H / 2) ** 2)
                    target = {"kind": "face", "u": f["cx"], "v": f["cy"], "score": f["score"],
                              "eye_px": f["eye_px"], "box": [f["x"], f["y"], f["w"], f["h"]],
                              "yaw": f["yaw"], "pitch": f["pitch"], "src_t": frame_t, "src_id": f"f{seq}"}
                    self.head = {"box": (f["x"] * w, f["y"] * h, f["w"] * w, f["h"] * h), "t_face": now0,
                                 "tracker": None}
                elif kind_c:
                    bx, by, bw, bh = box_c
                    target = {"kind": kind_c, "u": (bx + bw / 2) * sx, "v": (by + bh / 2) * sy, "score": 0,
                              "eye_px": 0, "box": [bx / w, by / h, bw / w, bh / h],
                              "src_t": frame_t, "src_id": f"f{seq}"}
                    if yaw_c is not None:
                        target["yaw"] = yaw_c
                elif persons:
                    p = min(persons, key=lambda p: abs(p["x"] + p["w"] / 2 - 0.5))
                    # aim at the head region of the person box
                    target = {"kind": "person", "u": (p["x"] + p["w"] / 2) * W,
                              "v": (p["y"] + min(p["h"], p["w"] * 1.2) * 0.2) * H, "score": p["score"],
                              "eye_px": 0, "box": [p["x"], p["y"], p["w"], p["h"]],
                              "src_t": ores.get("frame_t", frame_t), "src_id": f"o{ores.get('t', 0):.3f}"}
                elif hands:
                    p = max(hands, key=lambda p: p["score"])
                    target = {"kind": "hand", "u": (p["x"] + p["w"] / 2) * W, "v": (p["y"] + p["h"] / 2) * H,
                              "score": p["score"], "eye_px": 0, "box": [p["x"], p["y"], p["w"], p["h"]],
                              "src_t": ores.get("frame_t", frame_t), "src_id": f"o{ores.get('t', 0):.3f}"}
                if target:
                    eu, ev = target["u"] - W / 2, target["v"] - H / 2
                    target.update(eu_px=round(eu, 1), ev_px=round(ev, 1),
                                  eu_deg=round(pointing.pixel_to_deg(eu, W, s["cam_hfov_deg"]), 2),
                                  ev_deg=round(pointing.pixel_to_deg(ev, H, s["cam_vfov_deg"]), 2))
                now = time.monotonic()
                res = {"t": now, "faces": faces, "persons": persons, "hands": hands, "target": target,
                       "weak_faces": len(weak), "profile_cascade": self.profile is not None,
                       "motion": motion, "ms": int((now - t0) * 1000), "frame_t": frame_t}
                self.result = res
                self._times = [x for x in self._times if now - x < 2.0] + [now]
                self.rate = (len(self._times) - 1) / max(1e-3, self._times[-1] - self._times[0]) if len(self._times) > 1 else 0.0
                self.app.on_vision(res)
                self.app.hw_error.pop("vision", None)
            except Exception as exc:
                self.app.hw_error["vision"] = repr(exc)
            time.sleep(max(0.0, 1.0 / hz - (time.monotonic() - t0)))
