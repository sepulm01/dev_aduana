import io
import time
import logging

import torch
from PIL import Image
from fastapi import FastAPI, File, UploadFile
from fastapi.concurrency import run_in_threadpool
from transformers import AutoModelForImageTextToText, AutoProcessor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ocr-vl")

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
MODEL_PATH = "PaddlePaddle/PaddleOCR-VL-1.6"

logger.info(f"Loading model {MODEL_PATH} on {DEVICE}...")

model = AutoModelForImageTextToText.from_pretrained(
    MODEL_PATH,
    torch_dtype=torch.bfloat16,
).to(DEVICE).eval()

processor = AutoProcessor.from_pretrained(MODEL_PATH)

# Official recipe (HF model card): spotting needs upscale x2 (LANCZOS) for
# images smaller than 1500px on both sides and a larger pixel budget, or the
# thin vertical text becomes unreadable.
SPOTTING_UPSCALE_THRESHOLD = 1500
SPOTTING_MAX_PIXELS = 2048 * 28 * 28
OCR_MAX_PIXELS = 1280 * 28 * 28

try:
    MIN_PIXELS = processor.image_processor.min_pixels
except AttributeError:
    MIN_PIXELS = processor.image_processor.size["shortest_edge"]

logger.info(f"Model loaded (min_pixels={MIN_PIXELS}, "
            f"ocr_max_pixels={OCR_MAX_PIXELS}, spotting_max_pixels={SPOTTING_MAX_PIXELS})")

app = FastAPI()


@app.post("/ocr")
async def ocr_image(file: UploadFile = File(...)):
    contents = await file.read()
    return await run_in_threadpool(_process_image_sync, contents, "OCR:")


@app.post("/spotting")
async def spotting_image(file: UploadFile = File(...)):
    contents = await file.read()
    return await run_in_threadpool(_process_image_sync, contents, "Spotting:")


@app.post("/describe")
async def describe_image(file: UploadFile = File(...), prompt: str = "Describe esta escena en detalle."):
    contents = await file.read()
    return await run_in_threadpool(_process_image_sync, contents, prompt)


def _process_image_sync(contents: bytes, task: str):
    image = Image.open(io.BytesIO(contents)).convert("RGB")

    is_spotting = task.startswith("Spotting")
    if is_spotting and image.size[0] < SPOTTING_UPSCALE_THRESHOLD \
            and image.size[1] < SPOTTING_UPSCALE_THRESHOLD:
        image = image.resize(
            (image.size[0] * 2, image.size[1] * 2), Image.LANCZOS)
    max_pixels = SPOTTING_MAX_PIXELS if is_spotting else OCR_MAX_PIXELS

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image", "image": image},
                {"type": "text", "text": task},
            ],
        }
    ]
    inputs = processor.apply_chat_template(
        messages,
        add_generation_prompt=True,
        tokenize=True,
        return_dict=True,
        return_tensors="pt",
        images_kwargs={
            "size": {
                "shortest_edge": MIN_PIXELS,
                "longest_edge": max_pixels,
            }
        },
    ).to(DEVICE)

    t0 = time.time()
    with torch.no_grad():
        outputs = model.generate(**inputs, max_new_tokens=128)
    elapsed = (time.time() - t0) * 1000

    result = processor.decode(outputs[0][inputs["input_ids"].shape[-1]:-1])

    return {"text": result.strip(), "elapsed_ms": round(elapsed, 1)}


@app.get("/health")
async def health():
    return {
        "model": MODEL_PATH,
        "device": DEVICE,
        "cuda_available": torch.cuda.is_available(),
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "N/A",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5002)
