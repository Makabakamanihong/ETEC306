# Technical design — Edge–cloud intelligent security & dynamic target tracking

| Item | Content |
| --- | --- |
| Course | ETEC 306 / Electrical and Computer Engineering Technology |
| Basis | Capstone concept design + parts list |
| Status | Design draft v0.2 (includes near-range whitelist identity match) |

## 1. Review conclusions

Existing parts support a three-layer architecture: mmWave radar for spatial cueing, PCA9685 for servo PWM off the Linux scheduler, Raspberry Pi for vision confirmation and MQTT, and cloud receive of telemetry/alerts after events. Keys, traffic lights, and OLED provide a headless local HMI.

Critical decisions before wiring:

1. **Radar fixed in world frame; camera on the gimbal.** LD2450 returns X,Y relative to the radar. If the radar rides the gimbal, coordinates rotate with pan and the track loop can oscillate. Keep radar and gimbal horizontal axes coaxial/near and store a single install offset as a calibration constant.
2. **No height axis from radar; tilt is not arctan(X/Y) alone.** LD2450 is planar 1T2R. Pan from azimuth feedforward; tilt from mount height + assumed person height, then vision servo once a face is in frame.
3. **Two 5 V rails must share ground at the PCA9685 terminals; servo current must not go through the breadboard.** Power-bank C port feeds the Pi; A port feeds servos. PWM/I2C still reference Pi ground.
4. **PCA9685 logic VCC = 3.3 V; servo power only on green V+.** 5 V on logic would pull I2C to 5 V and risk Pi GPIO damage.
5. **250 ms covers radar frame → coarse gimbal align, not face recognition.** Split acceptance into software pulse-update time and mechanical settle for ~30° steps.
6. **0.5° is a PWM quantization upper bound; MG90S deadband is often larger.** At 50 Hz / 12-bit, 1 LSB ≈ 4.88 µs ≈ 0.44°. Report theory and measured deadband.
7. **Identity match on the 5MP camera only after ~2 m and face centered.** At 5 m, inter-ocular pixels are too few for reliable match; use detection/tracking only at range.
8. **Alert channel is swappable.** Prefer Telegram/ntfy for demos; WhatsApp Cloud API optional after Meta approval.

## 2. Architecture

```
Local HMI: K1–K4, OLED (I2C), R/Y/G status LEDs
LD2450 --UART 256000--> Raspberry Pi 4B (track / vision / FSM / MQTT)
PCA9685 --50 Hz PWM--> MG90S Pan/Tilt + USB camera
Pi --Wi-Fi/TLS--> Mosquitto (OCI) --> logs + phone notifications
```

Data path: radar ~10 Hz → tracker → Pan/Tilt → vision only when locked or K4 → on intrusion, one JPEG + event JSON over MQTT.

Edge keeps detecting, steering, and HMI offline; cloud is alerts, not the control loop.

## 3. Interfaces (summary)

| Device | Bus | Address / node |
| --- | --- | --- |
| HLK-LD2450 + USB-TTL | USB serial | `/dev/ttyUSB0`, 256000 8N1 |
| PCA9685A | I2C-1 | `0x40` |
| SSD1306 OLED | I2C-1 | `0x3C` (sometimes `0x3D`) |
| USB camera | UVC | `/dev/video0` MJPEG |

GPIO (avoid I2C 2/3 and console UART 14/15): K1 arm GPIO17, K2 OLED page GPIO27, K3 center GPIO22, K4 snap GPIO23; green/yellow/red status GPIO5/6/13.

PCA9685 ch0 Pan, ch1 Tilt; center ~1500 µs; software limits about Pan ±70°, Tilt ±25°.

## 4. Software stack

Raspberry Pi OS Bookworm 64-bit, Python 3, systemd. pyserial, gpiozero+lgpio, PCA9685 via CircuitPython or smbus2, luma.oled, OpenCV + YuNet/SFace, paho-mqtt TLS, local JSONL logs.

Threads: radar RX, guide/servo @50 Hz, vision ~10 fps when active, MQTT callbacks, HMI @20 Hz. Shared state under one lock; vision submits pixel error only — guide thread owns PWM.

## 5. Metrics to measure

| Metric | Concept target | Lab definition |
| --- | --- | --- |
| Track latency | ≤ 250 ms radar→coarse align | Software path separate; mechanical for small steps |
| Angular step | PWM < 0.5° | Theory ~0.44°/LSB + measured deadband |
| Intrusion confirm | >95% TP @ ~5 m | Humans positive; fan/pet/empty negative |
| Headless | No keyboard/display | OLED IP/MQTT/coords; systemd autostart |

## 6. Next implementation folders

Edge code will live under `src/` (radar, servo, vision, MQTT, HMI). Keep measurements and pin notes updated here as bring-up proceeds.
