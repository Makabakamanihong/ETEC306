#!/usr/bin/env python3
"""ETEC306 cloud logger (技术方案.md 8.3 offline fallback for InfluxDB/Grafana).

Subscribes home/# on the local broker and stores:
  data/YYYY-MM-DD.jsonl      status / event / cmd messages (all) + track (down-sampled to 1 Hz)
  data/images/<event_id>.jpg alarm snapshots
  data/latest.json           last status / event / track, for a quick look
Optional push notification for alarms via notify.json (ntfy / Telegram). Disabled unless
notify.json exists and has "enabled": true -- nothing is sent anywhere by default.
Old files are deleted after RETAIN_DAYS."""
import json, os, re, sys, time, threading, urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mqttcfg import make_client

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
IMG = DATA / "images"
RETAIN_DAYS = 30
IMG.mkdir(parents=True, exist_ok=True)
latest = {}
last_track = 0.0
lock = threading.Lock()


def log(msg):
    print(datetime.now().strftime("%H:%M:%S"), msg, flush=True)


def append(rec):
    with lock, open(DATA / f"{datetime.now():%Y-%m-%d}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def notify(ev):
    p = BASE / "notify.json"
    if not p.exists():
        return
    try:
        n = json.load(open(p))
        if not n.get("enabled"):
            return
        text = f"ETEC306 {ev.get('type')}: {ev.get('text', '')} ({ev.get('ts')})"
        if n.get("type") == "ntfy":
            urllib.request.urlopen(urllib.request.Request(n["url"], data=text.encode(), method="POST"), timeout=10)
        elif n.get("type") == "telegram":
            u = f"https://api.telegram.org/bot{n['token']}/sendMessage"
            body = json.dumps({"chat_id": n["chat_id"], "text": text}).encode()
            urllib.request.urlopen(urllib.request.Request(u, data=body, headers={"Content-Type": "application/json"}), timeout=10)
        log(f"notified {n.get('type')}")
    except Exception as exc:
        log(f"notify failed: {exc}")


def on_connect(cl, ud, flags, rc):
    log(f"connected rc={rc}")
    cl.subscribe("home/#", qos=1)


def on_message(cl, ud, msg):
    global last_track
    now = time.time()
    t = msg.topic
    try:
        if "/image/" in t:
            eid = re.sub(r"[^\w\-]", "_", t.rsplit("/", 1)[-1])
            (IMG / f"{eid}.jpg").write_bytes(msg.payload)
            append({"rx": datetime.now().isoformat(timespec="milliseconds"), "topic": t, "image": f"images/{eid}.jpg",
                    "bytes": len(msg.payload)})
            log(f"image {eid} {len(msg.payload)} B")
            return
        try:
            payload = json.loads(msg.payload.decode())
        except Exception:
            payload = msg.payload.decode(errors="replace")
        kind = t.rsplit("/", 1)[-1]
        latest[kind] = {"rx": datetime.now().isoformat(timespec="seconds"), "payload": payload}
        (DATA / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=1))
        if kind == "track":
            if now - last_track < 1.0:
                return
            last_track = now
        append({"rx": datetime.now().isoformat(timespec="milliseconds"), "topic": t, "retain": bool(msg.retain),
                "payload": payload})
        if kind == "event":
            log(f"event {payload.get('type')} {payload.get('text', '')}")
            if payload.get("type") == "alarm":
                threading.Thread(target=notify, args=(payload,), daemon=True).start()
        elif kind == "status" and isinstance(payload, dict) and not payload.get("online", True):
            log("device offline (LWT / shutdown)")
    except Exception as exc:
        log(f"error on {t}: {exc}")


def cleanup():
    while True:
        cut = time.time() - RETAIN_DAYS * 86400
        for f in list(DATA.glob("*.jsonl")) + list(IMG.glob("*.jpg")):
            if f.stat().st_mtime < cut:
                f.unlink()
        time.sleep(3600)


def main():
    cl, cfg = make_client(BASE / "credentials" / "cloud.json", "etec306-cloud-logger")
    cl.on_connect = on_connect
    cl.on_message = on_message
    threading.Thread(target=cleanup, daemon=True).start()
    while True:
        try:
            cl.connect(cfg["host"], int(cfg["port"]), keepalive=30)
            break
        except Exception as exc:
            log(f"connect failed: {exc}; retry in 5 s")
            time.sleep(5)
    cl.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    main()
