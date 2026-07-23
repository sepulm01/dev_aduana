CUDA_VER?=
ifeq ($(CUDA_VER),)
  $(error "CUDA_VER is not set")
endif

APP:= pipeline-test3
APP2:= aduana-test

TARGET_DEVICE = $(shell gcc -dumpmachine | cut -f1 -d -)

NVDS_VERSION:=8.0

LIB_INSTALL_DIR?=/opt/nvidia/deepstream/deepstream-$(NVDS_VERSION)/lib/

SRCS:= pipeline_test3.cpp snapshot_sender.cpp
SRCS2:= aduana_test.cpp

OBJS:= $(SRCS:.cpp=.o)
OBJS2:= $(SRCS2:.cpp=.o)

PKGS:= gstreamer-1.0

NVDS_SOURCES?=/opt/nvidia/deepstream/deepstream/sources

CXXFLAGS+= -I$(NVDS_SOURCES)/includes \
	 -I/usr/local/cuda-$(CUDA_VER)/include

CXXFLAGS+= $(shell pkg-config --cflags $(PKGS))

LIBS:= $(shell pkg-config --libs $(PKGS))

LIBS+= -L/usr/local/cuda-$(CUDA_VER)/lib64/ -lcudart -lnvdsgst_helper -lm \
	-L$(LIB_INSTALL_DIR) -lnvdsgst_meta -lnvds_meta -lnvds_yml_parser \
	-lcuda -Wl,-rpath,$(LIB_INSTALL_DIR) \
	-lhiredis -lnvds_batch_jpegenc

LIBS2:= $(shell pkg-config --libs $(PKGS))

LIBS2+= -L/usr/local/cuda-$(CUDA_VER)/lib64/ -lcudart -lnvdsgst_helper -lm \
	-L$(LIB_INSTALL_DIR) -lnvdsgst_meta -lnvds_meta -lnvds_yml_parser \
	-lcuda -Wl,-rpath,$(LIB_INSTALL_DIR)

all: $(APP) $(APP2)

%.o: %.cpp Makefile
	$(CXX) -c -o $@ $(CXXFLAGS) $<

$(APP): $(OBJS) Makefile
	$(CXX) -o $(APP) $(OBJS) $(LIBS)

$(APP2): $(OBJS2) Makefile
	$(CXX) -o $(APP2) $(OBJS2) $(LIBS2)

clean:
	rm -rf $(OBJS) $(OBJS2) $(APP) $(APP2)
