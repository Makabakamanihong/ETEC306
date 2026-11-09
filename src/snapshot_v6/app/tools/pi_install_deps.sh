#!/bin/bash
# run as root: install OpenCV, numpy, paho-mqtt from Debian; download OpenCV Zoo models
LOG=/tmp/etec_apt.log
{
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3-opencv python3-numpy python3-paho-mqtt
M=/home/<user>/etec306/app/models
mkdir -p $M
cd $M
for u in \
 "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx" \
 "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx"; do
  f=$(basename $u); [ -s $f ] && [ $(stat -c %s $f) -gt 100000 ] || curl -fL --retry 3 -o $f "$u"
done
# person detector (MobileNet-SSD, Caffe) - supplement per 7.6
[ -s MobileNetSSD_deploy.caffemodel ] || curl -fL --retry 3 -o MobileNetSSD_deploy.caffemodel "https://github.com/chuanqi305/MobileNet-SSD/raw/master/mobilenet_iter_73000.caffemodel"
[ -s MobileNetSSD_deploy.prototxt ] || curl -fL --retry 3 -o MobileNetSSD_deploy.prototxt "https://raw.githubusercontent.com/chuanqi305/MobileNet-SSD/master/deploy.prototxt"
chown -R <user>:<user> $M
ls -la $M; sha256sum $M/*
python3 -c "import cv2,numpy,paho.mqtt;print('cv2',cv2.__version__,'numpy',numpy.__version__,'paho',paho.mqtt.__version__); print(hasattr(cv2,'FaceDetectorYN'),hasattr(cv2,'FaceRecognizerSF'))"
echo APT_DONE
} > $LOG 2>&1
