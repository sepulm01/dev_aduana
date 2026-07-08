FROM nvcr.io/nvidia/deepstream:8.0-gc-triton-devel

WORKDIR /opt/nvidia/deepstream/deepstream/
RUN bash user_additional_install.sh

RUN apt-get update && apt-get install -y \
    libhiredis-dev \
    && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/marcoslucianops/DeepStream-Yolo.git \
    /opt/nvidia/deepstream/deepstream/DeepStream-Yolo
WORKDIR /opt/nvidia/deepstream/deepstream/DeepStream-Yolo
ENV CUDA_VER=12.8
RUN make -C nvdsinfer_custom_impl_Yolo clean && make -C nvdsinfer_custom_impl_Yolo

COPY . /opt/computer_vision/

WORKDIR /opt/computer_vision/app
ENV CUDA_VER=12.8
RUN make

WORKDIR /opt/computer_vision/config
RUN mkdir -p /opt/computer_vision/record
ENV DISPLAY=${DISPLAY}
ENV RECORD_OUTPUT=1
ENTRYPOINT ["/opt/nvidia/deepstream/deepstream-8.0/entrypoint.sh"]
CMD ["../app/pipeline-test3", "./config_aduana.yml"]
