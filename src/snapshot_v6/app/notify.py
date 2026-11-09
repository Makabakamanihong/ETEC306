"""Alarm notifications to the owner's phone (技术方案.md 8.2): Telegram Bot API (text + photo) and two
WhatsApp options, all off by default. Sent from the Pi itself:
  - the alarm picture is already on the Pi (no upload to the cloud and back),
  - the "send test" button gives an immediate, real result in the settings page,
  - works even if the MQTT/cloud link is down (only needs internet).
Secrets live only in notify.json (chmod 600, never returned by the API, never logged; error texts are
scrubbed of the token before they are stored).

notify.json (all fields empty / false by default):
  enabled            master switch
  min_interval_s     at most one alarm notification per this many seconds
  send_photo         attach the alarm snapshot where the channel supports it
  telegram           {enabled, bot_token, chat_id}
  whatsapp_callmebot {enabled, phone, apikey}           free, text only, personal use
  whatsapp_cloud     {enabled, access_token, phone_number_id, to, api_version, mode, template, lang,
                      template_param}                    Meta WhatsApp Cloud API (business account)
  ntfy               {enabled, server, topic, token, priority}       ntfy.sh or self-hosted; photo as attachment
  email              {enabled, host, port, security, user, password, from_addr, to}   SMTP with snapshot attached
  discord            {enabled, webhook_url}                          Discord channel webhook (text + image)
"""
from __future__ import annotations

import copy
import json
import os
import queue
import threading
import time
from pathlib import Path

try:
    import requests
except ImportError:          # the app still runs, notifications report the error
    requests = None

PATH = Path(__file__).resolve().parent / "notify.json"
SECRET_KEYS = {"bot_token", "apikey", "access_token", "token", "password", "webhook_url"}
DEFAULT = {
    "enabled": False,
    "min_interval_s": 60,
    "send_photo": True,
    "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
    "whatsapp_callmebot": {"enabled": False, "phone": "", "apikey": ""},
    "whatsapp_cloud": {"enabled": False, "access_token": "", "phone_number_id": "", "to": "",
                       "api_version": "v25.0", "mode": "template", "template": "", "lang": "zh_CN",
                       "template_param": True},
    "ntfy": {"enabled": False, "server": "https://ntfy.sh", "topic": "", "token": "", "priority": "high"},
    "email": {"enabled": False, "host": "smtp.gmail.com", "port": "587", "security": "starttls", "user": "",
              "password": "", "from_addr": "", "to": ""},
    "discord": {"enabled": False, "webhook_url": ""},
}
CHANNELS = ("telegram", "whatsapp_callmebot", "whatsapp_cloud", "ntfy", "email", "discord")
CN = {"telegram": "Telegram", "whatsapp_callmebot": "WhatsApp (CallMeBot)", "whatsapp_cloud": "WhatsApp Cloud API",
      "ntfy": "ntfy", "email": "Email (SMTP)", "discord": "Discord"}
TIMEOUT = 15


def merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = merge(out[k], v)
        elif k in out:
            out[k] = type(out[k])(v) if not isinstance(out[k], bool) else bool(v)
    return out


class Notifier:
    def __init__(self, path=PATH, log=None):
        self.path = Path(path)
        self.log = log or (lambda title, text: print(f"[notify] {title}: {text}", flush=True))
        self.lock = threading.Lock()
        self.cfg = self.load()
        self.q = queue.Queue(maxsize=5)
        self.last_sent = 0.0
        self.last = {}          # channel -> {"t", "ok", "msg"} (no secrets)
        threading.Thread(target=self.worker, daemon=True, name="notify").start()

    # ---------------------------------------------------------------- config
    def load(self):
        try:
            cfg = merge(DEFAULT, json.loads(self.path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            cfg = copy.deepcopy(DEFAULT)
            self.write(cfg)
        except Exception as exc:
            print("[notify] bad notify.json, using defaults:", exc, flush=True)
            cfg = copy.deepcopy(DEFAULT)
        return cfg

    def write(self, cfg):
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def public(self):
        """Config for the web page: secrets replaced by a 'set / not set' flag."""
        with self.lock:
            c = copy.deepcopy(self.cfg)
        for ch in CHANNELS:
            for k in list(c[ch]):
                if k in SECRET_KEYS:
                    c[ch][k + "_set"] = bool(c[ch][k])
                    c[ch][k] = ""
            c[ch]["missing"] = self.missing(ch, c=self.cfg)
        c["last"] = self.last
        c["has_requests"] = requests is not None
        return c

    def update(self, data):
        """Web form. An empty secret field keeps the stored value; {"clear": ["telegram.bot_token"]} erases."""
        with self.lock:
            cfg = copy.deepcopy(self.cfg)
            for k in ("enabled", "send_photo"):
                if k in data:
                    cfg[k] = bool(data[k])
            if "min_interval_s" in data:
                cfg["min_interval_s"] = max(0, min(3600, int(float(data["min_interval_s"]))))
            for ch in CHANNELS:
                for k, v in (data.get(ch) or {}).items():
                    if k not in cfg[ch]:
                        continue
                    if k in SECRET_KEYS and not str(v).strip():
                        continue
                    cfg[ch][k] = bool(v) if isinstance(cfg[ch][k], bool) else str(v).strip()[:400]
            for item in data.get("clear") or []:
                ch, _, k = str(item).partition(".")
                if ch in CHANNELS and k in cfg[ch]:
                    cfg[ch][k] = ""
            self.write(cfg)
            self.cfg = cfg
        return self.public()

    def missing(self, ch, c=None):
        c = (c or self.cfg)[ch]
        need = {"telegram": ["bot_token", "chat_id"], "whatsapp_callmebot": ["phone", "apikey"],
                "whatsapp_cloud": ["access_token", "phone_number_id", "to"], "ntfy": ["server", "topic"],
                "email": ["host", "port", "user", "password", "to"], "discord": ["webhook_url"]}[ch]
        if ch == "whatsapp_cloud" and c.get("mode") == "template":
            need.append("template")
        return [k for k in need if not str(c.get(k, "")).strip()]

    # ---------------------------------------------------------------- sending
    def scrub(self, text):
        s = str(text)
        with self.lock:
            for ch in CHANNELS:
                for k in SECRET_KEYS:
                    v = self.cfg[ch].get(k)
                    if v:
                        s = s.replace(v, "***")
        return s[:300]

    def alarm(self, text, jpg=None):
        """Called by the app on an alarm. Non-blocking; rate-limited; no-op when disabled."""
        with self.lock:
            cfg = self.cfg
            if not cfg["enabled"] or not any(cfg[ch]["enabled"] for ch in CHANNELS):
                return False
            now = time.monotonic()
            if now - self.last_sent < cfg["min_interval_s"]:
                return False
            self.last_sent = now
        try:
            self.q.put_nowait((text, jpg if cfg["send_photo"] else None))
            return True
        except queue.Full:
            return False

    def worker(self):
        while True:
            text, jpg = self.q.get()
            for ch in CHANNELS:
                if self.cfg[ch]["enabled"]:
                    ok, msg = self.send(ch, text, jpg)
                    self.log("通知" if ok else "通知失败", f"{CN[ch]}: {msg}")

    def send(self, ch, text, jpg=None):
        """Synchronous send to one channel -> (ok, message). Never raises."""
        miss = self.missing(ch)
        if miss:
            res = (False, "未配置：缺 " + ", ".join(miss))
        elif requests is None and ch != "email":
            res = (False, "python3-requests 没装")
        else:
            try:
                res = getattr(self, "_" + ch)(self.cfg[ch], text, jpg)
            except Exception as exc:
                res = (False, f"{type(exc).__name__}: {exc}")
        res = (res[0], self.scrub(res[1]))
        self.last[ch] = {"t": time.strftime("%m-%d %H:%M:%S"), "ok": res[0], "msg": res[1]}
        return res

    def test(self, ch, jpg=None):
        if ch not in CHANNELS:
            return False, "未知通道"
        return self.send(ch, "ETEC306 test message " + time.strftime("%Y-%m-%d %H:%M:%S") + " (settings page \"Send test\")", jpg)

    # Telegram Bot API: https://core.telegram.org/bots/api#sendphoto
    def _telegram(self, c, text, jpg):
        base = f"https://api.telegram.org/bot{c['bot_token']}/"
        if jpg:
            r = requests.post(base + "sendPhoto", data={"chat_id": c["chat_id"], "caption": text[:1000]},
                              files={"photo": ("alarm.jpg", jpg, "image/jpeg")}, timeout=TIMEOUT)
        else:
            r = requests.post(base + "sendMessage", data={"chat_id": c["chat_id"], "text": text[:4000]},
                              timeout=TIMEOUT)
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.ok and j.get("ok"):
            return True, "已发送" + ("（带图）" if jpg else "")
        return False, f"HTTP {r.status_code} {j.get('description') or r.text[:120]}"

    def telegram_chats(self):
        """getUpdates -> chats that recently wrote to the bot (to find the chat_id). Needs only the token.
        Returns (ok, list or message)."""
        c = self.cfg["telegram"]
        if not c["bot_token"]:
            return False, "未配置：先填 bot_token 并保存"
        if requests is None:
            return False, "python3-requests 没装"
        try:
            r = requests.get(f"https://api.telegram.org/bot{c['bot_token']}/getUpdates", timeout=TIMEOUT)
            j = r.json()
        except Exception as exc:
            return False, self.scrub(f"{type(exc).__name__}: {exc}")
        if not j.get("ok"):
            return False, self.scrub(f"HTTP {r.status_code} {j.get('description')}")
        chats = {}
        for u in j.get("result", []):
            m = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
            ch = m.get("chat") or {}
            if "id" in ch:
                chats[ch["id"]] = {"chat_id": ch["id"], "type": ch.get("type"),
                                   "name": ch.get("title") or " ".join(x for x in (ch.get("first_name"), ch.get("last_name"), ("@" + ch["username"]) if ch.get("username") else None) if x),
                                   "text": (m.get("text") or "")[:40]}
        if not chats:
            return False, "没有消息：先在 Telegram 里给你的机器人发一句话（比如 /start），再点一次"
        return True, list(chats.values())

    # CallMeBot (free, personal use, text only): https://www.callmebot.com/blog/free-api-whatsapp-messages/
    def _whatsapp_callmebot(self, c, text, jpg):
        r = requests.get("https://api.callmebot.com/whatsapp.php",
                         params={"phone": c["phone"], "text": text[:900] + ("（图片在网页/云端）" if jpg else ""),
                                 "apikey": c["apikey"]}, timeout=TIMEOUT)
        body = r.text
        ok = r.ok and ("queued" in body.lower() or "sent" in body.lower())
        return ok, ("已提交（CallMeBot 只发文字）" if ok else f"HTTP {r.status_code} {strip_html(body)[:160]}")

    # Meta WhatsApp Cloud API: POST /{ver}/{phone_number_id}/messages (Bearer token)
    def _whatsapp_cloud(self, c, text, jpg):
        base = f"https://graph.facebook.com/{c['api_version'] or 'v25.0'}/{c['phone_number_id']}"
        h = {"Authorization": f"Bearer {c['access_token']}"}
        if c["mode"] == "template":
            # outside the 24 h customer-service window only APPROVED templates can be sent
            tpl = {"name": c["template"], "language": {"code": c["lang"] or "zh_CN"}}
            if c["template_param"]:
                tpl["components"] = [{"type": "body", "parameters": [{"type": "text", "text": text[:900]}]}]
            body = {"messaging_product": "whatsapp", "to": c["to"], "type": "template", "template": tpl}
        elif jpg:
            # free-form image: only inside the 24 h window (the user wrote to the business number recently)
            up = requests.post(base + "/media", headers=h, data={"messaging_product": "whatsapp", "type": "image/jpeg"},
                               files={"file": ("alarm.jpg", jpg, "image/jpeg")}, timeout=TIMEOUT)
            mid = up.json().get("id") if up.ok else None
            if not mid:
                return False, f"图片上传失败 HTTP {up.status_code} {up.text[:160]}"
            body = {"messaging_product": "whatsapp", "to": c["to"], "type": "image",
                    "image": {"id": mid, "caption": text[:900]}}
        else:
            body = {"messaging_product": "whatsapp", "to": c["to"], "type": "text", "text": {"body": text[:4000]}}
        r = requests.post(base + "/messages", headers=h, json=body, timeout=TIMEOUT)
        if r.ok and r.json().get("messages"):
            return True, "已提交 (" + c["mode"] + ")"
        try:
            err = r.json().get("error", {})
            msg = f"{err.get('code')} {err.get('message')}"
        except Exception:
            msg = r.text[:160]
        return False, f"HTTP {r.status_code} {msg}"

    # ntfy: PUT the image as an attachment (ntfy.sh: up to 15 MB, kept a few hours), message in the header
    # https://docs.ntfy.sh/publish/#attach-local-file
    def _ntfy(self, c, text, jpg):
        url = c["server"].rstrip("/") + "/" + c["topic"].strip("/")
        h = {"Title": "ETEC306", "Priority": c["priority"] or "default", "Tags": "rotating_light"}
        if c["token"]:
            h["Authorization"] = f"Bearer {c['token']}"
        if jpg:
            h["Filename"] = "alarm.jpg"
            # header values must be latin-1: non-ASCII goes through RFC 2047 so Chinese text survives
            import base64
            h["Message"] = "=?UTF-8?B?" + base64.b64encode(text[:1000].encode()).decode() + "?="
            h["Title"] = "ETEC306"
            r = requests.put(url, data=jpg, headers=h, timeout=TIMEOUT)
        else:
            r = requests.post(url, data=text[:3000].encode("utf-8"), headers=h, timeout=TIMEOUT)
        if r.ok:
            return True, "已发送" + ("（带图）" if jpg else "")
        return False, f"HTTP {r.status_code} {r.text[:160]}"

    # SMTP (e.g. Gmail: smtp.gmail.com 587 STARTTLS + a 16-character app password; 2-step verification needed)
    def _email(self, c, text, jpg):
        import smtplib
        import ssl
        from email.message import EmailMessage
        m = EmailMessage()
        m["Subject"] = "ETEC306 " + text[:80]
        m["From"] = c["from_addr"] or c["user"]
        m["To"] = c["to"]
        m.set_content(text)
        if jpg:
            m.add_attachment(jpg, maintype="image", subtype="jpeg", filename="alarm.jpg")
        port = int(c["port"] or 587)
        ctx = ssl.create_default_context()
        if c["security"] == "ssl" or port == 465:
            srv = smtplib.SMTP_SSL(c["host"], port, context=ctx, timeout=TIMEOUT)
        else:
            srv = smtplib.SMTP(c["host"], port, timeout=TIMEOUT)
            if c["security"] != "none":
                srv.starttls(context=ctx)
        with srv:
            srv.login(c["user"], c["password"])
            srv.send_message(m)
        return True, "已发送" + ("（带图）" if jpg else "")

    # Discord webhook: multipart with payload_json + file  https://discord.com/developers/docs/resources/webhook
    def _discord(self, c, text, jpg):
        url = c["webhook_url"]
        if not url.startswith("https://"):
            return False, "webhook_url 应以 https:// 开头"
        payload = json.dumps({"content": text[:1900], "username": "ETEC306"})
        if jpg:
            r = requests.post(url, data={"payload_json": payload}, files={"files[0]": ("alarm.jpg", jpg, "image/jpeg")},
                              timeout=TIMEOUT)
        else:
            r = requests.post(url, data={"payload_json": payload}, timeout=TIMEOUT)
        if r.ok:
            return True, "已发送" + ("（带图）" if jpg else "")
        return False, f"HTTP {r.status_code} {r.text[:160]}"


def strip_html(s):
    import re
    return re.sub(r"<[^>]+>", " ", s or "").strip()
