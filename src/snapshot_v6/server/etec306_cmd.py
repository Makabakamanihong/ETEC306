#!/usr/bin/env python3
"""Send a remote command to the Pi:  python3 etec306_cmd.py arm|disarm|toggle_arm|center|snapshot|pause|resume|clear_alarm"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from mqttcfg import make_client

OK = {"arm", "disarm", "toggle_arm", "center", "snapshot", "pause", "resume", "clear_alarm"}
if len(sys.argv) != 2 or sys.argv[1] not in OK:
    sys.exit(__doc__)
cl, c = make_client(Path(__file__).resolve().parent / "credentials" / "cloud.json", "etec306-cmd")
cl.connect(c["host"], int(c["port"]), 30)
cl.loop_start()
cl.publish("home/etec306/cmd", json.dumps({"action": sys.argv[1]}), qos=1).wait_for_publish(5)
cl.disconnect(); cl.loop_stop()
print("sent", sys.argv[1])
