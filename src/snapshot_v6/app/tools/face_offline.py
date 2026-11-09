import sys, glob, numpy as np
sys.path.insert(0, __import__("os").path.join(__import__("os").path.dirname(__file__), ".."))
import vision
d = vision.Detector()
g = np.load(sys.argv[2] if len(sys.argv) > 2 else "faces/Tang.npz"); F = g["feats"]; F = F / np.linalg.norm(F, axis=1, keepdims=True)
Y = g["yaw"]; mean = g["mean"] / np.linalg.norm(g["mean"])
front = ~(np.abs(Y) >= 12)
for p in sorted(glob.glob(sys.argv[1] + "/*.jpg")):
    jpg = open(p, "rb").read()
    feat, info, crop = d.feature_from_still(jpg, 0.7, 0, 60)
    if feat is None:
        print(p.split("/")[-1], "no:", info.get("error")); continue
    s = F @ feat
    print("%-40s eye %5.1f yaw %+5.1f | mean %.3f top3 %.3f max %.3f maxfront %.3f maxold(nan) %.3f maxnew %.3f" % (
        p.split("/")[-1][-40:], info["eye_px"], info["yaw"], feat @ mean, np.sort(s)[-3:].mean(), s.max(), s[front].max(),
        s[np.isnan(Y)].max(), s[~np.isnan(Y)].max()))
