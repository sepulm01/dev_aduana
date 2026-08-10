FROM nvidia/cuda:12.6.0-devel-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive TZ=America/Santiago PIP_DEFAULT_TIMEOUT=1800
ENV TRITON_PTXAS_PATH=/usr/local/cuda/bin/ptxas
ENV CUDA_MODULE_LOADING=EAGER
ENV TRITON_CACHE_DIR=/tmp/.triton

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.12 python3.12-dev python3.12-venv python3-pip curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3.12 1

WORKDIR /app

RUN pip install --break-system-packages --no-cache-dir --retries 10 \
    torch torchvision && \
    pip install --break-system-packages --no-cache-dir --retries 10 \
    triton --upgrade

RUN pip install --break-system-packages --no-cache-dir --retries 10 \
    "transformers>=5.0.0" \
    accelerate \
    fastapi uvicorn \
    pillow requests \
    python-multipart

RUN rm -rf /usr/local/cuda-12.6/compat

ENV HF_HOME=/app/.cache/huggingface

COPY server.py .

EXPOSE 5002

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "5002"]
