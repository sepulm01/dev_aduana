#!/usr/bin/env python3
"""Servicio OCR liviano (PaddleOCR 2.7.3) para alt_batch4 — puerto 5004.

POST /ocr  (multipart file) -> {"texts": [...], "ms": ...}
"""
import io
import threading
import time

import cv2
import numpy as np
from fastapi import FastAPI, File, UploadFile
from fastapi.concurrency import run_in_threadpool
from paddleocr import PaddleOCR

app = FastAPI()
ocr = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
_lock = threading.Lock()


def _leer(contents: bytes):
    with _lock:
        t0 = time.time()
        img = cv2.imdecode(np.frombuffer(contents, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return {"texts": [], "ms": 0}
        r = ocr.ocr(img, cls=True)
        lineas = []
        for page in (r or []):
            for line in (page or []):
                texto = line[1][0] if len(line) > 1 and len(line[1]) > 0 else ""
                if texto:
                    lineas.append(str(texto))
        return {"texts": lineas, "ms": round((time.time() - t0) * 1000)}


@app.post("/ocr")
async def leer(file: UploadFile = File(...)):
    contents = await file.read()
    return await run_in_threadpool(_leer, contents)


@app.get("/health")
async def health():
    return {"motor": "paddleocr", "ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5004)
