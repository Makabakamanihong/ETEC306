"""Multi-target association, Kalman smoothing and primary-target selection (技术方案.md 7.3).

- Nearest neighbour with a gate (default 0.6 m), greedy on smallest distance.
- Track confirmed when present in >= 7 of the last 10 frames; deleted after track_drop_s.
- Each axis has a constant-velocity Kalman filter [p, v] (position-only update, process noise
  from a walking acceleration ~1 m/s^2, 7.3). use_kalman=false falls back to an EMA.
- Several LD2450 points closer than merge_m (arm / leg of the same person) are merged first.
- Robust update: an innovation larger than jump_m is down-weighted (x3 noise) and only accepted
  outright after 3 consecutive frames, so a waving hand does not drag the track.
- Primary target: confirmed tracks that are not marked "object", vision-confirmed people and
  approaching ones preferred, then nearest; switching needs a margin AND switch_hold_s.
"""
from __future__ import annotations

import math
import time
from collections import deque

import config as C


class Axis:
    """1-D constant-velocity Kalman filter, state [p (mm), v (mm/s)]."""

    def __init__(self, p, meas_std):
        self.p, self.v = float(p), 0.0
        r = meas_std ** 2
        self.P = [[r, 0.0], [0.0, 1.0e6]]     # unknown initial velocity

    def predict(self, dt, accel):
        if dt <= 0:
            return
        self.p += self.v * dt
        q = accel ** 2
        P = self.P
        p00 = P[0][0] + dt * (P[1][0] + P[0][1]) + dt * dt * P[1][1] + q * dt ** 4 / 4
        p01 = P[0][1] + dt * P[1][1] + q * dt ** 3 / 2
        p11 = P[1][1] + q * dt * dt
        self.P = [[p00, p01], [p01, p11]]

    def update(self, z, meas_std):
        P = self.P
        s = P[0][0] + meas_std ** 2
        k0, k1 = P[0][0] / s, P[1][0] / s
        y = z - self.p
        self.p += k0 * y
        self.v += k1 * y
        self.P = [[(1 - k0) * P[0][0], (1 - k0) * P[0][1]],
                  [P[1][0] - k1 * P[0][0], P[1][1] - k1 * P[0][1]]]


class Track:
    _next_id = 1

    def __init__(self, t, now, meas_std):
        self.id = Track._next_id
        Track._next_id += 1
        self.kx, self.ky = Axis(t.x_mm, meas_std), Axis(t.y_mm, meas_std)
        self.raw_x, self.raw_y = t.x_mm, t.y_mm
        self.speed = t.speed_cms
        self.hits = deque([1], maxlen=C.TRACK_CONFIRM_N)
        self.misses = 0
        self.born = now
        self.last_seen = now
        self.t_pred = now
        self.confirmed_at = None
        self.jumps = 0
        self.kind = "verify"          # verify | person | object  (set by the app from vision)
        self.kind_t = now
        self.obj_label = ""
        self.anchor = (t.x_mm, t.y_mm)  # where it was when marked object

    x = property(lambda self: self.kx.p)
    y = property(lambda self: self.ky.p)

    @property
    def r_m(self):
        return math.hypot(self.x, self.y) / 1000.0

    @property
    def confirmed(self):
        return sum(self.hits) >= C.TRACK_CONFIRM_M

    def range_rate(self):
        """m/s from the filtered velocity, negative = approaching (independent of the speed sign)."""
        r = math.hypot(self.x, self.y)
        if r < 1:
            return 0.0
        return (self.x * self.kx.v + self.y * self.ky.v) / r / 1000.0

    def as_dict(self, xs=1):
        return {"id": self.id, "x_mm": round(self.x), "y_mm": round(self.y), "xr_mm": round(xs * self.x),
                "raw_x_mm": self.raw_x, "raw_y_mm": self.raw_y, "speed_cms": self.speed,
                "vx_mms": round(xs * self.kx.v), "vy_mms": round(self.ky.v),
                "r_m": round(self.r_m, 3), "range_rate_ms": round(self.range_rate(), 2),
                "confirmed": self.confirmed, "hits": sum(self.hits), "kind": self.kind, "obj": self.obj_label,
                "age_s": round(time.monotonic() - self.born, 1)}


class Tracker:
    def __init__(self):
        self.tracks: list[Track] = []
        self.primary: Track | None = None
        self.drop_s = 1.0
        self.prefer_id = None          # track the camera currently sees (vision priority)
        self._switch = (None, 0.0)     # (candidate track, since)
        self.margin, self.hold = 0.5, 0.8

    def update(self, targets, s: dict, now=None):
        now = now or time.monotonic()
        gate = s.get("gate_m", 0.6) * 1000.0
        self.drop_s = s.get("track_drop_s", 1.0)
        use_kf = s.get("use_kalman", True)
        accel = s.get("kf_accel_mps2", 1.0) * 1000.0
        mstd = s.get("kf_meas_mm", 100.0)
        alpha = s.get("pos_alpha", 0.5)
        maxr = s.get("max_range_m", 6.0) * 1000.0
        minr = s.get("min_range_m", 0.0) * 1000.0
        meas = [t for t in targets if t.valid and minr <= math.hypot(t.x_mm, t.y_mm) <= maxr]
        meas = merge_points(meas, s.get("merge_m", 0.35) * 1000.0)
        jump = s.get("jump_m", 0.35) * 1000.0
        self.margin, self.hold = s.get("switch_margin_m", 0.5), s.get("switch_hold_s", 0.8)
        for tr in self.tracks:                       # predict to this frame
            if use_kf:
                dt = now - tr.t_pred
                tr.kx.predict(dt, accel)
                tr.ky.predict(dt, accel)
            tr.t_pred = now
        pairs = sorted(((math.hypot(tr.x - m.x_mm, tr.y - m.y_mm), i, j)
                        for i, tr in enumerate(self.tracks) for j, m in enumerate(meas)), key=lambda p: p[0])
        used_t, used_m = set(), set()
        for d, i, j in pairs:
            if d > gate or i in used_t or j in used_m:
                continue
            used_t.add(i); used_m.add(j)
            tr, m = self.tracks[i], meas[j]
            if d > jump:
                tr.jumps += 1
            else:
                tr.jumps = 0
            if use_kf:
                if tr.jumps >= 3:                     # it really moved: accept the new position
                    tr.kx.p, tr.ky.p = float(m.x_mm), float(m.y_mm)
                    tr.jumps = 0
                ms = mstd * (3.0 if tr.jumps else 1.0)
                tr.kx.update(m.x_mm, ms)
                tr.ky.update(m.y_mm, ms)
            else:
                tr.kx.p += alpha * (m.x_mm - tr.kx.p)
                tr.ky.p += alpha * (m.y_mm - tr.ky.p)
                tr.kx.v = tr.ky.v = 0.0
            tr.raw_x, tr.raw_y, tr.speed = m.x_mm, m.y_mm, m.speed_cms
            tr.hits.append(1)
            tr.misses = 0
            tr.last_seen = now
        for i, tr in enumerate(self.tracks):
            if i not in used_t:
                tr.hits.append(0)
                tr.misses += 1
        for j, m in enumerate(meas):
            if j not in used_m:
                self.tracks.append(Track(m, now, mstd))
        self.expire(now)
        for tr in self.tracks:
            if tr.confirmed and tr.confirmed_at is None:
                tr.confirmed_at = now
        self._select()

    def expire(self, now=None):
        now = now or time.monotonic()
        self.tracks = [t for t in self.tracks if now - t.last_seen < self.drop_s]
        if self.primary is not None and self.primary not in self.tracks:
            self.primary = None
            self._select()

    def _score(self, t):
        sc = t.r_m - (0.5 if t.range_rate() < -0.15 else 0.0)
        if t.kind == "person":
            sc -= 1.0
        if self.prefer_id is not None and t.id == self.prefer_id:
            sc -= 3.0
        return sc

    def _select(self):
        now = time.monotonic()
        cands = [t for t in self.tracks if t.confirmed and t.kind != "object"]
        if not cands:
            self.primary = None
            self._switch = (None, 0.0)
            return
        best = min(cands, key=self._score)
        cur = self.primary if (self.primary in cands) else None
        if cur is None:
            self.primary = best
            self._switch = (None, 0.0)
            return
        if best is cur or self._score(best) >= self._score(cur) - self.margin:
            self._switch = (None, 0.0)
            return
        if best.id == self.prefer_id:              # the camera sees this one: switch at once
            self.primary = best
            return
        cand, since = self._switch
        if cand is not best:
            self._switch = (best, now)
        elif now - since >= self.hold:
            self.primary = best
            self._switch = (None, 0.0)

    def reselect(self):
        self._select()


class _Merged:
    __slots__ = ("x_mm", "y_mm", "speed_cms", "valid", "index", "res_mm")

    def __init__(self, pts):
        n = len(pts)
        self.x_mm = sum(p.x_mm for p in pts) / n
        self.y_mm = sum(p.y_mm for p in pts) / n
        self.speed_cms = max((p.speed_cms for p in pts), key=abs)
        self.valid = True
        self.index = pts[0].index if hasattr(pts[0], "index") else 0
        self.res_mm = getattr(pts[0], "res_mm", 0)


def merge_points(meas, merge_mm):
    """Greedy clustering of radar points closer than merge_mm (one person's limbs)."""
    if merge_mm <= 0 or len(meas) < 2:
        return meas
    left, out = list(meas), []
    while left:
        seed = left.pop(0)
        grp = [seed] + [m for m in left if math.hypot(m.x_mm - seed.x_mm, m.y_mm - seed.y_mm) <= merge_mm]
        left = [m for m in left if m not in grp]
        out.append(seed if len(grp) == 1 else _Merged(grp))
    return out


def _selftest():
    class T:
        def __init__(s, x, y): s.x_mm, s.y_mm, s.speed_cms, s.valid = x, y, 0, True
    tr = Tracker()
    t0 = 100.0
    for i in range(30):   # walking 1 m/s to the right at 20 Hz
        tr.update([T(-1000 + 50 * i, 2000)], {}, t0 + i * 0.05)
    p = tr.primary
    assert p and abs(p.x - 450) < 60 and abs(p.kx.v - 1000) < 250, (p.x, p.kx.v)
    # waving hand: one frame 0.6 m off must not drag the track
    x0 = p.x
    tr.update([T(-1000 + 50 * 30 + 600, 2000)], {}, t0 + 30 * 0.05)
    assert abs(tr.primary.x - x0) < 300, tr.primary.x
    # two points of one person are merged
    tr2 = Tracker()
    for i in range(12):
        tr2.update([T(0, 2000), T(200, 2100)], {}, t0 + i * 0.05)
    assert len(tr2.tracks) == 1, len(tr2.tracks)
    print("tracker selftest OK", round(p.x), round(p.kx.v))


if __name__ == "__main__":
    _selftest()
