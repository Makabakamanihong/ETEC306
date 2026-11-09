#!/usr/bin/env python3
"""Offline statistics from the logger's JSONL (技术方案.md 8.3):  python3 etec306_stats.py [days=7]
Per day: heartbeats (≈ online minutes), alarms, known-person visits, snapshots, arm/disarm, track samples."""
import json, sys
from collections import Counter, defaultdict
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
files = sorted(DATA.glob("*.jsonl"))[-days:]
print(f"{'date':<12}{'online_min':>11}{'alarm':>7}{'known':>7}{'snap':>6}{'arm':>5}{'track':>7}  known names")
for f in files:
    c, names, hb = Counter(), Counter(), 0
    for line in f.open(encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        kind = r["topic"].rsplit("/", 1)[-1] if "/image/" not in r["topic"] else "image"
        p = r.get("payload")
        if kind == "status" and isinstance(p, dict) and p.get("online"):
            hb += 1
        elif kind == "event" and isinstance(p, dict):
            c[p.get("type")] += 1
            if p.get("type") == "known":
                names[p.get("name")] += 1
        elif kind == "track":
            c["track"] += 1
    print(f"{f.stem:<12}{hb * 10 / 60:>11.1f}{c['alarm']:>7}{c['known']:>7}{c['snapshot']:>6}{c['arm']:>5}{c['track']:>7}  "
          + ", ".join(f"{k}×{v}" for k, v in names.items()))
