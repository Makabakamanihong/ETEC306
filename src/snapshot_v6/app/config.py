"""ETEC306 formal program - hardware constants (技术方案.md 4.1-4.4, v0.3).

Wiring here is authoritative from sections 4.2/4.3/4.4. Do NOT use the old draft
pins (K1..K4 on GPIO17/27/22/23, LEDs on GPIO5/6/13, servos on CH0/CH1).
"""

# ---- GPIO (BCM) 4.2 ---------------------------------------------------------
# Buttons: internal pull-up, pressed = LOW, software debounce 30 ms
BTN_ARM = 22        # K1 arm / disarm      (Pin 15)
BTN_PAGE = 23       # K2 OLED page         (Pin 16)
BTN_CENTER = 24     # K3 pan-tilt center   (Pin 18)
BTN_SNAPSHOT = 25   # K4 forced snapshot   (Pin 22)
BTN_DEBOUNCE_S = 0.030
# LEDs: active HIGH
LED_GREEN = 4       # standby / armed idle (Pin 7)
LED_RED = 17        # alarm                (Pin 11)
LED_YELLOW = 27     # tracking lock        (Pin 13)

# ---- I2C-1 4.1 --------------------------------------------------------------
I2C_BUS = 1
PCA9685_ADDR = 0x40     # 0x70 seen on the bus is its ALL-CALL address, not a 3rd board
OLED_ADDR = 0x3C        # SSD1306 128x32
OLED_W, OLED_H = 128, 32

# ---- Radar 4.1 / 7.1 ---------------------------------------------------------
RADAR_PORT = "/dev/ttyUSB0"     # CH340 (1a86:7523) -> HLK-LD2450
RADAR_BAUD = 256000             # 8N1
RADAR_MAX_RANGE_MM = 6000
RADAR_MAX_AZ_DEG = 60.0
# Left/right sign of X. +1 = use the module's X as "right is positive".
# Section 11 step 3: verify with a person stepping to the right; flip to -1 if reversed.
RADAR_X_SIGN = +1   # default only; the running service uses settings.json x_sign

# ---- Camera ------------------------------------------------------------------
CAMERA_DEV = "/dev/video0"

# ---- Servos (PCA9685) 4.3 ----------------------------------------------------
PAN_CH = 4          # socket #5 counted from the LEFT
TILT_CH = 8         # socket #9 counted from the LEFT
SERVO_CENTER_US = 1500
SERVO_MIN_US = 1000              # tilt (CH8) clamp, unchanged
SERVO_MAX_US = 2000
US_PER_DEG = 1000.0 / 90.0      # pulse = 1500 - angle * 1000/90 (right/up positive)
# Pan (CH4): the user OK'd +-80 deg mechanically (2026-10-06) -> 1500 -+ 889 us. Verified on the
# real gimbal by stepping outwards and checking the picture still moves (tools/pan_range_test.py).
PAN_MIN_US = 611
PAN_MAX_US = 2389
TILT_MIN_US = SERVO_MIN_US
TILT_MAX_US = SERVO_MAX_US
PAN_LIMIT_DEG = 80.0            # software limit; step test 2026-10-06: picture still moved at 611 and 2389 us
TILT_LIMIT_DEG = 25.0
PAN_OFFSET_US = 0               # trim so 1500+offset is the mechanical center (clone osc drift)
TILT_OFFSET_US = 0
PCA_PRESCALE = 121              # 50 Hz @ 25 MHz nominal
PWM_PERIOD_US = 20000

# ---- Pointing geometry 7.2 (placeholders until step 4 calibration) ----------
SENSOR_HEIGHT_M = 1.2   # h_s: radar / pan-tilt optical axis height  -> MEASURE
TARGET_HEIGHT_M = 1.2   # h_t: chest ~1.2 m, face ~1.5 m
THETA0_DEG = 0.0        # pan mounting offset theta_0 (record in step 4)
PHI0_DEG = 0.0          # tilt mounting offset phi_0 (record in step 4)

# ---- Service (app.py) --------------------------------------------------------
HTTP_PORT = 8080
CAM_WIDTH, CAM_HEIGHT, CAM_FPS = 1280, 720, 15   # 6.x: 720p MJPEG ~15 fps for tracking view
GUIDE_HZ = 50                  # guidance / servo loop (section 6)
TRACK_CONFIRM_N, TRACK_CONFIRM_M = 10, 7         # 7.5: >=7 of last 10 frames -> confirmed
ALARM_HOLD_S = 5.0             # red stays on at least this long
ALARM_COOLDOWN_S = 30.0        # 7.5 cooldown (applied globally, IDs are not stable)
SNAP_KEEP = 300                # keep last N snapshots on disk

# ---- Vision (7.6) -------------------------------------------------------------
STILL_WIDTH, STILL_HEIGHT = 2960, 1664          # largest MJPEG mode of this UVC camera (~4.9 MP)
# UVC controls applied before streaming. 60 Hz mains in Canada (camera default is 50 Hz -> flicker bands
# under LED/fluorescent light); everything else stays at the camera defaults (auto exposure / white balance).
CAM_CONTROLS = ""   # "power_line_frequency=2" (60 Hz) doubled the camera to 30 fps / +CPU: off
MODEL_DIR = "models"
YUNET_MODEL = "face_detection_yunet_2023mar.onnx"
SFACE_MODEL = "face_recognition_sface_2021dec.onnx"
SSD_PROTO = "MobileNetSSD_deploy.prototxt"
SSD_MODEL = "MobileNetSSD_deploy.caffemodel"
FACES_DIR = "faces"
TEMP_THROTTLE_C = 75.0          # above this CPU temperature the detector drops to 2 Hz

# ---- MQTT (8.1) ---------------------------------------------------------------
MQTT_CONFIG = "mqtt.json"       # host/port/user/password/ca - created by tools, not in git
DEVICE_ID = "etec306"
HEARTBEAT_S = 10.0
