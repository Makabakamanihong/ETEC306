"""General object + hand detection (OpenCV Zoo models, run through cv2.dnn on the Pi 4B CPU).

NanoDet-Plus-m 416 (COCO 80 classes: person, chair, couch, bed, tv, laptop, dining table, refrigerator, book ...)
MediaPipe palm detector 192 (hands) -- a visible hand means a person is there even if the face is not.
Both return boxes normalised to the frame (x, y, w, h in 0..1)."""
from __future__ import annotations

import numpy as np

try:
    import cv2
except ImportError:          # pragma: no cover
    cv2 = None

COCO = ('person', 'bicycle', 'car', 'motorcycle', 'airplane', 'bus', 'train', 'truck', 'boat', 'traffic light',
        'fire hydrant', 'stop sign', 'parking meter', 'bench', 'bird', 'cat', 'dog', 'horse', 'sheep', 'cow',
        'elephant', 'bear', 'zebra', 'giraffe', 'backpack', 'umbrella', 'handbag', 'tie', 'suitcase', 'frisbee',
        'skis', 'snowboard', 'sports ball', 'kite', 'baseball bat', 'baseball glove', 'skateboard', 'surfboard',
        'tennis racket', 'bottle', 'wine glass', 'cup', 'fork', 'knife', 'spoon', 'bowl', 'banana', 'apple',
        'sandwich', 'orange', 'broccoli', 'carrot', 'hot dog', 'pizza', 'donut', 'cake', 'chair', 'couch',
        'potted plant', 'bed', 'dining table', 'toilet', 'tv', 'laptop', 'mouse', 'remote', 'keyboard',
        'cell phone', 'microwave', 'oven', 'toaster', 'sink', 'refrigerator', 'book', 'clock', 'vase', 'scissors',
        'teddy bear', 'hair drier', 'toothbrush')
CN = {'person': '人', 'chair': '椅子', 'couch': '沙发', 'bed': '床', 'tv': '电视', 'laptop': '笔记本',
      'dining table': '桌子', 'refrigerator': '冰箱', 'book': '书', 'bottle': '瓶子', 'cup': '杯子',
      'cell phone': '手机', 'keyboard': '键盘', 'mouse': '鼠标', 'backpack': '背包', 'handbag': '包',
      'suitcase': '箱子', 'potted plant': '盆栽', 'clock': '钟', 'cat': '猫', 'dog': '狗', 'toilet': '马桶',
      'sink': '水池', 'microwave': '微波炉', 'oven': '烤箱', 'remote': '遥控器', 'umbrella': '伞', 'vase': '花瓶',
      'teddy bear': '玩偶', 'scissors': '剪刀', 'bench': '长凳', 'hand': '手'}


class NanoDet:
    SIZE = 416
    STRIDES = (8, 16, 32, 64)
    REG_MAX = 7

    def __init__(self, path, prob=0.35, iou=0.6):
        self.net = cv2.dnn.readNet(str(path))
        self.prob, self.iou = prob, iou
        self.mean = np.array([103.53, 116.28, 123.675], np.float32).reshape(1, 1, 3)
        self.std = np.array([57.375, 57.12, 58.395], np.float32).reshape(1, 1, 3)
        self.anchors = []
        for s in self.STRIDES:
            n = self.SIZE // s
            xv, yv = np.meshgrid(np.arange(n) * s, np.arange(n) * s)
            self.anchors.append(np.column_stack((xv.flatten() + 0.5 * (s - 1), yv.flatten() + 0.5 * (s - 1))))
        self.project = np.arange(self.REG_MAX + 1)
        self.out_names = self.net.getUnconnectedOutLayersNames()

    def detect(self, img, prob=None):
        """img: BGR any size. Returns [{label, cn, score, x, y, w, h}] normalised."""
        H, W = img.shape[:2]
        S = self.SIZE
        scale = S / max(H, W)
        nh, nw = int(round(H * scale)), int(round(W * scale))
        top, left = (S - nh) // 2, (S - nw) // 2
        canvas = np.zeros((S, S, 3), np.uint8)
        canvas[top:top + nh, left:left + nw] = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
        blob = cv2.dnn.blobFromImage((canvas.astype(np.float32) - self.mean) / self.std)
        self.net.setInput(blob)
        outs = self.net.forward(self.out_names)
        # pair outputs by shape (the output order differs between OpenCV versions)
        cls_by_n, reg_by_n = {}, {}
        for o in outs:
            o = o.reshape(-1, o.shape[-1])
            (cls_by_n if o.shape[1] == len(COCO) else reg_by_n)[o.shape[0]] = o
        boxes, scores = [], []
        for s, anc in zip(self.STRIDES, self.anchors):
            n = len(anc)
            if n not in cls_by_n or n not in reg_by_n:
                continue
            cls, reg = cls_by_n[n], reg_by_n[n]
            keep = cls.max(axis=1) > (prob or self.prob) * 0.8
            if not keep.any():
                continue
            cls, reg, anc = cls[keep], reg[keep], anc[keep]
            e = np.exp(reg.reshape(-1, self.REG_MAX + 1))
            d = (e / e.sum(axis=1, keepdims=True)) @ self.project
            d = d.reshape(-1, 4) * s
            boxes.append(np.column_stack([anc[:, 0] - d[:, 0], anc[:, 1] - d[:, 1],
                                          anc[:, 0] + d[:, 2], anc[:, 1] + d[:, 3]]))
            scores.append(cls)
        if not boxes:
            return []
        b = np.clip(np.concatenate(boxes), 0, S)
        sc = np.concatenate(scores)
        cid, conf = sc.argmax(axis=1), sc.max(axis=1)
        wh = b.copy()
        wh[:, 2:] -= wh[:, :2]
        idx = cv2.dnn.NMSBoxesBatched(wh.tolist(), conf.tolist(), cid.tolist(), prob or self.prob, self.iou) \
            if hasattr(cv2.dnn, "NMSBoxesBatched") else cv2.dnn.NMSBoxes(wh.tolist(), conf.tolist(), prob or self.prob, self.iou)
        out = []
        for i in np.array(idx).flatten():
            x1, y1, x2, y2 = (float(v) for v in b[i])       # plain floats (float32 breaks json.dumps)
            x1, x2 = (x1 - left) / scale / W, (x2 - left) / scale / W
            y1, y2 = (y1 - top) / scale / H, (y2 - top) / scale / H
            lab = COCO[int(cid[i])]
            out.append({"label": lab, "cn": CN.get(lab, lab), "score": round(float(conf[i]), 2),
                        "x": round(max(0.0, x1), 4), "y": round(max(0.0, y1), 4),
                        "w": round(min(1.0, x2) - max(0.0, x1), 4), "h": round(min(1.0, y2) - max(0.0, y1), 4)})
        return out


class PalmDet:
    SIZE = 192

    def __init__(self, path, score=0.6, nms=0.3):
        self.net = cv2.dnn.readNet(str(path))
        self.score, self.nms = score, nms
        a = []
        for n, k in ((24, 2), (12, 6)):          # SSD anchors of the MediaPipe palm model (2016)
            for y in range(n):
                for x in range(n):
                    a += [((x + 0.5) / n, (y + 0.5) / n)] * k
        self.anchors = np.array(a, np.float32)
        self.out_names = self.net.getUnconnectedOutLayersNames()

    def detect(self, img, score=None):
        H, W = img.shape[:2]
        S = self.SIZE
        r = S / max(H, W)
        nh, nw = int(H * r), int(W * r)
        top, left = (S - nh) // 2, (S - nw) // 2
        canvas = np.zeros((S, S, 3), np.uint8)
        canvas[top:top + nh, left:left + nw] = cv2.resize(img, (nw, nh))
        blob = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32)[np.newaxis] / 255.0
        self.net.setInput(blob)
        outs = self.net.forward(self.out_names)
        reg, sc = (outs[0], outs[1]) if outs[0].shape[-1] == 18 else (outs[1], outs[0])
        sc = 1 / (1 + np.exp(-sc[0, :, 0].astype(np.float64)))
        d = reg[0, :, :4] / S
        c = d[:, :2] + self.anchors
        b = np.column_stack([c - d[:, 2:] / 2, c + d[:, 2:] / 2]) * S          # in canvas px
        wh = b.copy()
        wh[:, 2:] -= wh[:, :2]
        idx = cv2.dnn.NMSBoxes(wh.tolist(), sc.tolist(), score or self.score, self.nms)
        out = []
        for i in np.array(idx).flatten():
            x1, y1, x2, y2 = (float(v) for v in b[i])       # plain floats (float32 breaks json.dumps)
            x1, x2 = (x1 - left) / r / W, (x2 - left) / r / W
            y1, y2 = (y1 - top) / r / H, (y2 - top) / r / H
            out.append({"label": "hand", "cn": "手", "score": round(float(sc[i]), 2),
                        "x": round(max(0.0, x1), 4), "y": round(max(0.0, y1), 4),
                        "w": round(min(1.0, x2) - max(0.0, x1), 4), "h": round(min(1.0, y2) - max(0.0, y1), 4)})
        return out


class ObjectThread:
    """Runs NanoDet (+ palm detector) on the newest frame at obj_hz while app.objects_active()."""

    def __init__(self, app, model_dir):
        import threading
        self.app = app
        self.nano = NanoDet(model_dir / "object_detection_nanodet_2022nov.onnx")
        pp = model_dir / "palm_detection_mediapipe_2023feb.onnx"
        self.palm = PalmDet(pp) if pp.exists() else None
        self.result = {"t": 0.0, "items": [], "ms": 0, "hand_ms": 0}
        self.rate = 0.0
        self._times = []
        self.thread = threading.Thread(target=self.run, daemon=True, name="objects")

    def start(self):
        self.thread.start()

    def run(self):
        import time
        import config as C
        last_seq = -1
        while self.app.running:
            s = self.app.settings.get()
            if not (s["vision_enabled"] and s["obj_enabled"] and self.app.objects_active()):
                time.sleep(0.2)
                continue
            hz = s["obj_hz"] if self.app.cpu_temp() < C.TEMP_THROTTLE_C else min(0.5, s["obj_hz"])
            t0 = time.monotonic()
            seq, jpg = self.app.camera.wait_frame(last_seq, 1.0)
            if jpg is None or seq == last_seq:
                continue
            last_seq = seq
            frame_t = self.app.camera.frame_t          # pose lookup uses the time THIS frame arrived
            t0 = time.monotonic()
            try:
                img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_REDUCED_COLOR_2)
                items = self.nano.detect(img, s["obj_conf"])
                t1 = time.monotonic()
                if self.palm is not None and s["hand_detect"]:
                    items += self.palm.detect(img, s["hand_conf"])
                now = time.monotonic()
                self.result = {"t": now, "items": items, "ms": int((t1 - t0) * 1000),
                               "hand_ms": int((now - t1) * 1000), "frame_t": frame_t}
                self._times = [x for x in self._times if now - x < 4.0] + [now]
                self.rate = (len(self._times) - 1) / max(1e-3, self._times[-1] - self._times[0]) if len(self._times) > 1 else 0.0
                self.app.hw_error.pop("objects", None)
            except Exception as exc:
                self.app.hw_error["objects"] = repr(exc)
            time.sleep(max(0.0, 1.0 / hz - (time.monotonic() - t0)))


if __name__ == "__main__":
    import sys, time
    from pathlib import Path
    m = Path(__file__).resolve().parent / "models"
    nd = NanoDet(m / "object_detection_nanodet_2022nov.onnx")
    pd = PalmDet(m / "palm_detection_mediapipe_2023feb.onnx")
    for p in sys.argv[1:]:
        img = cv2.imread(p)
        img = cv2.resize(img, (640, int(640 * img.shape[0] / img.shape[1])))
        for _ in range(2):
            t = time.monotonic(); o = nd.detect(img); t1 = (time.monotonic() - t) * 1000
            t = time.monotonic(); h = pd.detect(img); t2 = (time.monotonic() - t) * 1000
        print(f"{Path(p).name}: nanodet {t1:.0f} ms {[(x['label'], x['score']) for x in o]} | palm {t2:.0f} ms "
              f"{[(x['score'], x['x'], x['y'], x['w']) for x in h]}")
