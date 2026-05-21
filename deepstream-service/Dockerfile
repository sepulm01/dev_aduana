FROM nvcr.io/nvidia/deepstream:8.0-gc-triton-devel

WORKDIR /opt/nvidia/deepstream/deepstream/
RUN bash user_additional_install.sh

RUN apt-get update && apt-get install -y \
    libjsoncpp-dev \
    && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/NVIDIA-AI-IOT/deepstream_tools.git && \
    git clone https://github.com/NVIDIA-AI-IOT/deepstream_reference_apps.git && \
    git clone https://github.com/marcoslucianops/DeepStream-Yolo.git

WORKDIR /opt/nvidia/deepstream/deepstream/DeepStream-Yolo
ENV CUDA_VER=12.8
RUN make -C nvdsinfer_custom_impl_Yolo clean && make -C nvdsinfer_custom_impl_Yolo

COPY worker/ /opt/deepstream-app/worker/
COPY bridge/ /opt/deepstream-app/bridge/
COPY models/ /opt/models/
COPY scripts/ /opt/scripts/

RUN mkdir -p /opt/nvidia/deepstream/deepstream/sources/project && \
    cp /opt/deepstream-app/worker/model_b3_gpu0_fp32.engine \
       /opt/nvidia/deepstream/deepstream/sources/project/model_b3_gpu0_fp32.engine

WORKDIR /opt/deepstream-app/bridge
RUN find /opt/nvidia/deepstream -name deepstream_perf.h \
        -exec cp {} /opt/nvidia/deepstream/deepstream/sources/includes/ \; \
        -quit
RUN find /opt/nvidia/deepstream -name deepstream_config.h \
        -exec cp {} /opt/nvidia/deepstream/deepstream/sources/includes/ \; \
        -quit
RUN make CUDA_VER=12.8

RUN mkdir -p /opt/deepstream-server && \
    cp /opt/deepstream-app/bridge/dsserver_config.yml \
       /opt/deepstream-app/bridge/dsserver_pgie_config.yml \
       /opt/deepstream-app/bridge/config_new_nvstreammux.txt \
       /opt/deepstream-app/bridge/config_preprocess.txt \
       /opt/deepstream-app/bridge/labels.txt \
       /opt/deepstream-server/ && \
    ( cp /opt/nvidia/deepstream/deepstream/sources/apps/sample_apps/deepstream-server/deepstream-server-app \
          /opt/deepstream-server/ 2>/dev/null || \
      cp /opt/nvidia/deepstream/deepstream-8.0/bin/deepstream-server-app \
          /opt/deepstream-server/ )

RUN chmod +x /opt/scripts/entrypoint-model.sh

ENV MODEL=yolo-v9

WORKDIR /opt/models
ENTRYPOINT ["/opt/nvidia/deepstream/deepstream-8.0/entrypoint.sh"]
CMD ["/opt/scripts/entrypoint-model.sh"]
