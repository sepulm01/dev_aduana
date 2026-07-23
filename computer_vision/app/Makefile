CUDA_VER?=
ifeq ($(CUDA_VER),)
  $(error "CUDA_VER is not set")
endif

APP:= pipeline-test3

TARGET_DEVICE = $(shell gcc -dumpmachine | cut -f1 -d -)

NVDS_VERSION:=8.0

LIB_INSTALL_DIR?=/opt/nvidia/deepstream/deepstream-$(NVDS_VERSION)/lib/

SRCS:= pipeline_test3.cpp snapshot_sender.cpp

OBJS:= $(SRCS:.cpp=.o)

PKGS:= gstreamer-1.0 cairo

NVDS_SOURCES?=/opt/nvidia/deepstream/deepstream/sources

CXXFLAGS+= -I$(NVDS_SOURCES)/includes \
	 -I/usr/local/cuda-$(CUDA_VER)/include

CXXFLAGS+= $(shell pkg-config --cflags $(PKGS))

LIBS:= $(shell pkg-config --libs $(PKGS))

LIBS+= -L/usr/local/cuda-$(CUDA_VER)/lib64/ -lcudart -lnvdsgst_helper -lm \
	-L$(LIB_INSTALL_DIR) -lnvdsgst_meta -lnvds_meta -lnvds_yml_parser \
	-lcuda -Wl,-rpath,$(LIB_INSTALL_DIR) \
	-lhiredis -lnvds_batch_jpegenc

all: $(APP)

%.o: %.cpp Makefile
	$(CXX) -c -o $@ $(CXXFLAGS) $<

$(APP): $(OBJS) Makefile
	$(CXX) -o $(APP) $(OBJS) $(LIBS)

clean:
	rm -rf $(OBJS) $(APP)
