#!/bin/bash
set -euo pipefail

CONTAINER="mediamtx-manager-deepstream-yolo-1"
VIDEOS_DIR="/opt/videos"
OUTPUT_DIR="/opt/output"
API="http://localhost:9000/api/v1"

echo "========================================="
echo " con1.sh — DeepStream YOLOv9 en paralelo"
echo "========================================="

echo ""
echo "==> Verificando contenedor..."
docker ps --format '{{.Names}}' | grep -q "$CONTAINER" || {
    echo "ERROR: $CONTAINER no está corriendo"
    exit 1
}

echo "==> Esperando pipeline ready..."
for i in $(seq 1 15); do
    STATE=$(docker exec "$CONTAINER" curl -s "$API/health/get-dsready-state" 2>/dev/null \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['health-info']['ds-ready'])" 2>/dev/null || echo "WAIT")
    [ "$STATE" = "YES" ] && break
    sleep 2
done
[ "$STATE" = "YES" ] || { echo "ERROR: pipeline no ready"; exit 1; }
echo "   Pipeline: READY"

echo ""
echo "==> Videos en $VIDEOS_DIR:"
VIDEOS=$(docker exec "$CONTAINER" sh -c "ls $VIDEOS_DIR/*.mp4 2>/dev/null" | grep -v out_inferencia | grep -v inferencia_paralelo || true)
echo "$VIDEOS" | while read v; do [ -n "$v" ] && echo "   $(basename "$v")"; done

echo ""
echo "==> Agregando streams al pipeline..."
for vid in $VIDEOS; do
    NAME=$(basename "$vid" .mp4)
    RESP=$(docker exec "$CONTAINER" curl -s -XPOST "$API/stream/add" \
        -d "{\"key\":\"sensor\",\"value\":{\"camera_id\":\"$NAME\",\"camera_name\":\"$NAME\",\"camera_url\":\"file://$vid\",\"change\":\"camera_add\",\"metadata\":{\"resolution\":\"1920x1080\",\"codec\":\"h264\",\"framerate\":30}},\"headers\":{\"source\":\"vst\",\"created_at\":\"2024-01-01T00:00:00.000Z\"}}")
    STATUS=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])" 2>/dev/null || echo "ERROR")
    echo "   $NAME → $STATUS"
done

echo ""
echo "==> Monitoreando streams (Ctrl+C para detener)..."
PREV_COUNT=-1
while true; do
    INFO=$(docker exec "$CONTAINER" curl -s "$API/stream/get-stream-info" 2>/dev/null)
    COUNT=$(echo "$INFO" | python3 -c "import sys,json; print(json.load(sys.stdin)['stream-info']['stream-count'])" 2>/dev/null || echo "0")

    if [ "$COUNT" != "$PREV_COUNT" ]; then
        GPU=$(docker exec "$CONTAINER" nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader 2>/dev/null || echo "N/A")
        echo "   Streams activos: $COUNT  | GPU: $GPU"
        PREV_COUNT=$COUNT
    fi

    [ "${COUNT:-0}" -eq 0 ] && break
    sleep 3
done

echo ""
echo "==> Procesamiento completado."
echo "   Modo PERF_MODE: inferencia ejecutada, sin video de salida."
echo "   Revisa métricas en los logs del contenedor."
echo ""
echo "==> Listo."
