#!/bin/bash
# ETEC306 cloud side (技术方案.md 8.1 / 8.3): Mosquitto (TLS 8883, user/password, ACL) + cloud logger.
# Run as opc:  bash ~/etec306/install_server.sh      (idempotent; does not touch other services)
set -euo pipefail
PUBIP=${PUBIP:-<CLOUD_SERVER_IP>}
BASE=/home/opc/etec306
CRED=$BASE/credentials
CERTS=/etc/mosquitto/certs
mkdir -p $BASE/data/images $CRED
chmod 700 $CRED

echo "== packages"
if ! rpm -q mosquitto >/dev/null; then sudo dnf -y install mosquitto; fi
if ! python3 -c 'import paho.mqtt' 2>/dev/null; then sudo dnf -y install python3-paho-mqtt; fi
sudo mkdir -p $CERTS

echo "== TLS certificates (private CA, server cert with SAN IP:$PUBIP)"
if [ ! -f $CERTS/server.crt ]; then
  T=$(mktemp -d)
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=ETEC306 MQTT CA" \
      -keyout $T/ca.key -out $T/ca.crt
  openssl req -newkey rsa:2048 -nodes -subj "/CN=$PUBIP" -keyout $T/server.key -out $T/server.csr
  printf "subjectAltName=IP:%s,IP:127.0.0.1,DNS:localhost\nextendedKeyUsage=serverAuth\n" "$PUBIP" > $T/ext.cnf
  openssl x509 -req -in $T/server.csr -CA $T/ca.crt -CAkey $T/ca.key -CAcreateserial -days 3650 \
      -extfile $T/ext.cnf -out $T/server.crt
  sudo install -m 644 -o root -g root $T/ca.crt $CERTS/ca.crt
  sudo install -m 600 -o root -g root $T/ca.key $CERTS/ca.key
  sudo install -m 644 -o root -g root $T/server.crt $CERTS/server.crt
  sudo install -m 640 -o root -g mosquitto $T/server.key $CERTS/server.key
  rm -rf $T
fi
cp $CERTS/ca.crt $BASE/ca.crt

echo "== users (passwords generated once, stored only in $CRED)"
genpw() { openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 24; }
for u in etec306-pi etec306-cloud etec306-admin; do
  [ -f $CRED/$u.pw ] || { genpw > $CRED/$u.pw; chmod 600 $CRED/$u.pw; }
done
T=$(mktemp)
for u in etec306-pi etec306-cloud etec306-admin; do
  printf "%s:%s\n" "$u" "$(cat $CRED/$u.pw)" >> $T
done
mosquitto_passwd -U $T
sudo install -m 600 -o mosquitto -g mosquitto $T /etc/mosquitto/passwd
rm -f $T

python3 - "$CRED" "$PUBIP" <<'PY'
import json, sys, os
cred, ip = sys.argv[1], sys.argv[2]
pw = lambda u: open(f"{cred}/{u}.pw").read().strip()
def dump(name, d):
    p = f"{cred}/{name}"
    with open(p, "w") as f:
        json.dump(d, f, indent=1)
    os.chmod(p, 0o600)
dump("pi_mqtt.json", {"host": ip, "port": 8883, "tls": True, "ca": "mqtt_ca.crt",
                      "username": "etec306-pi", "password": pw("etec306-pi"), "device_id": "etec306"})
dump("cloud.json", {"host": "127.0.0.1", "port": 8883, "tls": True, "ca": "/home/opc/etec306/ca.crt",
                    "username": "etec306-cloud", "password": pw("etec306-cloud")})
dump("admin.json", {"host": "127.0.0.1", "port": 8883, "tls": True, "ca": "/home/opc/etec306/ca.crt",
                    "username": "etec306-admin", "password": pw("etec306-admin")})
PY

echo "== broker config"
[ -f /etc/mosquitto/mosquitto.conf.orig ] || sudo cp /etc/mosquitto/mosquitto.conf /etc/mosquitto/mosquitto.conf.orig
sudo install -m 644 -o root -g root $BASE/mosquitto.conf /etc/mosquitto/mosquitto.conf
sudo install -m 600 -o mosquitto -g mosquitto $BASE/acl /etc/mosquitto/acl
sudo mkdir -p /var/lib/mosquitto && sudo chown mosquitto:mosquitto /var/lib/mosquitto
command -v restorecon >/dev/null && sudo restorecon -R /etc/mosquitto /var/lib/mosquitto || true
sudo systemctl enable mosquitto
sudo systemctl restart mosquitto
sleep 1
systemctl is-active mosquitto
sudo ss -ltnp | grep 8883 || { echo "mosquitto not listening"; sudo journalctl -u mosquitto -n 30 --no-pager; exit 1; }

echo "== OS firewall (only if active)"
if systemctl is-active --quiet firewalld; then
  sudo firewall-cmd --permanent --add-port=8883/tcp && sudo firewall-cmd --reload
elif sudo iptables -S INPUT | grep -q -- '-j REJECT\|-P INPUT DROP'; then
  sudo iptables -C INPUT -p tcp --dport 8883 -j ACCEPT 2>/dev/null || sudo iptables -I INPUT -p tcp --dport 8883 -m state --state NEW -j ACCEPT
  echo "iptables rule added (persist via netfilter-persistent/iptables-save if used)"
else
  echo "no blocking OS firewall"
fi

echo "== cloud logger service"
sudo install -m 644 -o root -g root $BASE/etec306-cloud.service /etc/systemd/system/etec306-cloud.service
sudo systemctl daemon-reload
sudo systemctl enable etec306-cloud
sudo systemctl restart etec306-cloud
sleep 2
systemctl is-active etec306-cloud
echo INSTALL_DONE
