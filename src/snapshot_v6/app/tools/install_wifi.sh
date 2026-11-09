#!/bin/bash
# install Wi-Fi setup mode (run once, with sudo rights)
set -e
command -v dnsmasq >/dev/null || sudo apt-get install -y dnsmasq-base
D=/home/<user>/etec306/app/tools
sudo install -m 644 $D/etec306-wifi-setup.service $D/etec306-wifi-check.service /etc/systemd/system/
echo '<user> ALL=(root) NOPASSWD: /usr/bin/systemctl start --no-block etec306-wifi-setup.service' | sudo tee /etc/sudoers.d/etec306-wifi >/dev/null
sudo chmod 440 /etc/sudoers.d/etec306-wifi && sudo visudo -cf /etc/sudoers.d/etec306-wifi
[ -f /etc/default/etec306-wifi ] || echo 'SETUP_TIMEOUT=600' | sudo tee /etc/default/etec306-wifi >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable etec306-wifi-check.service
echo wifi setup installed
