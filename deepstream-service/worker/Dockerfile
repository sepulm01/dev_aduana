FROM nvcr.io/nvidia/deepstream:8.0-triton-multiarch

# Install prerequisites
WORKDIR /opt/nvidia/deepstream/deepstream/
RUN bash user_additional_install.sh

# Download ReID model
ENV REID_DIR="/opt/nvidia/deepstream/deepstream/samples/models/Tracker/"
RUN mkdir -p $REID_DIR
RUN wget 'https://api.ngc.nvidia.com/v2/models/nvidia/tao/reidentificationnet/versions/deployable_v1.0/files/resnet50_market1501.etlt' -P $REID_DIR

# Clone both repositories into one folder
RUN git clone https://github.com/NVIDIA-AI-IOT/deepstream_tools.git
RUN git clone https://github.com/NVIDIA-AI-IOT/deepstream_reference_apps.git
RUN git clone https://github.com/marcoslucianops/DeepStream-Yolo.git
WORKDIR /opt/nvidia/deepstream/deepstream/DeepStream-Yolo
ENV CUDA_VER=12.8
RUN make -C nvdsinfer_custom_impl_Yolo clean && make -C nvdsinfer_custom_impl_Yolo


