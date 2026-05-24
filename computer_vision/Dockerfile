FROM nvcr.io/nvidia/deepstream:8.0-gc-triton-devel

WORKDIR /opt/nvidia/deepstream/deepstream/
RUN bash user_additional_install.sh

RUN apt-get update && apt-get install -y \
    libjsoncpp-dev \
    libhiredis-dev \
    libcurl4-gnutls-dev \
    && rm -rf /var/lib/apt/lists/*

COPY . /opt/computer_vision/

WORKDIR /opt/computer_vision/app
ENV CUDA_VER=12.8
RUN make

WORKDIR /opt/computer_vision/config
ENTRYPOINT ["/opt/nvidia/deepstream/deepstream-8.0/entrypoint.sh"]
CMD ["../app/deepstream-server-app", "./dsserver_config.yml"]
