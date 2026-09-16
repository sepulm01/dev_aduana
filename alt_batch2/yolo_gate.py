"""Gate YOLOv9 sobre frames: decide si un frame contiene alguna de las clases
del modelo (con_sello, sin_sello, cont data, container cod, truck).

Inferencia en PyTorch puro (sin ultralytics): carga best.pt usando las
clases de arquitectura del repo /var/www/yolov9 (necesarias para el
pickle del checkpoint). letterbox / NMS / scale_boxes adaptados inline
de ese repo para no depender de sus imports pesados.
"""
import sys

import cv2
import numpy as np
import torch
import torchvision

YOLOV9_DIR = "/var/www/yolov9"
MODEL_PATH = "/var/www/dev_aduana/computer_vision/models/yolov9_aduana/best.pt"
IMGSZ = 1280
CONF_THRES = 0.20
IOU_THRES = 0.45
STRIDE = 32


def _letterbox(im, new_shape=(1280, 1280), color=(114, 114, 114), stride=32):
    shape = im.shape[:2]
    r = min(new_shape[0] / shape[0], new_shape[1] / shape[1])
    new_unpad = int(round(shape[1] * r)), int(round(shape[0] * r))
    dw, dh = new_shape[1] - new_unpad[0], new_shape[0] - new_unpad[1]
    dw, dh = np.mod(dw, stride), np.mod(dh, stride)
    dw, dh = dw / 2, dh / 2
    if shape[::-1] != new_unpad:
        im = cv2.resize(im, new_unpad, interpolation=cv2.INTER_LINEAR)
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    im = cv2.copyMakeBorder(im, top, bottom, left, right,
                            cv2.BORDER_CONSTANT, value=color)
    return im


def _xywh2xyxy(x):
    y = x.clone()
    y[:, 0] = x[:, 0] - x[:, 2] / 2
    y[:, 1] = x[:, 1] - x[:, 3] / 2
    y[:, 2] = x[:, 0] + x[:, 2] / 2
    y[:, 3] = x[:, 1] + x[:, 3] / 2
    return y


def _clip_boxes(boxes, shape):
    boxes[:, [0, 2]] = boxes[:, [0, 2]].clamp(0, shape[1])
    boxes[:, [1, 3]] = boxes[:, [1, 3]].clamp(0, shape[0])


def _scale_boxes(img1_shape, boxes, img0_shape):
    gain = min(img1_shape[0] / img0_shape[0], img1_shape[1] / img0_shape[1])
    pad = (img1_shape[1] - img0_shape[1] * gain) / 2, (img1_shape[0] - img0_shape[0] * gain) / 2
    boxes[:, [0, 2]] -= pad[0]
    boxes[:, [1, 3]] -= pad[1]
    boxes[:, :4] /= gain
    _clip_boxes(boxes, img0_shape)


def _nms(prediction, conf_thres=0.40, iou_thres=0.45, max_det=300):
    """NMS estilo yolov9 para batch=1. Entrada: tensor (1, 4+nc, N) (puede
    venir envuelto en listas/tuplas del head dual). Salida: lista con
    tensor (n,6) [xyxy, conf, cls]."""
    if isinstance(prediction, (list, tuple)):
        prediction = prediction[0]
    if isinstance(prediction, (list, tuple)):
        prediction = prediction[0]
    prediction = prediction[0]  # batch dim (batch=1)
    nc = prediction.shape[0] - 4
    xc = prediction[4:4 + nc].amax(0) > conf_thres
    x = prediction.T[xc]
    if not x.shape[0]:
        return [torch.zeros((0, 6), device=prediction.device)]

    box, cls = x.split((4, nc), 1)
    box = _xywh2xyxy(box)
    conf, j = cls.max(1, keepdim=True)
    x = torch.cat((box, conf, j.float()), 1)[conf.view(-1) > conf_thres]
    if not x.shape[0]:
        return [torch.zeros((0, 6), device=prediction.device)]

    i = torchvision.ops.nms(x[:, :4], x[:, 4], iou_thres)
    i = i[:max_det]
    return [x[i]]


class YoloGate:
    def __init__(self, model_path=MODEL_PATH, imgsz=IMGSZ,
                 conf_thres=CONF_THRES, iou_thres=IOU_THRES):
        model_path = model_path or MODEL_PATH
        if YOLOV9_DIR not in sys.path:
            sys.path.insert(0, YOLOV9_DIR)
        import models.yolo  # noqa: F401  (clases para el unpickle del ckpt)

        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        ckpt = torch.load(model_path, map_location="cpu", weights_only=False)
        model = ckpt["model"]
        if hasattr(model, "module"):
            model = model.module
        self.model = model.float().eval().half().to(self.device)
        self.imgsz = imgsz
        self.conf_thres = conf_thres
        self.iou_thres = iou_thres
        self.calls = 0

    def warmup(self):
        dummy = torch.zeros((1, 3, self.imgsz, self.imgsz),
                            device=self.device).half()
        with torch.no_grad():
            self.model(dummy)

    def detect(self, frame_bgr):
        """Devuelve array (n,6) [x1,y1,x2,y2,conf,cls] en coords del frame."""
        self.calls += 1
        im = _letterbox(frame_bgr, new_shape=(self.imgsz, self.imgsz), stride=STRIDE)
        im = im[:, :, ::-1].transpose(2, 0, 1)  # BGR->RGB, HWC->CHW
        im = np.ascontiguousarray(im)
        im = torch.from_numpy(im).to(self.device).half() / 255.0
        im = im[None]
        with torch.no_grad():
            pred = self.model(im)
        # El head es dual (DDetect): devuelve (lista_y, lista_x) donde
        # lista_y = [y_main, y_aux]. Se concatenan AMBAS cabezas antes del
        # NMS: la auxiliar suele ver objetos que la principal pierde
        # (p.ej. camion 0.73 en aux vs 0.28 en main).
        pred = self._desenvolver(pred)
        dets = _nms(pred, self.conf_thres, self.iou_thres)[0]
        if len(dets):
            _scale_boxes(im.shape[2:], dets[:, :4], frame_bgr.shape[:2])
        return dets.cpu().numpy()

    def _desenvolver(self, pred):
        if isinstance(pred, (list, tuple)):
            pred = pred[0]
        if isinstance(pred, (list, tuple)):
            tensors = [t for t in pred if isinstance(t, torch.Tensor)]
            pred = torch.cat(tensors, dim=2)
        return pred

    def detect_batch(self, frames):
        """YOLO sobre un lote de frames BGR en UNA forward por lote.
        Amortiza el overhead de lanzamiento por llamada (la GPU queda casi
        ociosa procesando frame a frame). Devuelve lista de arrays (n,6)
        por frame, en coords del frame original."""
        self.calls += len(frames)
        ims = []
        for frame in frames:
            im = _letterbox(frame, new_shape=(self.imgsz, self.imgsz), stride=STRIDE)
            im = im[:, :, ::-1].transpose(2, 0, 1)
            im = np.ascontiguousarray(im)
            ims.append(im)
        im = torch.from_numpy(np.stack(ims)).to(self.device).half() / 255.0
        with torch.no_grad():
            pred = self.model(im)
        pred = self._desenvolver(pred)
        salida = []
        for i in range(im.shape[0]):
            dets = _nms(pred[i:i + 1], self.conf_thres, self.iou_thres)[0]
            if len(dets):
                _scale_boxes((im.shape[2], im.shape[3]), dets[:, :4],
                             frames[i].shape[:2])
            salida.append(dets.cpu().numpy())
        return salida

    def has_class(self, frame_bgr):
        """(bool, dets): True si hay al menos una deteccion sobre conf_thres."""
        dets = self.detect(frame_bgr)
        return len(dets) > 0, dets
