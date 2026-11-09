#!/bin/bash
# Switch from etec306-bench.service to etec306-app.service. Run as root (sudo).
set -u
APP=/home/<user>/etec306/app
cd $APP || exit 1
chown -R <user>:<user> $APP
echo "== selftests"; sudo -u <user> python3 radar.py --selftest || exit 1
echo "== camera pipe test (bench does not hold /dev/video0)"
fuser -v /dev/video0 2>&1
sudo -u <user> timeout 6 v4l2-ctl -d /dev/video0 --set-fmt-video=width=1280,height=720,pixelformat=MJPG --set-parm=15 --stream-mmap=4 --stream-count=20 --stream-to=- --silent > /tmp/etec_cam.mjpg 2>/tmp/etec_cam.err
python3 - <<'PY'
d=open('/tmp/etec_cam.mjpg','rb').read()
n=d.count(b'\xff\xd8\xff'); print('bytes',len(d),'jpeg SOI',n,'avg',len(d)//max(n,1),'DHT present', b'\xff\xc4' in d[:5000])
PY
cat /tmp/etec_cam.err | tail -3
echo "== install unit"
cp $APP/etec306-app.service /etc/systemd/system/etec306-app.service
systemctl daemon-reload
echo "== stop + disable bench (unit and files kept)"
systemctl stop etec306-bench.service; systemctl disable etec306-bench.service 2>&1
systemctl is-active etec306-bench.service; systemctl is-enabled etec306-bench.service
sleep 1
echo "== i2c"; ls -l /dev/i2c-1; i2cdetect -y 1
echo "== serial free?"; fuser -v /dev/ttyUSB0 2>&1 || echo "ttyUSB0 free"
echo "== start app"
systemctl enable --now etec306-app.service 2>&1
sleep 7
systemctl --no-pager status etec306-app.service | head -12
echo "== journal"; journalctl -u etec306-app.service -n 25 --no-pager -o cat
echo "== fuser"; fuser -v /dev/ttyUSB0 /dev/video0 2>&1
echo "== http"; curl -s -o /dev/null -w "GET / %{http_code} %{size_download}B\n" http://127.0.0.1:8080/
curl -s -o /dev/null -w "GET /api/frame.jpg %{http_code} %{size_download}B\n" http://127.0.0.1:8080/api/frame.jpg
timeout 3 curl -s http://127.0.0.1:8080/api/stream.mjpg -o /tmp/etec_stream.bin; python3 -c "d=open('/tmp/etec_stream.bin','rb').read();print('stream 3s bytes',len(d),'frames',d.count(b'--frame'))"
curl -s http://127.0.0.1:8080/api/state | python3 -c "
import json,sys;d=json.load(sys.stdin)
print('state',d['state'],'mode',d['guide']['mode'],'pan_us',d['guide']['pan_us'],'tilt_us',d['guide']['tilt_us'],'writes',d['guide']['writes'])
print('radar',{k:d['radar'][k] for k in ('open','error','fps','frames','dropped_bytes','age_ms')})
print('targets',d['radar']['targets']); print('tracks',d['tracks']); print('camera',d['camera']); print('hw',d['hw'])
print('events',d['events'][:8])"
