"""USB UVC camera (/dev/video0) as an MJPEG pass-through, no decoding.

Runs `v4l2-ctl --stream-mmap --stream-to=-` and splits the byte stream into JPEG frames.
The latest 720p frame feeds the web page, snapshots and the face detector (which decodes at
reduced size). capture_still() briefly stops the stream and grabs one frame at the camera's
largest MJPEG size for face recognition / enrollment (7.6), then restarts the 720p stream.
"""
from __future__ import annotations

import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import config as C

SOI, EOI = b"\xff\xd8", b"\xff\xd9"


def split_jpegs(data: bytes) -> list:
    out, i = [], 0
    while True:
        s = data.find(SOI, i)
        if s < 0:
            break
        e = data.find(EOI, s + 2)
        if e < 0:
            break
        out.append(data[s:e + 2])
        i = e + 2
    return out


class Camera(threading.Thread):
    def __init__(self, dev=C.CAMERA_DEV, w=C.CAM_WIDTH, h=C.CAM_HEIGHT, fps=C.CAM_FPS):
        super().__init__(daemon=True, name="camera")
        self.dev, self.w, self.h, self.fps_req = dev, w, h, fps
        self.cond = threading.Condition()
        self.frame = None
        self.frame_t = 0.0
        self.seq = 0
        self.error = ""
        self.running = True
        self.proc = None
        self.times = deque(maxlen=30)
        self.paused = threading.Event()      # set while a still capture owns the device
        self.idle = threading.Event()        # set by run() when the stream process is down
        self.sleeping = threading.Event()    # power saving: stream closed (device released), USB power stays on
        self.sleep_since = None
        self.wake_t = None                   # monotonic time of the last wake() (for wake latency)
        self.wake_log = deque(maxlen=20)     # {"t", "first_frame_ms"}
        self.still_lock = threading.Lock()
        self.last_still = {"t": None, "size": None, "bytes": 0, "ms": 0, "error": ""}

    @property
    def fps(self):
        if len(self.times) < 2 or time.monotonic() - self.times[-1] > 2:
            return 0.0
        span = self.times[-1] - self.times[0]
        return (len(self.times) - 1) / span if span > 0 else 0.0

    @property
    def ok(self):
        return self.frame is not None and time.monotonic() - self.frame_t < 2.0

    def run(self):
        while self.running:
            if self.paused.is_set() or self.sleeping.is_set():
                self.idle.set()
                time.sleep(0.05)
                continue
            self.idle.clear()
            if getattr(C, "CAM_CONTROLS", ""):
                try:
                    subprocess.run(["v4l2-ctl", "-d", self.dev, "-c", C.CAM_CONTROLS], timeout=3,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass
            cmd = ["v4l2-ctl", "-d", self.dev,
                   f"--set-fmt-video=width={self.w},height={self.h},pixelformat=MJPG",
                   f"--set-parm={self.fps_req}", "--stream-mmap=4", "--stream-count=0",
                   "--stream-to=-", "--silent"]
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
                buf = bytearray()
                while self.running and not self.paused.is_set() and not self.sleeping.is_set():
                    chunk = self.proc.stdout.read(65536)
                    if not chunk:
                        if self.paused.is_set() or self.sleeping.is_set():
                            break
                        err = self.proc.stderr.read().decode(errors="replace").strip()
                        raise RuntimeError(f"v4l2-ctl exited: {err[-200:]}")
                    buf += chunk
                    while True:
                        s = buf.find(SOI)
                        if s < 0:
                            buf.clear()
                            break
                        e = buf.find(EOI, s + 2)
                        if e < 0:
                            if s:
                                del buf[:s]
                            break
                        jpg = bytes(buf[s:e + 2])
                        del buf[:e + 2]
                        now = time.monotonic()
                        if self.wake_t is not None:
                            self.wake_log.append({"t": datetime.now().strftime("%H:%M:%S"),
                                                  "first_frame_ms": int((now - self.wake_t) * 1000)})
                            self.wake_t = None
                        with self.cond:
                            self.frame, self.frame_t = jpg, now
                            self.seq += 1
                            self.times.append(now)
                            self.cond.notify_all()
                    if len(buf) > 8_000_000:
                        buf.clear()
                    self.error = ""
            except Exception as exc:
                if not self.paused.is_set() and not self.sleeping.is_set():
                    self.error = str(exc)
            finally:
                self.stop_proc()
            if self.running and not self.paused.is_set() and not self.sleeping.is_set():
                time.sleep(2.0)

    def sleep(self):
        if not self.sleeping.is_set():
            self.sleeping.set()
            self.sleep_since = time.monotonic()
            self.stop_proc()

    def wake(self):
        if self.sleeping.is_set():
            self.wake_t = time.monotonic()
            self.sleep_since = None
            self.sleeping.clear()

    def capture_still(self, w=C.STILL_WIDTH, h=C.STILL_HEIGHT, frames=5, timeout=8.0) -> bytes:
        """Grab one full-resolution MJPEG frame. The 720p stream pauses for ~1 s."""
        with self.still_lock:
            t0 = time.monotonic()
            self.paused.set()
            try:
                self.stop_proc()
                self.idle.wait(3.0)
                cmd = ["v4l2-ctl", "-d", self.dev, f"--set-fmt-video=width={w},height={h},pixelformat=MJPG",
                       "--stream-mmap=3", f"--stream-count={frames}", "--stream-to=-", "--silent"]
                r = subprocess.run(cmd, capture_output=True, timeout=timeout)
                jpgs = split_jpegs(r.stdout)
                if not jpgs:
                    raise RuntimeError("still capture returned no JPEG: " + r.stderr.decode(errors="replace")[-200:])
                jpg = jpgs[-1]
                self.last_still = {"t": datetime.now().strftime("%H:%M:%S"), "size": f"{w}x{h}",
                                   "bytes": len(jpg), "ms": int((time.monotonic() - t0) * 1000), "error": ""}
                return jpg
            except Exception as exc:
                self.last_still["error"] = str(exc)
                raise
            finally:
                self.paused.clear()

    def wait_frame(self, last_seq, timeout=1.0):
        with self.cond:
            if self.seq == last_seq:
                self.cond.wait(timeout)
            return self.seq, self.frame

    def stop_proc(self):
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(2)
            except Exception:
                p.kill()

    def stop(self):
        self.running = False
        self.stop_proc()


class SnapshotStore:
    def __init__(self, folder: Path):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)

    def save(self, jpg: bytes, reason: str) -> str:
        name = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3] + f"-{reason}.jpg"
        (self.folder / name).write_bytes(jpg)
        files = sorted(self.folder.glob("*.jpg"))
        for old in files[:-C.SNAP_KEEP]:
            try:
                old.unlink()
            except OSError:
                pass
        return name

    def recent(self, n=12):
        return [p.name for p in sorted(self.folder.glob("*.jpg"))[-n:]][::-1]
