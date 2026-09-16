#!/usr/bin/env python3
"""Gate YOLOv7-w6-pose (4 keypoints, clase sistema_cierre) sobre crops.

Carga best_pose_cierre_v2.pt usando las clases del repo parcheado
/var/www/dev_base_img/yolov7_pose (necesarias para el unpickle del
checkpoint: IKeypoint con nkpt=4). letterbox / NMS / scale_coords se
reutilizan del repo, igual que hace yolo_gate con el repo de yolov9.
"""
import sys

import cv2
import numpy as np
import torch

YOLOV7_POSE_DIR = "/var/www/dev_base_img/yolov7_pose"
MODEL_PATH = "/var/www/dev_base_img/yolov7_pose/best_pose_cierre_v3.pt"
IMGSZ = 640
CONF_THRES = 0.25
IOU_THRES = 0.45
NC = 1
NKPT = 4


class PoseGate:
    def __init__(self, model_path=MODEL_PATH, imgsz=IMGSZ,
                 conf_thres=CONF_THRES, iou_thres=IOU_THRES):
        model_path = model_path or MODEL_PATH
        # Si en el mismo proceso ya se importo models/utils de OTRO repo
        # (p.ej. yolo_gate con /var/www/yolov9), quedan cacheados en
        # sys.modules y los imports del pose resolverian al repo equivocado.
        for mod in list(sys.modules):
            if (mod == "utils" or mod.startswith("utils.") or
                    mod == "models" or mod.startswith("models.")):
                sys.modules.pop(mod, None)
        if YOLOV7_POSE_DIR not in sys.path:
            sys.path.insert(0, YOLOV7_POSE_DIR)
        import models.yolo  # noqa: F401 (clases para el unpickle del ckpt)
        from utils.datasets import letterbox
        from utils.general import (check_img_size, non_max_suppression,
                                   scale_coords)

        self._letterbox = letterbox
        self._nms = non_max_suppression
        self._scale_coords = scale_coords

        self.device = torch.device(
            "cuda:0" if torch.cuda.is_available() else "cpu")
        ckpt = torch.load(model_path, map_location="cpu", weights_only=False)
        model = ckpt["model"]
        if hasattr(model, "module"):
            model = model.module
        self.model = model.float().eval().half().to(self.device)
        # Los grid del IKeypoint son atributos planos (no buffers): el
        # checkpoint los trae persistidos del entrenamiento en CPU con la
        # forma del imgsz de entrenamiento; si calzan, NO se reconstruyen y
        # revienta el forward por device. Se mueven explicitamente a GPU.
        for m in self.model.modules():
            if isinstance(m, models.yolo.IKeypoint):
                m.grid = [g.to(self.device) for g in m.grid]
        self.stride = int(self.model.stride.max())
        self.imgsz = check_img_size(imgsz, s=self.stride)
        self.conf_thres = conf_thres
        self.iou_thres = iou_thres
        self.calls = 0

    def warmup(self):
        dummy = torch.zeros((1, 3, self.imgsz, self.imgsz),
                            device=self.device).half()
        with torch.no_grad():
            self.model(dummy)

    def detect(self, frame_bgr):
        """Devuelve array (n, 6+3*nkpt) [x1,y1,x2,y2,conf,cls,kpts...] en
        coordenadas del frame. kpts vienen como (x,y,c) por keypoint."""
        self.calls += 1
        im = self._letterbox(frame_bgr, self.imgsz, stride=self.stride)[0]
        im = im[:, :, ::-1].transpose(2, 0, 1)  # BGR->RGB, HWC->CHW
        im = np.ascontiguousarray(im)
        im = torch.from_numpy(im).to(self.device).half() / 255.0
        im = im.unsqueeze(0)
        with torch.no_grad():
            pred = self.model(im)[0]
        dets = self._nms(pred, self.conf_thres, self.iou_thres,
                         kpt_label=True, nc=NC, nkpt=NKPT)[0]
        if len(dets):
            self._scale_coords(im.shape[2:], dets[:, :4],
                               frame_bgr.shape)
            self._scale_coords(im.shape[2:], dets[:, 6:],
                               frame_bgr.shape, kpt_label=True, step=3)
        return dets.cpu().numpy()

    def kpts_de(self, det):
        """Convierte una fila (18,) en kpts (4,3) [x, y, conf]."""
        return det[6:6 + 3 * NKPT].reshape(NKPT, 3)
