#!/usr/bin/env python3
"""ETEC306 Wi-Fi setup mode (runs as root via etec306-wifi-setup.service).

1. scans nearby networks (client mode), 2. starts a NetworkManager hotspot "ETEC306-Setup-XXXX"
(WPA2, random 8-digit password shown on the OLED), 3. captive portal on port 80 (all DNS names ->
10.42.0.1 through NM's shared dnsmasq), 4. user picks an SSID + password -> saved with nmcli, Pi goes
back to client mode and the OLED shows the new IP. The app can also submit credentials read from a
Wi-Fi QR code shown to the camera (POST /api/connect from 127.0.0.1).
Safety: after SETUP_TIMEOUT s (default 600) without success the hotspot is removed and the previous
connection is brought back; the hotspot profile is never autoconnect, saved client profiles are untouched.
"""
import html
import json
import os
import random
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

RUN = "/run/etec306-wifi"
STATUS = RUN + "/status.json"
CON = "etec306-setup"
DNSCONF = "/etc/NetworkManager/dnsmasq-shared.d/etec306-captive.conf"
AP_IP = "10.42.0.1"
TIMEOUT = float(os.environ.get("SETUP_TIMEOUT", "600"))
IFACE = os.environ.get("WIFI_IFACE", "wlan0")

state = {"active": True, "ssid": "", "password": "", "ip": AP_IP, "msg": "", "networks": [], "prev": None,
         "started": time.time(), "until": time.time() + TIMEOUT}
done = threading.Event()
lock = threading.Lock()


def sh(*cmd, timeout=40):
    r = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def write_status(**kw):
    with lock:
        state.update(kw)
        os.makedirs(RUN, exist_ok=True)
        pub = {k: v for k, v in state.items() if k not in ("networks",)}
        tmp = STATUS + ".tmp"
        with open(tmp, "w") as f:
            json.dump(pub, f)
        os.chmod(tmp, 0o644)
        os.replace(tmp, STATUS)


def scan():
    sh("nmcli", "dev", "wifi", "rescan", "ifname", IFACE, timeout=15)
    time.sleep(3)
    _, out = sh("nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "ifname", IFACE)
    nets = {}
    for line in out.splitlines():
        parts = line.replace("\\:", "\x00").split(":")
        if len(parts) < 3 or not parts[0]:
            continue
        ssid = parts[0].replace("\x00", ":")
        sig = int(parts[1] or 0)
        if ssid.startswith("ETEC306-Setup"):
            continue
        if ssid not in nets or nets[ssid]["signal"] < sig:
            nets[ssid] = {"ssid": ssid, "signal": sig, "security": parts[2].replace("\x00", ":")}
    return sorted(nets.values(), key=lambda n: -n["signal"])


def active_wifi():
    _, out = sh("nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "con", "show", "--active")
    for line in out.splitlines():
        name, typ, dev = (line.rsplit(":", 2) + ["", ""])[:3]
        if dev == IFACE and "wireless" in typ and name != CON:
            return name
    return None


def ap_up():
    mac = open(f"/sys/class/net/{IFACE}/address").read().strip().replace(":", "")[-4:].upper()
    ssid, pw = f"ETEC306-Setup-{mac}", f"{random.randint(0, 99999999):08d}"
    os.makedirs(os.path.dirname(DNSCONF), exist_ok=True)
    with open(DNSCONF, "w") as f:
        f.write(f"address=/#/{AP_IP}\n")          # captive portal: every name resolves to the Pi
    sh("nmcli", "con", "delete", CON)
    rc, out = sh("nmcli", "con", "add", "type", "wifi", "ifname", IFACE, "con-name", CON, "autoconnect", "no",
                 "ssid", ssid, "mode", "ap", "802-11-wireless.band", "bg", "ipv4.method", "shared",
                 "ipv4.addresses", f"{AP_IP}/24", "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", pw)
    if rc == 0:
        rc, out = sh("nmcli", "con", "up", CON)
    write_status(ssid=ssid, password=pw, msg="" if rc == 0 else "hotspot failed: " + out[-200:], mode="ap")
    return rc == 0


def ap_down():
    sh("nmcli", "con", "down", CON)
    sh("nmcli", "con", "delete", CON)
    try:
        os.remove(DNSCONF)
    except OSError:
        pass


def restore():
    prev = state.get("prev")
    if prev:
        sh("nmcli", "con", "up", prev, timeout=60)


def ip_of_iface():
    _, out = sh("nmcli", "-g", "IP4.ADDRESS", "dev", "show", IFACE)
    return out.split("/")[0].strip() if out else ""


def connect(ssid, password):
    """Leave AP mode, join ssid; on failure go back to AP mode with an error message."""
    write_status(msg=f"connecting to {ssid} ...", mode="connecting")
    ap_down()
    time.sleep(2)
    sh("nmcli", "dev", "wifi", "rescan", "ifname", IFACE, timeout=15)
    time.sleep(4)
    args = ["nmcli", "dev", "wifi", "connect", ssid, "ifname", IFACE]
    if password:
        args += ["password", password]
    rc, out = sh(*args, timeout=60)
    ip = ip_of_iface() if rc == 0 else ""
    if rc == 0 and ip:
        write_status(active=False, mode="client", msg=f"connected to {ssid}", result_ssid=ssid, result_ip=ip,
                     result_t=time.time(), password="")
        done.set()
        return
    write_status(msg=f"could not join {ssid}: {out[-160:]}")
    ap_up()


PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ETEC306 Wi-Fi setup</title><style>body{{font-family:sans-serif;margin:12px;background:#eee}} td{{padding:3px 6px}}
input,select,button{{font-size:16px}}</style></head><body>
<h3>ETEC306 Wi-Fi setup / 配网</h3>
<p>{msg}</p>
<form method="post" action="/connect">
<p>Network / 网络:<br><select name="ssid">{opts}</select><br>
or type / 或手动输入: <input name="ssid2" size="20"></p>
<p>Password / 密码:<br><input name="password" type="password" size="20"></p>
<button type="submit">Connect / 连接</button></form>
<p><small>Tip / 提示: you can also show a Wi-Fi QR code (phone: Wi-Fi settings → share) to the ETEC306 camera.<br>
也可以把手机“分享 Wi-Fi”生成的二维码对准 ETEC306 摄像头。<br>Setup mode ends automatically at {until}.</small></p>
</body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def page(self):
        opts = "".join(f'<option value="{html.escape(n["ssid"])}">{html.escape(n["ssid"])} ({n["signal"]}%{" 🔒" if n["security"] else ""})</option>'
                       for n in state["networks"]) or '<option value="">(no networks found)</option>'
        self._send(200, PAGE.format(msg=html.escape(state.get("msg") or ""), opts=opts,
                                    until=time.strftime("%H:%M", time.localtime(state["until"]))))

    def do_GET(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in (AP_IP, "127.0.0.1", "localhost", ""):
            # captive-portal detection (Android generate_204, Apple hotspot-detect, Windows ncsi): redirect
            return self._send(302, "", extra={"Location": f"http://{AP_IP}/"})
        if self.path.startswith("/api/status"):
            return self._send(200, json.dumps({k: v for k, v in state.items() if k != "password"}), "application/json")
        self.page()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(n, 4096)).decode(errors="replace")
        if self.path == "/api/connect":
            if self.client_address[0] != "127.0.0.1":
                return self._send(403, "local only", "text/plain")
            d = json.loads(raw or "{}")
            ssid, pw = str(d.get("ssid", ""))[:32], str(d.get("password", ""))[:63]
        else:
            q = parse_qs(raw)
            ssid = (q.get("ssid2", [""])[0].strip() or q.get("ssid", [""])[0]).strip()[:32]
            pw = q.get("password", [""])[0][:63]
        if not ssid:
            state["msg"] = "Pick a network first / 请先选网络"
            return self.page()
        threading.Thread(target=connect, args=(ssid, pw), daemon=True).start()
        self._send(200, f"<html><body style='font-family:sans-serif'><h3>Connecting to {html.escape(ssid)} ...</h3>"
                        "<p>The hotspot will disappear. Look at the ETEC306 OLED for the new IP address.<br>"
                        "热点会消失，新 IP 地址显示在 ETEC306 的 OLED 上。</p></body></html>")


def main():
    write_status(active=True, msg="starting", mode="starting")
    state["prev"] = active_wifi()
    state["networks"] = scan()
    srv = ThreadingHTTPServer(("0.0.0.0", 80), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        if not ap_up():
            done.set()
        while not done.wait(1.0):
            if time.time() > state["until"]:
                write_status(msg="timeout, back to the previous network")
                break
    finally:
        srv.shutdown()
        if state.get("mode") != "client":
            ap_down()
            restore()
            write_status(active=False, mode="client", password="", result_ip=ip_of_iface(),
                         result_ssid=active_wifi() or "", result_t=time.time())


if __name__ == "__main__":
    main()
