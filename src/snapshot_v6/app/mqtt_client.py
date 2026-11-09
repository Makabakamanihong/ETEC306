"""MQTT link to the cloud broker (技术方案.md 8.1): TLS 8883, username/password.

Topics (DEVICE_ID = etec306):
  home/etec306/status   device -> cloud  retained; heartbeat every 10 s (armed, state, pan/tilt, IP)
                                         LWT {"online": false}
  home/etec306/track    device -> cloud  primary target x, y, v_r, theta; <= 2 Hz
  home/etec306/event    device -> cloud  QoS 1; alarm / known / snapshot / arm events (metadata only)
  home/etec306/image/<event_id>  device -> cloud  QoS 1; JPEG bytes for unknown-person alarms only
  home/etec306/cmd      cloud -> device  {"action": "arm|disarm|center|snapshot|pause|resume"}
Config comes from mqtt.json (not in the repo); missing file = MQTT disabled.
Reconnect uses paho's exponential back-off (1..60 s). QoS-1 messages published while offline
are kept in a small queue and flushed after the next CONNACK (T6: last alarm is not lost).
"""
from __future__ import annotations

import json
import ssl
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import config as C

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

ROOT = Path(__file__).resolve().parent
ALLOWED_CMDS = {"arm", "disarm", "toggle_arm", "center", "snapshot", "pause", "resume", "clear_alarm"}


class MqttLink:
    def __init__(self, on_cmd, on_event=None):
        self.on_cmd = on_cmd
        self.log_event = on_event or (lambda k, t: None)
        self.cfg = None
        self.client = None
        self.connected = False
        self.last_connack = None
        self.last_heartbeat = None
        self.last_error = ""
        self.sent = 0
        self.queue = deque(maxlen=50)
        self.wa = {}            # "qr" / "status" from the WhatsApp bot (retained topics)
        self.lock = threading.Lock()
        path = ROOT / C.MQTT_CONFIG
        if mqtt is None:
            self.last_error = "paho-mqtt not installed"
            return
        if not path.exists():
            self.last_error = f"{C.MQTT_CONFIG} missing (MQTT disabled)"
            return
        self.cfg = json.loads(path.read_text())
        self.base = f"home/{self.cfg.get('device_id', C.DEVICE_ID)}"

    @property
    def enabled(self):
        return self.cfg is not None

    def start(self):
        if not self.enabled:
            return
        c = self.cfg
        try:
            cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{C.DEVICE_ID}-pi", clean_session=True)
        except AttributeError:       # paho 1.x
            cl = mqtt.Client(client_id=f"{C.DEVICE_ID}-pi", clean_session=True)
        cl.username_pw_set(c["username"], c["password"])
        if c.get("tls", True):
            cl.tls_set(ca_certs=str(ROOT / c.get("ca", "mqtt_ca.crt")), tls_version=ssl.PROTOCOL_TLS_CLIENT)
        cl.will_set(f"{self.base}/status", json.dumps({"online": False}), qos=1, retain=True)
        cl.reconnect_delay_set(1, 60)
        cl.max_queued_messages_set(200)
        cl.on_connect = self._on_connect
        cl.on_disconnect = self._on_disconnect
        cl.on_message = self._on_message
        self.client = cl
        cl.connect_async(c["host"], int(c.get("port", 8883)), keepalive=30)
        cl.loop_start()

    def stop(self, status_payload=None):
        if self.client:
            try:
                if status_payload is not None and self.connected:
                    self.client.publish(f"{self.base}/status", json.dumps(status_payload), qos=1, retain=True).wait_for_publish(2)
                self.client.disconnect()
                self.client.loop_stop()
            except Exception:
                pass

    # paho callbacks (v1/v2 compatible signatures)
    def _on_connect(self, client, userdata, flags, rc, props=None):
        ok = (rc == 0) if isinstance(rc, int) else (not rc.is_failure)
        if not ok:
            self.last_error = f"CONNACK refused: {rc}"
            return
        self.connected = True
        self.last_connack = datetime.now().strftime("%H:%M:%S")
        self.last_error = ""
        client.subscribe(f"{self.base}/cmd", qos=1)
        client.subscribe(f"{self.base}/wa/#", qos=0)     # self-hosted WhatsApp bot: pairing QR + status
        self.log_event("MQTT", f"已连接 {self.cfg['host']}:{self.cfg.get('port', 8883)}")
        with self.lock:
            pending, self.queue = list(self.queue), deque(maxlen=50)
        for topic, payload, qos, retain in pending:
            client.publish(topic, payload, qos=qos, retain=retain)

    def _on_disconnect(self, client, userdata, *args):
        if self.connected:
            self.log_event("MQTT", "断开，自动重连中")
        self.connected = False

    def _on_message(self, client, userdata, msg):
        if msg.topic.startswith(f"{self.base}/wa/"):
            try:
                self.wa[msg.topic.rsplit("/", 1)[-1]] = json.loads(msg.payload.decode())
            except Exception:
                pass
            return
        try:
            data = json.loads(msg.payload.decode())
            action = str(data.get("action", ""))
            if action not in ALLOWED_CMDS:
                raise ValueError(f"cmd not allowed: {action}")
            self.log_event("远程命令", f"{action} (MQTT)")
            self.on_cmd(action)
        except Exception as exc:
            self.log_event("远程命令错误", str(exc))

    def publish(self, sub, payload, qos=0, retain=False, queue_if_offline=False):
        if not self.enabled or self.client is None:
            return False
        topic = f"{self.base}/{sub}"
        if not isinstance(payload, (bytes, bytearray)):
            payload = json.dumps(payload, ensure_ascii=False)
        if not self.connected:
            if queue_if_offline:
                with self.lock:
                    self.queue.append((topic, payload, qos, retain))
            return False
        info = self.client.publish(topic, payload, qos=qos, retain=retain)
        if info.rc != 0:
            if queue_if_offline:
                with self.lock:
                    self.queue.append((topic, payload, qos, retain))
            return False
        self.sent += 1
        return True

    def status(self):
        return {"enabled": self.enabled, "connected": self.connected, "host": self.cfg.get("host") if self.cfg else None,
                "last_connack": self.last_connack, "last_heartbeat": self.last_heartbeat,
                "sent": self.sent, "queued": len(self.queue), "error": self.last_error,
                "topics": f"{self.base}/#" if self.enabled else None}
