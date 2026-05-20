FROM nvcr.io/nvidia/deepstream:8.0-gc-triton-devel

WORKDIR /opt/nvidia/deepstream/deepstream/
RUN bash user_additional_install.sh

RUN apt-get update && apt-get install -y \
    libhiredis-dev \
    libjsoncpp-dev \
    && rm -rf /var/lib/apt/lists/*

ENV REID_DIR="/opt/nvidia/deepstream/deepstream/samples/models/Tracker/"
RUN mkdir -p $REID_DIR && \
    wget -q 'https://api.ngc.nvidia.com/v2/models/nvidia/tao/reidentificationnet/versions/deployable_v1.0/files/resnet50_market1501.etlt' -P $REID_DIR

RUN git clone https://github.com/NVIDIA-AI-IOT/deepstream_tools.git && \
    git clone https://github.com/NVIDIA-AI-IOT/deepstream_reference_apps.git && \
    git clone https://github.com/marcoslucianops/DeepStream-Yolo.git

WORKDIR /opt/nvidia/deepstream/deepstream/DeepStream-Yolo
ENV CUDA_VER=12.8
RUN make -C nvdsinfer_custom_impl_Yolo clean && make -C nvdsinfer_custom_impl_Yolo

COPY worker/ /opt/deepstream-app/worker/
COPY bridge/ /opt/deepstream-app/bridge/

RUN mkdir -p /opt/nvidia/deepstream/deepstream/sources/project && \
    cp /opt/deepstream-app/worker/model_b3_gpu0_fp32.engine \
       /opt/nvidia/deepstream/deepstream/sources/project/model_b3_gpu0_fp32.engine && \
    cp /opt/deepstream-app/worker/resnet50_market1501.etlt_b100_gpu0_fp32.engine \
       /opt/nvidia/deepstream/deepstream/sources/project/resnet50_market1501.etlt_b100_gpu0_fp32.engine

WORKDIR /opt/deepstream-app/bridge
RUN make CUDA_VER=12.8

CMD ["/opt/deepstream-app/bridge/dsbridge-app", "/opt/deepstream-app/bridge/dsbridge_config.yml"]