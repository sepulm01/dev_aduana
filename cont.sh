xhost + 
docker run --rm -it   --gpus '"device=1"'   --runtime=nvidia   \
-e NVIDIA_VISIBLE_DEVICES=all   -e NVIDIA_DRIVER_CAPABILITIES=all \
-v ./deepstream-service/worker:/opt/nvidia/deepstream/deepstream/sources/project \
-v /tmp/.X11-unix:/tmp/.X11-unix  \
-e DISPLAY=$DISPLAY \
mediamtx-manager-deepstream-yolo  \
bash 
#deepstream-app -c /opt/nvidia/deepstream/config/deepstream_app_config.txt
