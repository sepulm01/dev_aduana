CUDA_VER?=
ifeq ($(CUDA_VER),)
  $(error "CUDA_VER is not set")
endif

APP:= deepstream-security-app
CC:= g++

NVDS_VERSION:=8.0
DS_ROOT:= /opt/nvidia/deepstream/deepstream
LIB_INSTALL_DIR:= $(DS_ROOT)/lib
APP_INSTALL_DIR:= $(DS_ROOT)/bin

INCLUDES:= -I$(DS_ROOT)/sources/includes \
           -I$(DS_ROOT)/sources/apps-common/includes \
           -I/usr/local/cuda/include

PKGS:= gstreamer-1.0

SRCS:= deepstream_app.cpp analytics_probe.cpp redis_publisher.cpp stream_manager.cpp
OBJS:= $(SRCS:.cpp=.o)

CFLAGS+= $(INCLUDES) $(shell pkg-config --cflags $(PKGS))

LIBS:= $(shell pkg-config --libs $(PKGS))
LIBS+= -L$(LIB_INSTALL_DIR) -lnvdsgst_meta -lnvds_meta -lnvdsgst_helper
LIBS+= -lnvds_yml_parser -lgstnvcustomhelper -lnvdsgst_customhelper
LIBS+= -L/usr/local/cuda/lib64/ -lcudart -lcuda
LIBS+= -lhiredis -ljsoncpp
LIBS+= -Wl,-rpath,$(LIB_INSTALL_DIR)

all: $(APP)

%.o: %.cpp $(SRCS)
	$(CC) -c -o $@ $(CFLAGS) $<

$(APP): $(OBJS)
	$(CC) -o $(APP) $(OBJS) $(LIBS)

clean:
	rm -rf $(OBJS) $(APP)

.PHONY: all clean