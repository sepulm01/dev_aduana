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

logger.info(f"Loading model {MODEL_PATH} on {DEVICE}...")

model = AutoModelForImageTextToText.from_pretrained(
    MODEL_PATH,
    torch_dtype=torch.bfloat16,
).to(DEVICE).eval()

processor = AutoProcessor.from_pretrained(MODEL_PATH)

SHORTEST_EDGE = processor.image_processor.size["shortest_edge"]
LONGEST_EDGE = processor.image_processor.size["longest_edge"]

logger.info(f"Model loaded (shortest_edge={SHORTEST_EDGE}, longest_edge={LONGEST_EDGE})")

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
                {"type": "text", "text": "OCR:"},
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
                "shortest_edge": SHORTEST_EDGE,
                "longest_edge": LONGEST_EDGE,
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
