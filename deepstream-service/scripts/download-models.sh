#!/bin/bash
set -euo pipefail

MODELS_DIR="$(dirname "$0")/../models"
echo "DeepStream Model Downloader"
echo "=========================="
echo ""

download_yolo() {
    local MODEL_DIR="${MODELS_DIR}/yolo-v9"
    mkdir -p "${MODEL_DIR}"
    if [ -f "${MODEL_DIR}/model_b3_gpu0_fp32.engine" ]; then
        echo "✓ YOLOv9 engine already present"
        return
    fi
    echo "→ YOLOv9 engine must be converted from ONNX with trtexec"
    echo "  Run: trtexec --onnx=${MODEL_DIR}/yolov9-e-converted.pt.onnx \\"
    echo "         --saveEngine=${MODEL_DIR}/model_b3_gpu0_fp32.engine \\"
    echo "         --fp16"
}

download_peoplenet() {
    local MODEL_DIR="${MODELS_DIR}/peoplenet"
    mkdir -p "${MODEL_DIR}"
    if [ -f "${MODEL_DIR}/peoplenet.engine" ]; then
        echo "✓ PeopleNet engine already present"
        return
    fi
    echo "→ Downloading PeopleNet from NGC..."
    echo "  Requires NGC CLI + API key: https://ngc.nvidia.com/setup"
    echo "  ngc registry model download-version nvidia/tao/peoplenet:trainable_v2.6 --dest ${MODEL_DIR}"
    echo ""
    echo "  Then convert in the container with tao-converter (entrypoint does this automatically):"
    echo "  MODEL=peoplenet docker-compose up -d"
}

download_trafficcamnet_chain() {
    local MODEL_DIR="${MODELS_DIR}/trafficcamnet-lpd-lpr"
    mkdir -p "${MODEL_DIR}"
    echo "→ Downloading TrafficCamNet + LPDNet + LPRNet from NGC..."
    echo "  Requires NGC CLI + API key: https://ngc.nvidia.com/setup"
    echo ""
    echo "  ngc registry model download-version nvidia/tao/trafficcamnet:pruned_v1.0 --dest ${MODEL_DIR}"
    echo "  ngc registry model download-version nvidia/tao/lpdnet:trainable_v1.0 --dest ${MODEL_DIR}"
    echo "  ngc registry model download-version nvidia/tao/lprnet:trainable_v1.0 --dest ${MODEL_DIR}"
    echo ""
    echo "  Then convert in the container (entrypoint does this automatically):"
    echo "  MODEL=trafficcamnet-lpd-lpr docker-compose up -d"
}

case "${1:-}" in
    yolo|yolo-v9)
        download_yolo
        ;;
    peoplenet)
        download_peoplenet
        ;;
    trafficcam|trafficcamnet-lpd-lpr)
        download_trafficcamnet_chain
        ;;
    all)
        download_yolo
        download_peoplenet
        download_trafficcamnet_chain
        ;;
    *)
        echo "Usage: $0 {yolo-v9|peoplenet|trafficcamnet-lpd-lpr|all}"
        echo ""
        download_yolo
        echo ""
        download_peoplenet
        echo ""
        download_trafficcamnet_chain
        ;;
esac
