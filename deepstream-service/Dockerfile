# syntax=docker/dockerfile:1.4
FROM nvcr.io/nvidia/deepstream:8.0-gc-triton-devel

ENV DEEPSTREAM_DIR=/opt/nvidia/deepstream/deepstream
ENV DEEPSTREAM_YOLO_DIR=/opt/nvidia/deepstream/DeepStream-Yolo
ENV CUDA_VER=12.8

SHELL ["/bin/bash", "-c"]

# ── System deps ────────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    build-essential \
    python3-dev \
    python3-pip \
    python3-venv \
    wget \
    libgstreamer1.0-dev \
    libgstreamer-plugins-base1.0-dev \
    && rm -rf /var/lib/apt/lists/*

# ── DeepStream Python bindings (pyds) ─────────────────────────────────────────
# DS 8 ships a pre-built wheel; fall back to PyPI if not present in this variant
RUN PYDS_WHL=$(find ${DEEPSTREAM_DIR}/lib -name "pyds-*.whl" 2>/dev/null | head -1) && \
    if [ -n "$PYDS_WHL" ]; then \
        pip3 install --no-cache-dir "$PYDS_WHL"; \
    else \
        pip3 install --no-cache-dir pyds; \
    fi

# ── DeepStream-Yolo ───────────────────────────────────────────────────────────
RUN git clone https://github.com/marcoslucianops/DeepStream-Yolo.git ${DEEPSTREAM_YOLO_DIR}

WORKDIR ${DEEPSTREAM_YOLO_DIR}

# CUDA_VER must be exported explicitly for the sub-make
RUN export CUDA_VER=${CUDA_VER} && \
    make -C nvdsinfer_custom_impl_Yolo clean && \
    make -C nvdsinfer_custom_impl_Yolo

# ── Python venv ───────────────────────────────────────────────────────────────
# --system-site-packages gives the venv access to pyds (installed at system level above)
RUN python3 -m venv /opt/venv --system-site-packages && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip && \
    /opt/venv/bin/pip install --no-cache-dir \
        ultralytics \
        onnx \
        onnxslim \
        onnxruntime-gpu          # GPU-aware runtime for a CUDA container

# ── App config ────────────────────────────────────────────────────────────────
COPY config/ /opt/nvidia/deepstream/config/

WORKDIR ${DEEPSTREAM_DIR}

# ── Environment ───────────────────────────────────────────────────────────────
ENV PATH=/opt/venv/bin:${PATH}
ENV PYTHONPATH=${DEEPSTREAM_YOLO_DIR}:${PYTHONPATH}
ENV LD_LIBRARY_PATH=${DEEPSTREAM_YOLO_DIR}/nvdsinfer_custom_impl_Yolo:${LD_LIBRARY_PATH}

CMD ["/bin/bash"]