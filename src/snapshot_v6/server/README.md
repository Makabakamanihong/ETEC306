# ETEC306 云端（OCI <CLOUD_SERVER_IP>，Oracle Linux 9，用户 opc）

文件在服务器 `~/etec306/`，本目录是本地副本。只新装了 mosquitto 和 python3-paho-mqtt，没有动服务器上的其他服务。

| 文件 | 作用 |
| --- | --- |
| `install_server.sh` | 一键安装（可重复跑）：mosquitto、证书、账号、配置、日志服务 |
| `mosquitto.conf` | 只开 8883 TLS；禁止匿名；password_file + acl_file；持久化。原配置备份为 `/etc/mosquitto/mosquitto.conf.orig` |
| `acl` | `etec306-pi` 只能写 status/track/event/image、读 cmd；`etec306-cloud` 读 home/#、写 cmd；`etec306-admin` 读写 home/# |
| `etec306_cloud.py` + `etec306-cloud.service` | 记录程序：订阅 home/#，写 `data/YYYY-MM-DD.jsonl`（track 降到 1 Hz），图片存 `data/images/<id>.jpg`，`data/latest.json` 最新状态；保留 30 天 |
| `etec306_cmd.py` | 远程命令：`python3 etec306_cmd.py arm|disarm|center|snapshot|...` |
| `etec306_stats.py` | 离线统计（8.3）：每天在线分钟、告警数、已知人员次数、抓拍、track 条数 |
| `mqttcfg.py` | paho 1.6/2.x 通用连接函数 |
| `notify.json.example` | 告警推送（ntfy 或 Telegram），复制成 `notify.json` 并 `"enabled": true` 才会发；默认不发。**v4 起推荐用 Pi 端的通知（app/notify.py，设置页第 5 节，含 WhatsApp），两边只开一个，避免重复推送** |

## 证书和密码
- 自签 CA + 服务器证书（SAN: IP <CLOUD_SERVER_IP> / 127.0.0.1 / localhost，10 年），在 `/etc/mosquitto/certs/`（私钥 root 600）。
- 三个账号的随机密码只保存在 `~/etec306/credentials/`（目录 700，文件 600）。Pi 用的配置是 `credentials/pi_mqtt.json`，
  已复制到 Pi 的 `/home/<user>/etec306/app/mqtt.json`，CA 复制为 `mqtt_ca.crt`。
- 改密码：删掉 `credentials/<用户>.pw` 再跑 `bash install_server.sh`，然后把新的 `pi_mqtt.json` 复制到 Pi 重启 app。

## 常用命令
```
systemctl status mosquitto etec306-cloud
journalctl -u etec306-cloud -f                       # 收到的 event / image
tail -f ~/etec306/data/$(date +%F).jsonl
cat ~/etec306/data/latest.json
python3 ~/etec306/etec306_stats.py 7
# 直接订阅（admin 账号）
mosquitto_sub -h 127.0.0.1 -p 8883 --cafile ~/etec306/ca.crt -u etec306-admin -P "$(cat ~/etec306/credentials/etec306-admin.pw)" -t 'home/#' -v
```

## 开机自启
`mosquitto` 和 `etec306-cloud` 都已 `systemctl enable`（2026-10-05 用 `systemctl is-enabled mosquitto etec306-cloud` 确认均为 enabled/active），
服务器重启后自动起来；Pi 端 `etec306-app` 也是开机自启，开机后自动重连 8883。

## 端口
8883/tcp：OS 防火墙（firewalld 未运行，iptables INPUT 默认 ACCEPT）不用改；OCI 安全列表已经放行（从家里 Windows 测试 TCP 8883 能连通，Pi 已连上）。1883 不开。

## 卸载 / 回滚
```
sudo systemctl disable --now etec306-cloud mosquitto
sudo rm /etc/systemd/system/etec306-cloud.service && sudo systemctl daemon-reload
sudo cp /etc/mosquitto/mosquitto.conf.orig /etc/mosquitto/mosquitto.conf
sudo dnf remove mosquitto python3-paho-mqtt     # 可选
```
