FROM nvidia/cuda:12.6.0-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive TZ=America/Santiago

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.12 python3.12-venv python3-pip curl \
    && rm -rf /var/lib/apt/lists/* \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3.12 1

WORKDIR /app

RUN pip install --break-system-packages --no-cache-dir --default-timeout=120 \
    torch torchvision \
    "transformers>=5.0.0" \
    accelerate \
    fastapi uvicorn \
    pillow requests \
    python-multipart

ENV HF_HOME=/app/.cache/huggingface

COPY server.py .

EXPOSE 5002

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "5002"]
