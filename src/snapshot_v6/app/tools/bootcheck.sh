echo "== services"
for u in etec306-app etec306-bench; do echo "$u enabled=$(systemctl is-enabled $u 2>&1) active=$(systemctl is-active $u)"; done
systemctl cat etec306-app | grep -E 'After|Wants|Restart|ExecStart|WantedBy'
echo "== boot config"
grep -E '^(dtparam=i2c|enable_uart|dtoverlay=disable-bt|camera_auto|dtoverlay=.*cam|start_x)' /boot/firmware/config.txt
echo "serial-getty: $(systemctl is-enabled serial-getty@ttyS0 2>&1) / $(systemctl is-enabled serial-getty@serial0 2>&1)"
ls -l /dev/serial0 /dev/i2c-1 /dev/video0 2>&1 | cut -c1-120
echo "== i2c"; /usr/sbin/i2cdetect -y 1 2>/dev/null | grep -E '^[34]0' 
echo "== journal"; journalctl --disk-usage; cat /etc/systemd/journald.conf.d/*.conf 2>/dev/null
echo "== disk"; df -h / | tail -1; du -sh /home/<user>/etec306/app/snapshots /home/<user>/etec306/app/events.jsonl* 2>/dev/null
echo "== uptime"; uptime; vcgencmd measure_temp; vcgencmd get_throttled
echo "== ports"; ss -ltn | grep -E ':8080|:8443'
echo "== https"; curl -sk -o /dev/null -w "https %{http_code}\n" https://127.0.0.1:8443/enroll.html
curl -s http://127.0.0.1:8080/api/state?lite | python3 -c "import json,sys; s=json.load(sys.stdin); print('state',s['state'],'radar_fps',s['radar']['fps'],'cam_fps',s['camera']['fps'],'mqtt',s['mqtt'].get('connected'),'i2c',s['hw']['i2c'],'pca',s['hw']['pca9685'],'vision',s['vision']['available'],'objects',s['objects']['available'],'https',s['https_port'])"
