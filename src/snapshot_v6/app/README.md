# ETEC306 正式程序（app/）— v6（2026-10-20）

v4 改动（相对 v3）：水平 ±80°（611–2389 µs，逐步实测）、巡逻 ±80°；侧脸处理（只比对接近正脸、侧脸/转头继续跟踪、录入要求正面+左右转头）；
远处人脸比对用全分辨率局部检测；Telegram / WhatsApp 告警通知（默认关）；主页两栏布局；x_sign 修正为 +1；方案 T2/T3/T7 自动测试脚本；
退出舵机手动后自动恢复跟踪；修了 v3 的一个隐藏 bug：手掌检测框是 float32，画面里一出现“手”`/api/state` 就 JSON 出错、连接被直接断开
（网页时不时“连接中断”），现在转成普通 float，网页接口出错也会写日志并返回 500。v3 备份：`/home/<user>/etec306/backup_v4/app_v3.tar`。

Pi 上的路径 `/home/<user>/etec306/app/`，服务 `etec306-app.service`（开机自启）。
- 网页 **http://<PI_LAN_IP>:8080/**（局域网，电脑用）
- 网页 **https://<PI_LAN_IP>:8443/**（同一个程序，自签证书；手机/电脑浏览器摄像头录入必须用这个。第一次打开浏览器会提示“不安全/证书无效”，点“高级 → 继续访问”一次即可）
- 页面：`/` 监控主页（两栏：左边大画面+按钮+最近抓拍；右边状态、目标/身份、雷达俯视图、事件；手机上自动上下排列），
  右上角“设置” → `/settings.html`（标定、调参、舵机测试、雷达表、手机通知、系统信息），`/enroll.html` 人脸录入。

云端（OCI，<CLOUD_SERVER_IP>）上的文件在 `../server/`，见 `../server/README.md`。
旧检测台在 `/home/<user>/etec306/bench/`，`etec306-bench.service` 停止并禁用（回滚见最后）。

## v6 改动（2026-10-20 晚）
### 状态栏指标与功耗估算（`metrics.py`，`/api/metrics`，`logs/metrics_YYYYMMDD.csv` 每 10 s 一行）
主页状态栏：CPU 温度 | CPU 使用率 | 估算总功耗；“系统”行：内存、负载、CPU 频率、欠压/降频标志（`vcgencmd get_throttled`，当前与开机以来）、
运行时间、雷达 Hz、摄像头 fps / 休眠、检测 Hz、雷达帧→舵机脉冲时延、舵机动作比例、Wi-Fi SSID/RSSI/IP、MQTT 在线/队列、磁盘。
设置页“系统 / 功耗”：1 小时曲线（功耗、CPU、摄像头休眠区间）+ 分项表 + 系数。

**功耗是估算值（树莓派 4B 没有功耗 ADC），线性模型：**
| 项 | 系数 | 依据 |
|---|---|---|
| 树莓派空闲 | 2.85 W | 树莓派官方/社区 USB 电表实测 4B 空闲 2.7–3.0 W（Wi-Fi 开、无 HDMI） |
| CPU 负载 | +3.5 W × CPU% | 4 核满载压力测试整板约 6.4 W |
| USB 摄像头 | 推流 0.85 W / 关闭流 0.25 W | UVC 摄像头典型 150–200 mA@5 V（假设，未实测） |
| 雷达 LD2450 + CH340 | 0.55 W | 数据手册要求电源 ≥200 mA，典型约 100 mA@5 V |
| OLED + LED | 0.06 W + 0.02 W/灯 | SSD1306 典型 10–20 mA@3.3 V |
| 舵机（独立 5 V 电源，单列） | 保持 0.05 W/个，运动 1.0 W/个 × 运动比例 | 假设（舵机型号未定） |
系数都在 `metrics.py` 顶部，可按实测改。建议用 USB 电表测一次空闲/满载校准。

### 摄像头休眠（省电）
布防且 `camera_sleep_s`（默认 60 s）内无雷达航迹、无视觉目标 → 关闭视频流（释放设备，**不断 USB 电**：雷达 CH340 也在 USB 上），视觉暂停，
页面显示“Camera: sleeping”占位图。雷达一出现航迹立即唤醒；网页“唤醒摄像头”、比对、抓拍、录入、标定也会唤醒。撤防时不休眠。
休眠时默认不巡逻（只靠雷达），`patrol_while_sleep` 可开。唤醒时延记录在 `/api/metrics` 的 `camera_wake`（唤醒→第一帧）和 `detect_wake`（→第一次检测）。

### 侧脸/远距离比对
- 按姿态分箱匹配：转头的脸额外和录入时同侧、yaw 相差 ≤20° 的样本比（取最好 2 个平均）。
- 清晰度门限：对齐后 112×112 人脸的拉普拉斯方差 < 120 视为模糊，跳过不计分（实测清晰图 680–1250）。
- 转头上限 60°（>30° 阈值 +0.05）；中距离用全分辨率 ROI（v5c）。

### Wi-Fi 配网（AP 热点 + 强制门户）
- 进入方式：开机 60 s 内没连上已知 Wi-Fi（也没网线）自动进入；或同时长按 **K2+K3 5 秒**；或设置页“进入 Wi-Fi 配网模式”。
- 树莓派开热点 **ETEC306-Setup-XXXX**（XXXX = MAC 后 4 位），WPA2 密码 8 位数字随机生成，显示在 OLED（`SETUP ...` / `pw ... 10.42.0.1`）。
- 手机连上后会弹出登录页（所有域名都解析到 10.42.0.1）；没弹出就打开 http://10.42.0.1 。选网络、输密码、点连接 → 树莓派用 nmcli 保存并连接，热点消失，OLED 显示新 IP 60 s。
- 也可以把手机“分享 Wi-Fi”的二维码对准摄像头（配网模式下每秒读 2 次）。
- 10 分钟（`/etc/default/etec306-wifi` 的 `SETUP_TIMEOUT`）没成功 → 自动关热点、恢复原连接。已保存的连接不会被删。
- 安装：`bash tools/install_wifi.sh`（systemd 服务 `etec306-wifi-setup` / `etec306-wifi-check`，sudoers 只允许启动这个服务）。

## v5 改动（2026-10-20）
- **身份比对**：根因不是特征/阈值，而是门控：人坐在桌前侧着脸（yaw −10…−40°）时 `identify_loop` 一直等“正脸+云台静止+居中”，
  15 s 后记为“未知（相似度 0.00）”且不再比对，于是每 30 s 告警一次。现在：超时“未知”是软状态（继续比对，认出即改 known），
  转头上限 30→45°（>30° 阈值 +`face_side_margin` 0.05），居中/静止条件放宽，多帧投票（两次 ≥ 阈值−`face_vote_margin`），
  只有清晰（眼距 ≥1.5×min）的低分才计入“不符”；本次会话出现过可比对的脸就不再按“比对距离外”告警；手动“立即比对”认出也写入当前目标。
  `/api/state` 新增 `id_scores`（最近比对分数）。离线测试：`tools/face_offline.py`。
- **主动搜索**：雷达有目标、对准后 `search_after_s` 内画面没人 → 在雷达方位附近按 9 点图案搜索（±`search_pan_deg`，俯仰 `search_tilt_deg`，
  每点停 `search_dwell_s`），云台静止时做帧差，运动块→对准等检测；搜完一圈仍没人才判“非人目标”。雷达指向加卡尔曼速度提前量 `lead_s`。
  画面叠加：每个雷达航迹按当前映射投影成洋红十字（T号 距离 速度），与视觉目标关联时画青色框。每次锁定记录“脸方位 vs 雷达方位”误差（事件“关联”，`/api/state` 的 `assoc`）。
- **界面语言**：默认英文，右上角按钮切中文（localStorage `etec_lang`）。翻译表 `tools/i18n_tr.txt` → `python3 tools/gen_i18n.py` 生成 `i18n.js`；OLED 只输出 ASCII。
- **摄像头**：XC-CAMERA，只有 MJPG：2960×1664、2048×1536、1920×1080、1600×900、1280×960、1280×720、640×480、320×240（均 30 fps）。
  保持 视频流 1280×720@15、检测 640×360（YuNet 8 Hz）、比对静态图 2960×1664；新增 `config.CAM_CONTROLS`（电源频率 60 Hz，防灯光条纹）。
- **通知**：新增 ntfy（图片附件）、SMTP 邮件（附图）、Discord webhook（文字+图），均默认关，各有“发送测试”。

## 整体流程
```
LD2450 (19 Hz) → radar.py → tracker.py（近点合并、跳变抑制、卡尔曼、门限、换目标滞回）→ 雷达主目标
摄像头 1280×720 MJPEG ─┬→ vision.py  YuNet 人脸（≈7 Hz）+ 标定时的运动块
                      └→ objects.py NanoDet 物体（人/笔记本/床/椅子…，≈1.5 Hz，Pi 上 ~210 ms）+ 手掌检测（~85 ms）
引导优先级：手动 > 归中 > 标定 > 撤防 > 暂停 > 视觉跟踪（看到脸/人体）> 雷达 > 巡逻 > 回中
状态机：布防 → 核实(VERIFY，云台转过去看) → 锁定(LOCKED，确认是人才进) → 比对 → 告警(ALARM) / 已知人员
非人目标（云台看了 verify_s 秒没看到人）→ 标为“物体”，记住位置，object_ignore_s 秒内不再理会，不抓拍不锁定
→ 灯 / OLED / 事件 / 抓拍 → MQTT (TLS 8883) → 云端记录
```

## 文件
| 文件 | 作用 |
| --- | --- |
| `app.py` | 主服务：雷达线程、引导循环（只写 CH4/CH8）、视觉跟踪、非人目标过滤、巡逻、标定、比对/录入、状态机、灯/按键/OLED、MQTT、HTTP(8080)+HTTPS(8443)/API |
| `objects.py` | NanoDet-Plus（COCO 80 类，中文名）+ MediaPipe 手掌检测，OpenCV DNN；`python3 objects.py a.jpg` 离线测 |
| `vision.py` | YuNet 人脸、SFace 特征、白名单（`faces/`）、运动块检测（标定用）、视觉线程 |
| `tracker.py` | 近点合并（同一个人手臂/身体多点）、最近邻关联（门限 gate_m）、跳变抑制（单帧跳 > jump_m 先不信，连续 3 帧才接受）、卡尔曼、换目标需要优势 switch_margin_m 并保持 switch_hold_s |
| `pointing.py` | θ = az_scale·atan2(x_sign·X, Y) + θ0，φ = atan2(h_t−h_s, r) + φ0；脉宽 = 1500 − sign·角度·1000/90 + 微调，水平限幅 611–2389 µs（±80°），俯仰 1000–2000 µs（±25°）；像素→角度用针孔模型 |
| `notify.py` / `notify.json` | 告警推送：Telegram（文字+图）、WhatsApp CallMeBot（文字）、WhatsApp Cloud API（模板/图）。`notify.json` 权限 600，只在 Pi 上，默认全关 |
| `settings.py` / `settings.json` | 所有可调参数（设置页按分组显示） |
| `index.html` / `settings.html` / `enroll.html` / `common.js` | 网页（纯表格，改完刷新浏览器即可） |
| `tls/cert.pem`、`tls/key.pem` | HTTPS 自签证书（10 年，SAN=<PI_LAN_IP>、<pi-hostname>.local、localhost）；key 权限 600，只在 Pi 上 |
| `models/` | YuNet、SFace、NanoDet（object_detection_nanodet_2022nov.onnx）、手掌（palm_detection_mediapipe_2023feb.onnx），都是 OpenCV Zoo 官方 ONNX；`haarcascade_profileface.xml`（OpenCV 官方侧脸级联，只用来继续跟踪） |
| `faces/` | 白名单：`<名字>.npz`（最多 60 个特征，v4 起每个样本带转头角 yaw）+ `<名字>_k.jpg` 小图。只在 Pi 上 |
| `mqtt.json`、`mqtt_ca.crt` | MQTT 账号和 CA（600，只在 Pi 上） |
| `radar.py` `camera.py` `hw.py` `mqtt_client.py` `config.py` | 同 v2 |
| `tools/enroll.py` | 命令行录入/列表/删除 |
| `tools/bootcheck.sh` | 开机后自检：服务、I2C、串口、摄像头、端口、HTTPS、MQTT |
| `tools/pi_run.py` | Windows 端远程执行/上传（密码从环境变量 `PI_PASS` 读） |
| `tools/pan_range_test.py` | 水平范围逐步测试（每步 5 计数 ≈2.2°，用画面相位相关确认还在转，转不动就停） |
| `tools/servo_tests.py` | 方案第 10 节 T2（最小可动步进）/T3（30° 跳变到画面稳定的时间）/T7（来回转 2 分钟看是否欠压/重启），结果 `tools/servo_tests.json` |

## 方向约定与标定
- 角度约定：水平角 + = 镜头向右，俯仰角 + = 镜头向上。
- `pan_sign` / `tilt_sign`：舵机方向。实测这台云台 CH4 1700 µs = 镜头向左，CH8 1300 µs = 镜头向上，所以两个都应为 **+1**。
  v2 里 `tilt_sign` 是 −1（当时是凭现象改的，其实和硬件相反，导致视觉修正变正反馈、镜头越跑越偏）；
  v3 的“方向自检”在 Pi 上实测得到 pan_sign=+1、tilt_sign=+1，已自动写入。以后仍可在设置页改。
- `x_sign`：雷达 X 的左右（−1 = 原始 X 取反为右）。`az_scale`、`theta0_deg`：雷达方位 → 云台水平角的比例和偏置。
  这三个由“标定取点”最小二乘拟合得到（见下）。
  **v4：x_sign 改回 +1、theta0 −3.9°、az_scale 1.0。** 证据：① 10-05 晚你在画面最左（约 −55…−60°）时雷达原始方位 −11…−18°；
  ② 你自己标的两个点（实际 pan −9.6° ↔ 原始 −10.5°，−21.1° ↔ −12.4°）原始方位和实际方向同号；
  ③ 截图里 X=+217/Y=2805 的 T164 其实是正前方 2.5–3 m 的固定杂波（原始方位 −3…−5°，记录了 9 分钟，位置不随你动），
  旧的 θ0 = −26.8° 正好把它“搬”到你的脸附近，才出现“左边的脸配右边的雷达点”。原始 X < 0 = 左，所以 x_sign = +1。
  两点左右只差 11°，比例拟合不可靠：v4 拟合在左右范围 < 8° 时只拟合偏置，并比较 x_sign=±1 两种结果（残差 + 偏置太大罚分）。
  **建议你有空再标一次：左、中、右各站一个点（左右差 30° 以上）。**
- 设置页“标定”：
  1. **方向自检**（不需要人，场景别有大的运动）：云台向右、向上各转 8°，用相位相关看画面平移方向，自动改 `pan_sign`/`tilt_sign`，
     并按平移像素估计摄像头视场角 `cam_hfov_deg`/`cam_vfov_deg`。约 10 s。
  2. **取点**（需要一个人）：输入一个固定姿态（默认 pan 0 tilt 0）点“开始标定”，云台不动，跟踪暂停。
     人站到画面里，在脸旁边挥手（或正对镜头），画面出现红框“候选”（有脸用脸，否则用最强的运动块），右边显示它和当时雷达目标的配对：
     “实际 pan/tilt ← 雷达 T 的原始方位”。确认红框是你，点“记录这一点”。换位置（左/中/右、近/远）重复 3–6 次。
     左右范围要超过 8° 才能拟合比例。
  3. “拟合并保存”：水平用 pan_true = a·θ_raw + b 最小二乘 → `x_sign = sign(a)`、`az_scale = |a|`、`theta0_deg = b`；
     有人脸的点再拟合俯仰（`phi0_deg` + `target_height_m`）。“只计算不保存”可以先看残差。点“退出标定”恢复跟踪。

## 跟踪、视觉优先、非人目标、巡逻
- 雷达：同一个人的多个点（手臂、身体）在 `merge_m`(0.35 m) 内合并；单帧跳动 > `jump_m` 当作肢体动作先不跟，连续 3 帧才接受；
  换主目标要比当前目标近 `switch_margin_m` 且保持 `switch_hold_s`，避免左右来回摆；云台死区 `deadband_deg`。
- 视觉优先（`vision_priority`）：看到人脸（连续 2 帧）或人体（置信 ≥ `person_conf`，连续 `person_frames` 次；
  没有对应雷达目标时多要 1 次）→ 改为视觉跟踪：按像素误差×视场角换算角度，增益 `vis_track_gain`、死区 `vis_deadband_px`，
  每个新检测只用一次，并用“拍这一帧时云台的角度”来算，所以不会因检测慢而越转越过头。
  视觉跟踪时不再跟雷达；`vis_lost_s` 秒没看到人才交回雷达，雷达只用来重新找人。
- 手：手掌检测只用来判断“人是哪个雷达目标”（优先那个目标），不单独锁定或告警（手掌模型对杂物有误检）。
- 非人目标：雷达目标 → 云台转过去，`verify_s`(2.5 s) 内画面里没有人 → 标为物体（画面上若认出是床/冰箱等会写上）、
  记住位置 `object_ignore_s`(60 s)；同一位置再冒出来的新目标直接当物体；只有真的移动 > 0.5 m 才重新核实。
  **只有确认是人才进入 LOCKED、才抓拍、才告警。**
- 巡逻（`patrol_enabled`）：`patrol_idle_s`(30 s) 没有任何目标 → 在 ±`patrol_range_deg`（v4 默认 80°）内每 `patrol_step_deg`（40°）停 `patrol_dwell_s` 秒，
  俯仰 `patrol_tilt_deg`；停的时候跑视觉，雷达或视觉一有人立即退出巡逻去锁定/比对/告警。
- 最小距离 `min_range_m` = 0.5 m（v2 时 0.6 m 的静止目标其实是坐着的你，没有屏蔽）。

## 比对、告警、抓拍
- 身份比对：r ≤ `identify_range_m`、脸在中央、云台静止 → 拍 2960×1664 一帧 → SFace → 和白名单比：
  分数 = max(平均特征余弦, 最相近 3 个样本的平均)，≥ `face_threshold` = 已知；最多 `identify_attempts` 次。
- 只有真正比对过（找到正脸、够大、分数低）才计入“未知”：`identify_attempts` 次低分 = 未知；没脸/侧脸只重试（最多 3 倍次数）。
- 告警：确认是人且持续 `alarm_confirm_s`(1 s)，比对结果“未知” → 告警 + 抓拍 + MQTT；每个目标冷却 `alarm_cooldown_s`(30 s)，两次告警间至少 5 s。
  已知人员不告警。看不到可比对的脸（太远/只有人体）时：持续 `far_alarm_delay_s`(4 s) 且这期间没看到够大的脸才告警
  （给比对留时间；实测 v3 第一版会在脸转过来之前就把你本人报警），并且认出家人后 `known_hold_s`(120 s) 内只看到人体不告警。
  `alarm_far_detect` 关掉则看不到脸的人一律不告警。
- 抓拍：锁定时一张（每个目标一次，间隔 ≥ `snapshot_min_interval_s`），画面和上一张几乎一样就不存（去重）；最多保留 `SNAP_KEEP` 张。
- 侧脸 / 转头（v4）：
  - YuNet 5 个关键点估计转头角 yaw（鼻尖偏离两眼中点 / 眼距，yaw ≈ atan(比值/0.35)，正 = 鼻子朝画面右）。画面上脸框写“转 N°”。
  - **只比对接近正面的脸**：|yaw| ≤ `identify_max_yaw_deg`(30°)，否则显示“侧脸，等正脸再比对”，不比对、也不算一次“不符”。
  - 已锁定的人转头后继续跟：① 低置信人脸（`face_weak_score` 0.55 ≤ 分数 < `face_score`）且在上一张脸附近 → “弱人脸/侧脸”；
    ② OpenCV 侧脸级联（左右两个方向）在上一张脸附近找到 → “侧脸”；③ 都没有 → KCF 跟踪上一张脸的位置，最多 `head_track_s`(3 s)。
    ①② 每次找到都续时；这些只能**延续**已有的视觉跟踪，不能单独确认是人、不能开始锁定。之后才退回人体框/雷达。
  - 近处的人一直不露正脸（侧脸/背对）超过 `identify_wait_s`(15 s) → 按“未知”处理（认出家人后 `known_hold_s` 内除外）；0 = 一直等。
  - 远处（2.5–3 m）的脸在 2960×1664 静态图缩小 4 倍后经常找不到：v4 会在实时画面人脸位置附近截全分辨率小图再检测一次。
    实测你躺在约 2.5–3 m 时静态图眼距 30–40 px，多数低于 `min_eye_px`(40)，一次比到 0.30（阈值 0.363）→ 报“未知”。
    想在床边也认出你：用监控摄像头在那个位置再录一次（同名追加），或把 `min_eye_px` 降到 30（误认风险会增加）。
- CPU ≥ 75 °C 自动降低检测频率；只在核实/锁定/告警/巡逻/录入/标定时跑检测。实测 Pi 4B 运行中 42–51 °C。
- 日志：`events.jsonl` 超过 5 MB 轮转（保留 .1–.3）；journald 限制 200 MB / 1 个月（`/etc/systemd/journald.conf.d/etec306.conf`）。

## 人脸录入（白名单）— 主页“人脸录入”按钮 → /enroll.html
方法 A：用访客自己的手机/电脑摄像头（getUserMedia）
1. 手机连同一个 Wi-Fi，打开 **https://<PI_LAN_IP>:8443/enroll.html**（第一次有证书警告，点“高级 → 继续”）。
   用 http 打开时页面会提示并给出 https 链接（浏览器规定只有 HTTPS 才能开摄像头）。
2. 输入姓名、张数（默认 9：1/3 正面、1/3 向自己左边转 20–30°、1/3 向右转），点“打开摄像头”，再点“开始录入”，按页面上的红色大字提示转头。
   每张算出转头角，哪个方向够了就不再收这个方向；试了 3 倍张数还凑不齐就不再限制方向。允许的最大转头 `enroll_max_yaw_deg`(45°)。
3. 页面每 0.7 s 截一帧 JPEG 上传到 Pi，Pi 用 YuNet+SFace 处理，显示每张是否合格（置信、眼距）；和上一张几乎一样的不要。
4. 够数自动保存。同名再录 = 追加样本（最多 60 个；平均模板只用正面样本，比对时“最相近 3 个样本”会用到转头样本）。**建议手机录一次，再用监控摄像头录一次**，两种摄像头画质不同，混合后识别更稳。
方法 B：用监控摄像头（原来的方式）：站在云台前 1–2 m，点“用监控摄像头录入”，按进度里的提示先正面、再向左、向右转头。
v3 以前录的样本没有转头角，算作正面；表格里“样本 (正/左/右)”显示各方向数量。
下面表格列出已录入的人、样本数、小图，可删除。命令行：`python3 tools/enroll.py add|list|delete`。
注意：同一局域网里能打开网页的人都能录入/删除（没有登录），只在家里网络使用。

## MQTT（8.1）
Broker：`<CLOUD_SERVER_IP>:8883`，TLS（自签 CA，`mqtt_ca.crt`），用户名密码，禁止匿名，ACL 限制。
| 主题 | 方向 | 内容 |
| --- | --- | --- |
| `home/etec306/status` | Pi→云，保留 | 每 10 s 心跳：online、armed、state、pan/tilt、IP、CPU 温度、雷达帧率；断线遗嘱 `{"online": false}` |
| `home/etec306/track` | Pi→云 | 主目标 x(右正)、y、r、v_r、θ，≤ `mqtt_track_hz`(2 Hz) |
| `home/etec306/event` | Pi→云，QoS1 | 事件 JSON：id、ts、type(alarm/known/snapshot/arm)、文字、坐标、image 文件名 |
| `home/etec306/image/<id>` | Pi→云，QoS1 | JPEG（质量 70，长边 1280）：未知人员告警图 + K4/网页抓拍图（7.5；`upload_manual_snap` 关掉则 K4 只发事件不发图）。已知人员不发图 |
| `home/etec306/cmd` | 云→Pi | `{"action": "arm|disarm|toggle_arm|center|snapshot|pause|resume|clear_alarm"}`，和 K1/网页进同一个状态机 |
断网时跟踪/灯/OLED 照常；QoS1 消息最多排队 50 条，重连后补发。OLED 第 2 页显示 MQTT 在线/离线和上次心跳。

## 手机通知（Telegram / WhatsApp）— 设置页第 5 节
从 **Pi 直接发**（不经过云端）：告警图本来就在 Pi 上，不用先传云再下发；“发送测试”按钮能立即看到真实结果；云端/MQTT 断了也能发（只要有网）。
云端 `etec306-cloud` 里 v3 留的 ntfy/Telegram 接口仍可用（见 server/README），两边只开一个即可，避免重复。
- 只在“告警”时发（未知人员 / 比对距离外看到人 / 无视觉接近告警），文字 + 抓拍图；`min_interval_s`(60 s) 内最多一条；后台队列发送，不卡跟踪。
- `notify.json`（权限 600）：总开关 `enabled`、`min_interval_s`、`send_photo`，各通道的开关和参数。网页只显示“已保存/未设置”，**不会回显密钥**；
  留空保存 = 不改原值；“清除所有已保存的密钥”可删掉。错误信息里的密钥会被替换成 ***。
- **Telegram**（推荐，免费，文字+图）：
  1. Telegram 里找 **@BotFather** → `/newbot` → 按提示起名 → 得到 **bot token**（形如 `123456789:AA…`）。
  2. 设置页填 bot_token → “保存通知设置”。
  3. 在 Telegram 里打开你的新机器人，发一句 `/start`（机器人不能主动找你，必须你先发）。
  4. 点“查找 chat_id”（调用 getUpdates），点列出的数字填入 chat_id → 保存。（群组：把机器人拉进群发一句话，群的 chat_id 是负数。）
  5. 通道“启用”选开、总开关选开 → 保存 → “发送测试”（可勾“带当前画面”）。
- **WhatsApp – CallMeBot**（免费，**只能发文字**、只能发给自己、仅限个人使用）：按 callmebot.com 的“Free API WhatsApp”页面，
  把页面上给出的机器人号码加为联系人（号码会变，以官网为准），用 WhatsApp 给它发 `I allow callmebot to send me messages`，
  收到的 APIKEY 和你的手机号（带国家码，如 +44…）填进来。告警文字里会注明“图片在网页/云端”。
- **WhatsApp – Meta Cloud API**（官方）：需要 Meta 开发者应用 + WhatsApp Business 账号 + 一个业务电话号码（测试号也行），
  填 access_token（建议系统用户永久令牌）、phone_number_id、收件人号码。24 小时会话窗口外**只能发已审核通过的模板**（方式选“模板”，
  填模板名/语言；模板正文带一个 {{1}} 参数时会把告警文字填进去）；窗口内可选“文字/图片”（图片先上传 /media 再发）。默认 API 版本 v25.0。
  对学生作业来说手续最多，建议用 Telegram，WhatsApp 用 CallMeBot 收文字提醒即可。

## 网页 API
`GET /api/state`（`?lite` 精简）、`GET /api/settings`、`POST /api/settings {...}`、`POST /api/settings/reset`、
`POST /api/cmd {"action": "arm|disarm|toggle_arm|center|pause|resume|snapshot|clear_alarm|identify|reset_identity"}`、
`POST /api/servo {"action":"nudge","ch":4,"ticks":1}` / `{"action":"center","ch":8}` / `{"action":"exit"}`、
`POST /api/calib {"action":"selftest|start|record|undo|clear|fit|stop", "pan":0, "tilt":0, "apply":true}`、
`POST /api/enroll {"name":"张三","samples":5}`（监控摄像头）、
`POST /api/enroll/upload?sid=<随机>&name=<名字>&n=6`（body = JPEG，浏览器摄像头）、`POST /api/enroll/finish {"sid":...}`、`POST /api/enroll/cancel`、
`POST /api/faces/delete {"name":"张三"}`、`GET /api/notify`（无密钥）、`POST /api/notify {...}`、`POST /api/notify/test {"channel":"telegram|whatsapp_callmebot|whatsapp_cloud","photo":true}`、
`POST /api/notify/telegram_chats`、`POST /api/servo {"action":"pulse","ch":4,"us":1800,"quiet":true}`（绝对脉宽，返回寄存器回读）、`GET /api/stream.mjpg`、`GET /api/frame.jpg`、`GET /snapshots/<f>.jpg`、`GET /faces/<f>.jpg`

## 常用命令（Pi）
```
sudo systemctl status etec306-app
journalctl -u etec306-app -f            # [track] 每秒一行（vis faces/persons/hands、objs），[event] 事件
sudo systemctl restart etec306-app      # 改 .py 后重启（服务 Restart=always）
bash tools/bootcheck.sh                 # 开机自检
python3 objects.py a.jpg                # 离线测物体/手掌模型和耗时
python3 tools/vision_selftest.py a.jpg  # 离线测人脸模型
```

## 测试（第 10 节）怎么做
- T2：设置页“舵机手动测试”，CH4/CH8 每次 ±1 计数（≈4.88 µs ≈ 0.44°）；自动版 `python3 tools/servo_tests.py t2`（用画面平移判断真的动了没有）。
- T3：设置页“时延”是软件部分（帧→脉宽）中位数/最大值；机械部分 `python3 tools/servo_tests.py t3`（30° 跳变 20 次，到画面稳定的时间，含摄像头延迟，是上限）。
- T7：`python3 tools/servo_tests.py t7`（±45° 来回 2 分钟，记录 vcgencmd get_throttled、uptime、内核欠压日志、寄存器回读）。
- 水平范围：`python3 tools/pan_range_test.py`（v4 实测 611–2389 µs 每一步画面都在动，回读一致，throttled=0x0）。
- v4 实测（2026-10-06 早上，`tools/servo_tests.json`）：
  - T2：从 1500 µs 来回走 1/2/3/4 计数（4.9–19.5 µs）画面**完全不动**（噪声 0.01 px）；6 计数（29 µs，指令 2.6°）每次都动，实际约 1.2°。
    即这只舵机来回换向时的死区+齿隙约 20–29 µs（≈2°），比 PWM 分辨率（0.44°）大得多；同方向连续走 5 计数每步都动（范围测试）。
  - T3：手动脉宽一次跳 30°（不经过限速），20 次：画面开始动 中位 112 ms，画面稳定 中位 254 ms、最大 318 ms（含摄像头约 100 ms 延迟，是上限）；
    软件部分（雷达帧→写脉宽）中位 30 ms、最大 50 ms。正常跟踪时还有限速/低通，这里测的是舵机+摄像头本身。
  - T7：±45° 每秒来回一次共 2 分钟（110 次）：throttled 前后都是 0x0，没重启，内核无欠压日志，寄存器回读最大差 2 µs，MODE1 一直 0x20。
- T6：断网后 MQTT 变离线，恢复后自动重连并补发排队的 event。
- T8：画面上绿框=人脸、蓝框=人体（浅蓝“人?”=低于确认门限）、橙框=手、灰框=其他物体，黄线=视觉跟踪误差；右上角小窗=放大的人脸。
- T9：`events.jsonl` 每次比对有相似度/眼距；云端 `python3 etec306_stats.py` 出每日统计。

## 开机自启
Pi：`etec306-app`（enabled，HTTP 8080 + HTTPS 8443 在同一个进程里，没有其他辅助服务），`etec306-bench` disabled。
I2C（dtparam=i2c_arm=on）、串口、摄像头开机自动可用。云端：`mosquitto`、`etec306-cloud` 都是 enabled。
开机后可以运行 `bash tools/bootcheck.sh` 检查。

## 切回检测台（回滚）
```
sudo systemctl disable --now etec306-app.service
sudo systemctl enable --now etec306-bench.service
```
v3 程序备份在 `/home/<user>/etec306/backup_v4/app_v3.tar`（含 settings.json、faces/；`cd app && tar -xf ../backup_v4/app_v3.tar` 再重启；
注意 v3 的水平限幅是 1000–2000 µs）；v2 在 `/home/<user>/etec306/backup_v3/app_v2.tar`（`cd app && tar -xf ../backup_v3/app_v2.tar` 再重启即可，含 settings.json 和 faces/）；
v1 在 `/home/<user>/etec306/backup_v2/`。关闭 MQTT：删掉或改名 `mqtt.json` 再重启。
再切回正式程序：
```
sudo systemctl disable --now etec306-bench.service
sudo systemctl enable --now etec306-app.service
```

## 没做 / 需要你决定
- 通知：代码已做（Telegram / CallMeBot / Cloud API），**需要你自己申请 token 填进设置页**，默认关闭，没发过任何消息。
- AES-GCM 本地加密：方案里写“若作业明确要求”才做；目前只有 TLS。
- InfluxDB/Grafana：按 8.3 的备选方案，先用 JSONL 日志 + 离线统计脚本代替。
- COCO 没有“柜子”这一类：柜子/衣柜会被认成 冰箱/微波炉/行李箱 等相近类别或不认；只用于显示和“不是人”的判断，不影响逻辑。
- HTTPS 是自签证书，每台手机第一次要点一次“继续访问”。
