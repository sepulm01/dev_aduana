#!/bin/bash
set -euo pipefail

MODEL="${MODEL:-yolo-v9}"
MODEL_DIR="/opt/models"
ACTIVE_DIR="${MODEL_DIR}/active"
MODEL_SRC="${MODEL_DIR}/${MODEL}"
BINARY="/opt/deepstream-app/bridge/deepstream-server-app"

echo "========================================="
echo " DeepStream Model Runner"
echo " Model: ${MODEL}"
echo "========================================="

if [ ! -d "${MODEL_SRC}" ]; then
    echo "ERROR: Model '${MODEL}' not found at ${MODEL_SRC}"
    echo "Available models:"
    ls -1 "${MODEL_DIR}" 2>/dev/null | grep -v active || echo "  (none)"
    exit 1
fi

rm -f "${ACTIVE_DIR}"
ln -sfn "${MODEL_SRC}" "${ACTIVE_DIR}"

if [ -x /usr/local/bin/tao-converter ]; then
    for model_file in "${ACTIVE_DIR}"/*.etlt "${ACTIVE_DIR}"/*.tlt; do
        [ -f "${model_file}" ] || continue
        engine_file="${model_file%.*}.engine"
        if [ -f "${engine_file}" ]; then
            echo "Engine: $(basename "${engine_file}") (exists)"
            continue
        fi
        echo "Converting $(basename "${model_file}") → $(basename "${engine_file}")..."
        KEY="nvidia_tlt"
        DIMS="3,544,960"
        OUTPUT="output_bbox/BiasAdd"
        tao-converter "${model_file}" \
            -k "${KEY}" \
            -d "${DIMS}" \
            -o "${OUTPUT}" \
            -e "${engine_file}" && \
            echo "   ✓ $(ls -lh "${engine_file}" | awk '{print $5}')" || \
            echo "   ✗ Conversion failed, nvinfer will try runtime build"
    done
fi

cd "${ACTIVE_DIR}/config"
exec "${BINARY}" dsserver_config.yml
