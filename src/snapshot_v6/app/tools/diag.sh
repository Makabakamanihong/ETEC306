#!/bin/bash
# read-only diagnostics, run as root
echo "== host"; hostname; uname -a; cat /etc/os-release | grep PRETTY
echo "== python"; sudo -u <user> python3 --version; sudo -u <user> python3 -c "import serial,sys;print('pyserial',serial.__version__)" 2>&1
sudo -u <user> python3 -c "import smbus2;print('smbus2 ok')" 2>&1; sudo -u <user> python3 -c "import lgpio;print('lgpio ok')" 2>&1; sudo -u <user> python3 -c "import gpiozero;print('gpiozero ok')" 2>&1
echo "== id"; id <user>
echo "== dev"; ls -l /dev/ttyUSB* /dev/ttyACM* /dev/i2c-1 /dev/video0 2>&1; ls -l /dev/serial/by-id/ 2>&1
echo "== lsusb"; lsusb 2>&1
echo "== service"; systemctl is-active etec306-bench.service; systemctl show -p MainPID,User,ExecStart,WorkingDirectory etec306-bench.service
echo "== fuser"; fuser -v /dev/ttyUSB0 2>&1
echo "== lsof"; lsof /dev/ttyUSB0 2>&1 | head
echo "== runtime dir"; ls -la /home/<user>/etec306/ /home/<user>/etec306/bench/ 2>&1
echo "== server.py md5"; md5sum /home/<user>/etec306/bench/server.py /home/<user>/etec306/bench/index.html
echo "== server.py radar parts"; grep -n -E "def ld_signed|0x8000|7FFF|RADAR|exclusive|aa.xff|55.xcc|start \+ 30" /home/<user>/etec306/bench/server.py
sed -n '/def ld_signed/,/^def radar_loop/p' /home/<user>/etec306/bench/server.py
echo "== api radar"; curl -s --max-time 3 http://127.0.0.1:8080/api/status | python3 -c "import json,sys;d=json.load(sys.stdin);print(json.dumps(d['radar'],ensure_ascii=False,indent=1)); print('i2c',d['i2c'],'pca',d['pca9685'])"
