#!/bin/bash
# run as opc from the folder with bot.js etc.
set -e
sudo cp -n /etc/mosquitto/passwd /etc/mosquitto/passwd.bak-wa; sudo cp -n /etc/mosquitto/acl /etc/mosquitto/acl.bak-wa0
command -v node >/dev/null || { sudo dnf -y module reset nodejs; sudo dnf -y module enable nodejs:22; sudo dnf -y install nodejs npm; }
id etecwa >/dev/null 2>&1 || sudo useradd --system --home-dir /opt/etec306-wa --shell /sbin/nologin etecwa
sudo mkdir -p /opt/etec306-wa/state
sudo cp bot.js package.json /opt/etec306-wa/
sudo cp /etc/mosquitto/certs/ca.crt /opt/etec306-wa/ca.crt
sudo install -m 755 etec306-wa-relink /usr/local/bin/etec306-wa-relink
if ! sudo test -f /opt/etec306-wa/config.json; then
  PW=$(openssl rand -hex 24)
  sudo mosquitto_passwd -b /etc/mosquitto/passwd etec306-wa "$PW"
  sudo tee /opt/etec306-wa/config.json >/dev/null <<J
{"device": "etec306", "mqtt_url": "mqtts://127.0.0.1:8883", "mqtt_user": "etec306-wa", "mqtt_password": "$PW",
 "recipients": [], "types": ["alarm"], "min_interval_s": 60, "send_image": true, "pair_phone": ""}
J
  unset PW
fi
if ! sudo grep -q "^user etec306-wa$" /etc/mosquitto/acl; then
  sudo cp /etc/mosquitto/acl /etc/mosquitto/acl.bak-wa
  printf '\n# WhatsApp forwarder: read-only events + images\nuser etec306-wa\ntopic read home/etec306/event\ntopic read home/etec306/image/#\n' | sudo tee -a /etc/mosquitto/acl >/dev/null
fi
sudo chown -R etecwa:etecwa /opt/etec306-wa
sudo chmod 600 /opt/etec306-wa/config.json
sudo chmod 700 /opt/etec306-wa/state
sudo systemctl reload mosquitto || sudo kill -HUP $(pidof mosquitto)
cd /opt/etec306-wa && sudo -u etecwa HOME=/opt/etec306-wa npm install --omit=dev --no-audit --no-fund 2>&1 | tail -3
sudo cp /home/opc/etec306/wa/etec306-wa.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now etec306-wa
echo installed
