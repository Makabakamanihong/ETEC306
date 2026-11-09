#!/usr/bin/env python3
"""Pan (CH4) range test on the running service (run ON the Pi):
step the pulse outwards in small increments and check from the camera picture (phase correlation)
that the gimbal is still really moving. Stops at the first step where the picture stops moving
(mechanical stop / stall) and backs off. Ramps back to 1500 us slowly and leaves manual mode.

  python3 tools/pan_range_test.py            # both sides, 2000->2389 and 1000->611
  python3 tools/pan_range_test.py --max 2389 --min 611 --step 5
"""
import argparse, json, subprocess, time, urllib.request
import numpy as np, cv2

API = "http://127.0.0.1:8080"


def post(path, obj):
    r = urllib.request.Request(API + path, data=json.dumps(obj).encode(), headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(r, timeout=5))


def frame():
    jpg = urllib.request.urlopen(API + "/api/frame.jpg", timeout=5).read()
    g = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_REDUCED_GRAYSCALE_2)   # 640x360
    return np.float32(g)


def pulse(us, quiet=True):
    return post("/api/servo", {"action": "pulse", "ch": 4, "us": us, "quiet": quiet})["manual"]


def ramp(a, b, step=25, dt=0.08):
    n = max(1, int(abs(b - a) / step))
    for i in range(1, n + 1):
        pulse(a + (b - a) * i / n)
        time.sleep(dt)


def throttled():
    try:
        return subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True).stdout.strip()
    except Exception:
        return "?"


def side(start, end, step_ticks, hfov, log):
    us_per_tick = 20000 / 4096
    step_us = step_ticks * us_per_tick * (1 if end > start else -1)
    exp_px = abs(step_us) / (1000 / 90) * 640 / hfov            # expected picture shift per step
    ramp(1500, start)
    time.sleep(0.8)
    prev = frame()
    win = cv2.createHanningWindow(prev.shape[::-1], cv2.CV_32F)
    us, good, weak = start, start, 0
    while (end - us) * (1 if end > start else -1) > 0.5:
        us = us + step_us
        us = min(us, end) if end > start else max(us, end)
        rb = pulse(us)["readback"]
        time.sleep(0.7)
        cur = frame()
        (dx, dy), resp = cv2.phaseCorrelate(prev, cur, win)
        prev = cur
        # higher pulse = camera turns LEFT = picture moves RIGHT (dx > 0)
        moved = dx * (1 if end > start else -1)
        ok = moved > 0.35 * exp_px
        rec = {"us": round(us, 1), "readback_us": rb.get("us"), "mode1": rb.get("mode1"), "dx": round(dx, 1),
               "dy": round(dy, 1), "resp": round(resp, 3), "expected": round(exp_px, 1), "ok": ok}
        log.append(rec)
        print(json.dumps(rec), flush=True)
        if ok:
            good, weak = us, 0
        else:
            weak += 1
            if weak >= 2:
                print(f"picture stopped moving after {good:.0f} us -> stop", flush=True)
                break
    ramp(us, 1500)
    return good


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=float, default=2389)
    ap.add_argument("--min", type=float, default=611)
    ap.add_argument("--from-hi", type=float, default=1950)
    ap.add_argument("--from-lo", type=float, default=1050)
    ap.add_argument("--step", type=int, default=5, help="12-bit counts per step (5 = 24.4 us = 2.2 deg)")
    a = ap.parse_args()
    s = json.load(urllib.request.urlopen(API + "/api/settings", timeout=5))["settings"]
    print("throttled before:", throttled(), flush=True)
    log = []
    try:
        hi = side(a.from_hi, a.max, a.step, s["cam_hfov_deg"], log)
        lo = side(a.from_lo, a.min, a.step, s["cam_hfov_deg"], log)
    finally:
        try:
            ramp(float(json.load(urllib.request.urlopen(API + "/api/state?lite"))["guide"].get("pan_us") or 1500), 1500)
        except Exception:
            pass
        post("/api/servo", {"action": "exit"})
    print("throttled after:", throttled(), flush=True)
    res = {"good_max_us": round(hi, 1), "good_min_us": round(lo, 1),
           "left_deg": round((hi - 1500) / (1000 / 90), 1), "right_deg": round((1500 - lo) / (1000 / 90), 1)}
    print("RESULT", json.dumps(res), flush=True)
    with open("pan_range_test.json", "w") as f:
        json.dump({"result": res, "steps": log, "time": time.strftime("%Y-%m-%d %H:%M:%S")}, f, indent=1)


if __name__ == "__main__":
    main()
