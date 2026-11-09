"""Shared helper: build a paho client (1.6 or 2.x) from a credentials json."""
import json, ssl
import paho.mqtt.client as mqtt


def make_client(cfg_path, client_id):
    c = json.load(open(cfg_path))
    try:
        cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id)
    except AttributeError:
        cl = mqtt.Client(client_id=client_id)
    cl.username_pw_set(c["username"], c["password"])
    if c.get("tls", True):
        cl.tls_set(ca_certs=c["ca"], tls_version=ssl.PROTOCOL_TLS_CLIENT)
    cl.reconnect_delay_set(1, 60)
    return cl, c
