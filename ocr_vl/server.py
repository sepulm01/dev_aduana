import io
import time
import logging

import torch
from PIL import Image
from fastapi import FastAPI, File, UploadFile
from transformers import AutoModelForImageTextToText, AutoProcessor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ocr-vl")

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
MODEL_PATH = "PaddlePaddle/PaddleOCR-VL-1.6"
TASK = "OCR:"

logger.info(f"Loading model {MODEL_PATH} on {DEVICE}...")

model = AutoModelForImageTextToText.from_pretrained(
    MODEL_PATH,
    torch_dtype=torch.bfloat16,
    attn_implementation="flash_attention_2",
).to(DEVICE).eval()

processor = AutoProcessor.from_pretrained(MODEL_PATH)

logger.info("Model loaded")

app = FastAPI()


@app.post("/ocr")
async def ocr_image(file: UploadFile = File(...)):
    contents = await file.read()
    image = Image.open(io.BytesIO(contents)).convert("RGB")

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image", "image": image},
                {"type": "text", "text": TASK},
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
                "shortest_edge": processor.image_processor.min_pixels,
                "longest_edge": 1280 * 28 * 28,
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
