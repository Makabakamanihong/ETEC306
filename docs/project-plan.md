# Project plan — ETEC306 Capstone

Phased checklist grounded in the technical design for the edge–cloud security & tracking system.

## Phase 1 — Hardware bring-up

- [ ] Pi 4B boots headless; systemd service skeleton ready
- [ ] LD2450 on `/dev/ttyUSB0` at 256000 8N1
- [ ] `i2cdetect -y 1` shows PCA9685 `0x40` and OLED `0x3C` (or `0x3D`)
- [ ] Servo power on PCA9685 V+ from power-bank A port; logic 3.3 V; single-point common ground
- [ ] OLED shows IP / MQTT / status pages; keys K1–K4 debounce OK

## Phase 2 — Radar tracking loop

- [ ] Parse LD2450 frames (~10 Hz, up to 3 targets)
- [ ] Associate targets; select primary track
- [ ] Convert X,Y to Pan; geometric Tilt from mount height + assumed person height
- [ ] Write PCA9685 channels 0/1 at 50 Hz with Pan ±70° / Tilt ±25° software limits
- [ ] Measure software latency toward ≤250 ms coarse-align goal

## Phase 3 — Vision confirm & identity

- [ ] Open camera early (720p MJPEG); run detector only when locked or K4 pressed
- [ ] Face/body box center error → small pan/tilt correction
- [ ] Whitelist identity compare only when radar range ≤ ~2 m and face centered
- [ ] At ~5 m: detection / intrusion only (no identity claim)

## Phase 4 — MQTT & alerts

- [ ] TLS MQTT to Mosquitto on OCI; heartbeat + event JSON
- [ ] On confirmed intrusion: one JPEG + event publish
- [ ] Notification adapter (Telegram/ntfy first; WhatsApp optional)

## Phase 5 — Integration & demo

- [ ] End-to-end demo: radar cue → gimbal → vision confirm → alert
- [ ] OLED / traffic-light status story for headless run
- [ ] Update README and technical design with measured numbers
- [ ] Presentation-ready GitHub project board and commit history
