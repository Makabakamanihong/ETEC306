#!/usr/bin/env python3
"""Offline check of the vision models on the Pi (no camera needed).
usage: python3 tools/vision_selftest.py img1.jpg [img2.jpg ...]
Each image is pasted into a 2960x1664 still (like capture_still), run through
YuNet (preview path + still path), SFace, MobileNet-SSD; cosine similarity matrix printed."""
import sys, time, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cv2, numpy as np
import vision

det = vision.Detector()
feats = []
for p in sys.argv[1:]:
    img = cv2.imread(p)
    canvas = np.full((1664, 2960, 3), 128, np.uint8)
    s = min(1200 / img.shape[0], 1.0) if img.shape[0] > 1200 else 1200 / img.shape[0]
    big = cv2.resize(img, None, fx=s, fy=s)[:1600, :2900]
    canvas[:big.shape[0], :big.shape[1]] = big
    jpg = cv2.imencode(".jpg", canvas, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()
    prev = cv2.resize(canvas, (640, 360))
    t = time.monotonic(); rows = det.faces(prev, 0.8); t1 = (time.monotonic() - t) * 1000
    t = time.monotonic(); per = det.persons(prev, 0.5); t2 = (time.monotonic() - t) * 1000
    f, info, crop = det.feature_from_still(jpg, 0.8, 40)
    print(f"{Path(p).name}: preview faces={len(rows)} ({t1:.0f} ms)  persons={len(per)} ({t2:.0f} ms)  still={info}")
    feats.append((Path(p).name, f))
with tempfile.TemporaryDirectory() as d:
    g = vision.Gallery(d)
    ok = [(n, f) for n, f in feats if f is not None]
    if ok:
        g.save("test_person", [ok[0][1]], [None])
        for n, f in ok:
            print(f"match {n}: {g.match(f)}  (threshold 0.363)")
