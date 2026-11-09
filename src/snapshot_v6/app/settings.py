"""Live-tunable settings, persisted to settings.json next to this file."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

PATH = Path(__file__).resolve().parent / "settings.json"

# key: (type, min, max, default, label); GROUPS gives the order/sections on settings.html
SPEC = {
    # --- direction / geometry (7.2, 4.3)  -- the calibration page writes these
    "x_sign":          (int,   -1,   1,    -1,   "雷达 X 方向 (-1 = 原始 X 取反后为右)"),
    "pan_sign":        (int,   -1,   1,    1,    "水平舵机方向 (+1 = 角度+ 脉宽变小 镜头向右, 方向自检会改)"),
    "tilt_sign":       (int,   -1,   1,    1,    "俯仰舵机方向 (+1 = 角度+ 脉宽变小 镜头向上; 实测+自检本机为 +1)"),
    "az_scale":        (float, 0.5,  1.5,  1.0,  "雷达方位→水平角 比例 (标定拟合)"),
    "theta0_deg":      (float, -30,  30,   0.0,  "水平安装偏角 θ0 (°, 标定拟合)"),
    "phi0_deg":        (float, -30,  30,   0.0,  "俯仰安装偏角 φ0 (°, 标定拟合)"),
    "sensor_height_m": (float, 0.1,  3.0,  1.0,  "雷达/云台安装高度 h_s (m)"),
    "target_height_m": (float, 0.1,  2.5,  1.3,  "瞄准高度 h_t (m, 标定按人脸拟合)"),
    "pan_offset_us":   (int,   -300, 300,  0,    "CH4 中位脉宽微调 (µs)"),
    "tilt_offset_us":  (int,   -300, 300,  0,    "CH8 中位脉宽微调 (µs)"),
    # --- radar tracking (7.3)
    "use_kalman":      (bool,  0,    1,    True, "使用卡尔曼滤波 (关 = 指数平滑)"),
    "kf_accel_mps2":   (float, 0.1,  10,   1.0,  "卡尔曼过程噪声: 加速度 (m/s²)"),
    "kf_meas_mm":      (float, 10,   500,  100,  "卡尔曼量测噪声 (mm)"),
    "pos_alpha":       (float, 0.05, 1.0,  0.5,  "指数平滑 α (仅关闭卡尔曼时)"),
    "merge_m":         (float, 0.0,  1.0,  0.35, "同一个人的多个雷达点合并距离 (m, 手臂/腿)"),
    "jump_m":          (float, 0.1,  2.0,  0.35, "单帧跳变门限 (m, 超过则降权, 连续 3 帧才接受)"),
    "gate_m":          (float, 0.2,  2.0,  0.7,  "航迹关联门限 (m)"),
    "track_drop_s":    (float, 0.2,  5.0,  1.0,  "航迹多久无量测删除 (s)"),
    "switch_margin_m": (float, 0.0,  3.0,  0.5,  "换主目标要好多少 (m, 滞回)"),
    "switch_hold_s":   (float, 0.0,  5.0,  0.8,  "换主目标要持续多久 (s)"),
    "min_range_m":     (float, 0.0,  3.0,  0.5,  "有效最小距离 (m, 屏蔽云台本身反射)"),
    "max_range_m":     (float, 0.5,  6.0,  6.0,  "有效最大距离 (m)"),
    # --- guidance (7.4)
    "lowpass_hz":      (float, 0.2,  10.0, 3.0,  "角度指令低通截止 (Hz)"),
    "max_rate_dps":    (float, 10,   360,  120,  "云台最大角速度 (°/s)"),
    "deadband_deg":    (float, 0.0,  5.0,  1.5,  "雷达跟踪角度死区 (°)"),
    "return_center_s": (float, 0.5,  60,   3.0,  "无目标多少秒后归中 (s)"),
    "vision_priority": (bool,  0,    1,    True, "看到人脸/人体后改用视觉跟踪, 不再跟雷达"),
    "vis_track_gain":  (float, 0.1,  1.0,  0.6,  "视觉跟踪增益 (每次检测修正误差的比例)"),
    "vis_deadband_px": (float, 0,    200,  25,   "视觉死区 (像素, 1280×720)"),
    "vis_lost_s":      (float, 0.3,  10,   1.5,  "视觉丢失多久交回雷达 (s)"),
    "verify_s":        (float, 0.5,  10,   2.5,  "云台对准雷达目标后多久看不到人就判为物体 (s)"),
    "object_ignore_s": (float, 5,    600,  60,   "判为物体(非人)的静止目标忽略多久 (s)"),
    # --- patrol
    "patrol_enabled":  (bool,  0,    1,    True, "无目标时巡逻 (左右扫视找人)"),
    "patrol_idle_s":   (float, 5,    600,  30,   "无目标多久开始巡逻 (s)"),
    "patrol_range_deg": (float, 5,   80,   80,   "巡逻水平范围 ± (°，最大 = 水平限位 80°)"),
    "patrol_step_deg": (float, 5,    80,   40,   "巡逻每步转多少 (°，镜头视场约 76°，40° 有重叠)"),
    "patrol_dwell_s":  (float, 0.5,  10,   2.0,  "每个位置停留 (s, 停下来检测)"),
    "patrol_tilt_deg": (float, -25,  25,   0.0,  "巡逻俯仰角 (°)"),
    # --- vision (7.4 / 7.6)
    "vision_enabled":  (bool,  0,    1,    True, "启用视觉 (人脸/物体检测/比对)"),
    "detect_always":   (bool,  0,    1,    False, "一直检测 (关 = 只在有目标/巡逻/录入时检测)"),
    "vision_hz":       (float, 1,    15,   8,    "人脸检测频率上限 (Hz)"),
    "face_score":      (float, 0.5,  0.99, 0.8,  "YuNet 置信度门限"),
    "face_weak_score": (float, 0.3,  0.99, 0.55, "低置信人脸（侧脸）门限：只用来继续跟踪已锁定的人"),
    "head_track":      (bool,  0,    1,    True, "脸转开后继续跟踪头部 (侧脸检测 + KCF 跟踪)"),
    "head_track_s":    (float, 0.5,  15,   3.0,  "只靠 KCF 跟踪头部最多多久 (s，看到侧脸会续时)"),
    "obj_enabled":     (bool,  0,    1,    True, "物体识别 (NanoDet: 人/椅子/床/笔记本…)"),
    "obj_hz":          (float, 0.2,  5,    1.5,  "物体识别频率 (Hz)"),
    "obj_conf":        (float, 0.2,  0.9,  0.4,  "物体识别置信度门限"),
    "person_conf":     (float, 0.3,  0.95, 0.55, "人体确认门限 (低于它只显示不锁定)"),
    "person_frames":   (int,   1,    6,    2,    "人体需连续检出几次才算人 (无雷达对应时 +1)"),
    "hand_detect":     (bool,  0,    1,    True, "手部检测 (只用来提示人在哪个雷达目标，不单独告警)"),
    "hand_conf":       (float, 0.3,  0.95, 0.8,  "手部检测置信度门限"),
    "cam_hfov_deg":    (float, 30,   150,  100,  "摄像头水平视场 (°, 方向自检会估计)"),
    "cam_vfov_deg":    (float, 20,   120,  68,   "摄像头垂直视场 (°, 方向自检会估计)"),
    # --- identity (7.6)
    "identify_range_m": (float, 0.3, 6.0,  4.0,  "身份比对距离门限 r (m)"),
    "face_threshold":  (float, 0.1,  0.9,  0.363, "SFace 余弦相似度阈值"),
    "min_eye_px":      (float, 0,    200,  40,   "比对最小眼距 (全分辨率像素)"),
    "enroll_min_eye_px": (float, 0,  200,  30,   "手机/电脑录入最小眼距 (像素)"),
    "identify_max_yaw_deg": (float, 5, 75,  60,   "只比对接近正面的脸：左右转头最大角度 (°)"),
    "enroll_max_yaw_deg": (float, 5, 60,   45,   "录入时允许的转头角度 (°，录入会要求正面+左右各几张)"),
    "identify_attempts": (int, 1,    5,    3,    "每个目标最多比对次数"),
    "face_side_margin": (float, 0.0, 0.3,  0.05, "转头超过 30° 时阈值加多少"),
    "face_vote_margin": (float, 0.0, 0.2,  0.04, "多帧投票：两次相似度都在阈值减这个值以上也算认出 (0 = 关闭)"),
    "identify_wait_s": (float, 0,    120,  15,   "近处的人一直不露正脸（侧脸/背对）多久后按未知处理 (s，0 = 一直等)"),
    # --- active search when radar has a target but vision sees nobody
    "search_enabled":  (bool, 0,    1,    True, "雷达有目标但画面里没人时，在雷达方位附近主动搜索"),
    "search_pan_deg":  (float, 0,   40,   18,   "搜索水平范围 ± (°)"),
    "search_tilt_deg": (float, 0,   25,   10,   "搜索俯仰步长 (°)"),
    "search_dwell_s":  (float, 0.2, 3.0,  0.8,  "搜索每个位置停留 (s)"),
    "search_after_s":  (float, 0.2, 10,   1.5,  "雷达目标对准后多久没看到人才开始搜索 (s)"),
    "lead_s":          (float, 0.0, 1.0,  0.25, "按卡尔曼速度提前量 (s，补偿云台滞后)"),
    # --- power saving
    "camera_sleep_s":  (float, 0,   3600, 60,   "布防且无目标多久后摄像头休眠 (s，0 = 不休眠)"),
    "patrol_while_sleep": (bool, 0, 1,    False, "摄像头休眠时仍巡逻（会唤醒摄像头）"),
    "metrics_log_s":   (float, 0,   600,  10,   "功耗/性能记录间隔 (s，0 = 不记录)"),
    # --- alarm / snapshots (7.5)
    "alarm_far_detect": (bool, 0,    1,    True, "比对距离外看到人就告警 (7.5)"),
    "alarm_confirm_s": (float, 0.0,  5.0,  1.0,  "看到人持续多久才告警 (s, 防误报)"),
    "far_alarm_delay_s": (float, 0.0, 20.0, 4.0, "看不到可比对的脸时，等多久才按“身份未知”告警 (s，给比对留时间)"),
    "known_hold_s":    (float, 0.0,  600,  120,  "认出家人后多久内，看不到脸的人体不告警 (s)"),
    "alarm_cooldown_s": (float, 5,   600,  30,   "同一目标告警冷却 (s, 7.5)"),
    "alarm_range_m":   (float, 0.0,  6.0,  0.0,  "无视觉的接近告警距离 (m, 0 = 关闭)"),
    "snapshot_on_lock": (bool, 0,    1,    True, "确认是人后自动抓拍"),
    "snapshot_delay_s": (float, 0.0, 3.0,  0.5,  "确认后延迟抓拍 (s)"),
    "snapshot_min_interval_s": (float, 0.0, 300, 15, "自动抓拍最小间隔 (s)"),
    # --- misc
    "upload_manual_snap": (bool, 0, 1,  True,  "K4/网页抓拍的图片也上传云端（7.5）"),
    "stream_fps":      (float, 1,    30,   10,   "网页视频帧率上限"),
    "mqtt_track_hz":   (float, 0,    5,    2,    "MQTT 轨迹上报频率 (Hz, 8.1 建议 ≤2)"),
    "arm_on_boot":     (bool,  0,    1,    True, "开机即布防"),
}

GROUPS = [("方向与几何（标定会自动写）", "x_sign"), ("雷达跟踪", "use_kalman"), ("云台引导 / 视觉跟踪", "lowpass_hz"),
          ("巡逻", "patrol_enabled"), ("视觉检测", "vision_enabled"), ("身份比对", "identify_range_m"),
          ("告警 / 抓拍", "alarm_far_detect"), ("其他", "upload_manual_snap")]


def defaults() -> dict:
    return {k: v[3] for k, v in SPEC.items()}


def coerce(key: str, value):
    if key not in SPEC:
        raise KeyError(f"未知设置 {key}")
    typ, lo, hi, _, _ = SPEC[key]
    if typ is bool:
        if isinstance(value, str):
            value = value.strip().lower() in ("1", "true", "on", "yes")
        return bool(value)
    v = typ(float(value))
    if key.endswith("_sign"):
        v = -1 if v < 0 else 1
    return max(lo, min(hi, v))


class Settings:
    def __init__(self, path: Path = PATH):
        self.path = path
        self.lock = threading.Lock()
        self.data = defaults()
        try:
            stored = json.loads(self.path.read_text())
            for k, v in stored.items():
                if k in SPEC:
                    self.data[k] = coerce(k, v)
        except FileNotFoundError:
            self.save()
        except Exception as exc:
            print("settings.json unreadable, using defaults:", exc)

    def get(self) -> dict:
        with self.lock:
            return dict(self.data)

    def update(self, changes: dict) -> dict:
        with self.lock:
            for k, v in changes.items():
                self.data[k] = coerce(k, v)
            self._save_locked()
            return dict(self.data)

    def reset(self) -> dict:
        with self.lock:
            self.data = defaults()
            self._save_locked()
            return dict(self.data)

    def save(self):
        with self.lock:
            self._save_locked()

    def _save_locked(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False))
        os.replace(tmp, self.path)

    @staticmethod
    def spec_for_ui() -> list:
        first = {k: g for g, k in GROUPS}
        return [{"key": k, "type": v[0].__name__, "min": v[1], "max": v[2], "default": v[3], "label": v[4],
                 "group": first.get(k)} for k, v in SPEC.items()]
