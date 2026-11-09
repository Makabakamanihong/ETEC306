#!/usr/bin/env python3
"""Face enrollment script (技术方案 7.6: 录入用单独脚本). Runs on the Pi and talks to the running
service (the service owns the camera), so tracking keeps working.
  python3 tools/enroll.py add 张三 [samples=5]
  python3 tools/enroll.py list
  python3 tools/enroll.py delete 张三
Same thing as the 人脸录入 box on the web page."""
import json, sys, time, urllib.request

API = "http://127.0.0.1:8080"


def call(path, body=None):
    req = urllib.request.Request(API + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.load(e)


def main(a):
    if len(a) >= 2 and a[0] == "add":
        r = call("/api/enroll", {"name": a[1], "samples": int(a[2]) if len(a) > 2 else 5})
        if not r.get("ok"):
            sys.exit(f"error: {r.get('error')}")
        seen = 0
        while True:
            time.sleep(1)
            e = call("/api/state")["enroll"]
            for line in e["log"][seen:]:
                print(" ", line)
            seen = len(e["log"])
            if not e["running"]:
                print(e["msg"])
                break
    elif a[:1] == ["list"]:
        for p in call("/api/state")["gallery"]:
            print(f"{p['name']}\t{p['samples']} samples\t{p['time']}")
    elif len(a) == 2 and a[0] == "delete":
        print(call("/api/faces/delete", {"name": a[1]}))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
