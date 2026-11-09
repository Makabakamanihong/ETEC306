#!/usr/bin/env python3
"""Camera-based servo tests from 技术方案.md section 10 that need nobody in front of the camera
(run ON the Pi against the running service; it takes manual control of CH4 and gives it back):

  python3 tools/servo_tests.py t2     # smallest step that really moves the picture (1, 2, 3 ... LSB)
  python3 tools/servo_tests.py t3     # 30 deg pan jump: time until the picture has settled (x20)
  python3 tools/servo_tests.py t7     # 2 min back-and-forth +-45 deg, check undervoltage / reboot / readback
  python3 tools/servo_tests.py all

Measurement = phase correlation of /api/frame.jpg (640x360 grey). Results -> tools/servo_tests.json.
Needs a scene with some texture (a blank wall gives no picture shift).
"""
import json, subprocess, sys, time, statistics
from pathlib import Path
import numpy as np, cv2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pan_range_test import API, post, ramp, throttled      # noqa: E402
import urllib.request                                       # noqa: E402

US_PER_LSB = 20000 / 4096
OUT = Path(__file__).resolve().parent / "servo_tests.json"


def pulse(us):
    return post("/api/servo", {"action": "pulse", "ch": 4, "us": us, "quiet": True})["manual"]


def grab():
    t0 = time.monotonic()
    jpg = urllib.request.urlopen(API + "/api/frame.jpg", timeout=5).read()
    return t0, jpg


def gray(jpg):
    return np.float32(cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_REDUCED_GRAYSCALE_2))


def shift(a, b, win):
    (dx, dy), resp = cv2.phaseCorrelate(a, b, win)
    return dx, resp


def state():
    return json.load(urllib.request.urlopen(API + "/api/state?lite", timeout=5))


def cur_us():
    try:
        m = state().get("manual") or {}
        return float(m.get("pan_us") or state()["guide"].get("pan_us") or 1500)
    except Exception:
        return 1500.0


def t2(res):
    """Steps of 1..6 LSB from 1500 us, each repeated 4x forward/back; picture shift per step."""
    ramp(cur_us(), 1500)
    time.sleep(1.0)
    a = gray(grab()[1])
    win = cv2.createHanningWindow(a.shape[::-1], cv2.CV_32F)
    hfov = state()["settings"].get("cam_hfov_deg", 100)
    px_per_deg = 640 / hfov
    rows = []
    for n in (1, 2, 3, 4, 6):
        moved = []
        for rep in range(4):
            for sgn in (1, -1):
                base = gray(grab()[1])
                pulse(1500 + sgn * n * US_PER_LSB if sgn > 0 else 1500)
                time.sleep(0.6)
                cur = gray(grab()[1])
                moved.append(abs(shift(base, cur, win)[0]))
        # noise floor: same measurement without commanding anything
        rows.append({"lsb": n, "us": round(n * US_PER_LSB, 2), "deg_cmd": round(n * US_PER_LSB / (1000 / 90), 3),
                     "px_expected": round(n * US_PER_LSB / (1000 / 90) * px_per_deg, 2),
                     "px_median": round(statistics.median(moved), 2), "px_min": round(min(moved), 2)})
        print("T2", json.dumps(rows[-1]), flush=True)
    noise = []
    for _ in range(6):
        b = gray(grab()[1]); time.sleep(0.6); c = gray(grab()[1])
        noise.append(abs(shift(b, c, win)[0]))
    nf = statistics.median(noise)
    for r in rows:
        r["moves"] = r["px_min"] > max(3 * nf, 0.3 * r["px_expected"], 0.15)
    smallest = next((r for r in rows if r["moves"]), None)
    res["t2"] = {"noise_px": round(nf, 3), "steps": rows,
                 "smallest_moving_lsb": smallest["lsb"] if smallest else None,
                 "smallest_moving_deg": smallest["deg_cmd"] if smallest else None}
    print("T2 result", json.dumps(res["t2"]), flush=True)


def t3(res, n=20, jump_deg=30.0):
    """Manual pulse jump of 30 deg (bypasses the guidance rate limiter = pure servo + camera time).
    Settled = first frame whose shift vs the final frame is < 1 px. Camera pipeline delay is included,
    so the number is an upper bound for the mechanical time."""
    lo, hi = 1500 - jump_deg * 1000 / 90 / 2, 1500 + jump_deg * 1000 / 90 / 2
    ramp(cur_us(), lo)
    time.sleep(1.0)
    times, now_us = [], lo
    fps = state()["camera"]["fps"]
    for i in range(n):
        target = hi if now_us == lo else lo
        ref0 = gray(grab()[1])
        frames = []
        t_cmd = time.monotonic()
        pulse(target)
        last = None
        while time.monotonic() - t_cmd < 1.2:
            t, jpg = grab()
            if jpg != last:
                frames.append((t - t_cmd, jpg))
                last = jpg
        final = gray(frames[-1][1])
        win = cv2.createHanningWindow(final.shape[::-1], cv2.CV_32F)
        settle, started = None, None
        for dt, jpg in frames:
            dx, _ = shift(gray(jpg), final, win)
            if started is None and abs(shift(ref0, gray(jpg), win)[0]) > 2:
                started = dt
            if abs(dx) < 1.0:
                settle = dt
                break
        times.append({"settle_ms": round(settle * 1000) if settle is not None else None,
                      "first_motion_ms": round(started * 1000) if started is not None else None, "frames": len(frames)})
        print("T3", i, json.dumps(times[-1]), flush=True)
        now_us = target
        time.sleep(0.5)
    ramp(now_us, 1500)
    ok = [t["settle_ms"] for t in times if t["settle_ms"] is not None]
    fm = [t["first_motion_ms"] for t in times if t["first_motion_ms"] is not None]
    lat = state().get("latency_ms", {})
    res["t3"] = {"jump_deg": jump_deg, "n": len(ok), "camera_fps": fps,
                 "settle_ms_median": statistics.median(ok) if ok else None, "settle_ms_max": max(ok) if ok else None,
                 "first_motion_ms_median": statistics.median(fm) if fm else None,
                 "software_latency_ms": lat, "samples": times}
    print("T3 result", json.dumps({k: v for k, v in res["t3"].items() if k != "samples"}), flush=True)


def t7(res, seconds=120, amp_deg=45.0, period=1.0):
    up0 = float(open("/proc/uptime").read().split()[0])
    th0 = throttled()
    lo, hi = 1500 - amp_deg * 1000 / 90, 1500 + amp_deg * 1000 / 90
    t_end = time.monotonic() + seconds
    n, bad, maxdiff, modes = 0, 0, 0.0, set()
    ramp(cur_us(), lo)
    us = lo
    while time.monotonic() < t_end:
        us = hi if us == lo else lo
        rb = pulse(us)["readback"]
        n += 1
        diff = abs((rb.get("us") or 0) - us)
        maxdiff = max(maxdiff, diff)
        modes.add(str(rb.get("mode1")))
        if diff > 6 or str(rb.get("mode1")) not in ("0x20", "32"):     # 0x20 = auto-increment, awake
            bad += 1
        time.sleep(period)
    ramp(us, 1500)
    up1 = float(open("/proc/uptime").read().split()[0])
    jr = subprocess.run(["journalctl", "-k", "--since", f"-{seconds + 30}s", "--no-pager"], capture_output=True, text=True).stdout
    res["t7"] = {"seconds": seconds, "moves": n, "amp_deg": amp_deg, "readback_mismatch": bad, "readback_max_diff_us": round(maxdiff, 2), "mode1_seen": sorted(modes),
                 "throttled_before": th0, "throttled_after": throttled(),
                 "uptime_continuous": up1 > up0, "kernel_undervoltage_lines": sum("oltage" in l for l in jr.splitlines())}
    print("T7 result", json.dumps(res["t7"]), flush=True)


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    res = json.loads(OUT.read_text()) if OUT.exists() else {}
    res["time"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        if which in ("t2", "all"):
            t2(res)
        if which in ("t3", "all"):
            t3(res)
        if which in ("t7", "all"):
            t7(res)
    finally:
        try:
            ramp(cur_us(), 1500)
        finally:
            post("/api/servo", {"action": "exit"})
        OUT.write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
