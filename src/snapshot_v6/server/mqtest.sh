cd ~/etec306
echo "-- anonymous connect (must fail):"
timeout 5 mosquitto_sub -h 127.0.0.1 -p 8883 --cafile ca.crt -t 'home/#' -C 1 -W 3 2>&1 | head -2
echo "-- wrong password (must fail):"
timeout 5 mosquitto_sub -h 127.0.0.1 -p 8883 --cafile ca.crt -u etec306-cloud -P wrong -t 'home/#' -C 1 -W 3 2>&1 | head -2
echo "-- plain 1883 (must be closed):"
timeout 3 bash -c 'echo > /dev/tcp/127.0.0.1/1883' 2>&1 | head -1
echo "-- remote cmd snapshot via cloud account:"
python3 etec306_cmd.py snapshot
sleep 6
echo "-- admin sub retained status:"
AP=$(cat credentials/etec306-admin.pw)
timeout 6 mosquitto_sub -h 127.0.0.1 -p 8883 --cafile ca.crt -u etec306-admin -P "$AP" -t home/etec306/status -C 1 -W 5 -v
echo "-- logger:"
journalctl -u etec306-cloud -n 6 --no-pager | cut -c 40-
ls -la data/images | tail -3
grep -c '"topic": "home/etec306/status"' data/*.jsonl
grep '"topic": "home/etec306/event"' data/*.jsonl | tail -3 | cut -c 1-400
python3 etec306_stats.py
