#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$project_dir"

if [[ -x .venv/bin/python ]]; then
    export PATH="$project_dir/.venv/bin:$PATH"
fi

if [[ -f .env && "${LOAD_DOTENV:-true}" == "true" ]]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

runtime_dir="${WORKER_RUNTIME_DIR:-/tmp/dameokja-ai-worker}"
supervisor_pid_file="$runtime_dir/supervisor.pid"
mkdir -p "$runtime_dir"

if [[ -s "$supervisor_pid_file" ]]; then
    existing_pid="$(cat "$supervisor_pid_file")"
    if [[ "$existing_pid" =~ ^[0-9]+$ ]] && kill -0 "$existing_pid" 2>/dev/null; then
        echo "GPU Worker가 이미 실행 중입니다: PID $existing_pid" >&2
        exit 1
    fi
fi
printf '%s\n' "$$" > "$supervisor_pid_file"

export OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}"
export OLLAMA_MODELS="${OLLAMA_MODELS:-/app/.ollama/models}"
# Colab이 주입하는 Jupyter 전용 matplotlib backend는 격리된 Worker venv에서
# import할 수 없다. Worker는 그래프 UI가 필요 없으므로 headless backend를 쓴다.
export MPLBACKEND="${WORKER_MPLBACKEND:-Agg}"
if ! mkdir -p "$OLLAMA_MODELS" 2>/dev/null; then
    echo "Ollama 모델 경로를 쓸 수 없어 컨테이너 경로로 대체합니다: $OLLAMA_MODELS" >&2
    export OLLAMA_MODELS="$project_dir/.ollama/models"
    mkdir -p "$OLLAMA_MODELS"
fi

bundled_input_classifier="$project_dir/models/input-type-mobilenetv3-small.onnx"
runtime_input_classifier="${INPUT_CLASSIFIER_MODEL_PATH:-$bundled_input_classifier}"
bundled_product_detector="$project_dir/models/yolov8s-worldv2.pt"
runtime_product_detector="${WORKER_PRODUCT_DETECTOR_MODEL_PATH:-$bundled_product_detector}"

if [[ -f "$bundled_input_classifier" && ! -f "$runtime_input_classifier" ]]; then
    if ! mkdir -p "$(dirname "$runtime_input_classifier")" 2>/dev/null || ! cp "$bundled_input_classifier" "$runtime_input_classifier" 2>/dev/null; then
        echo "입력 분류 모델 경로를 쓸 수 없어 이미지 내 체크포인트를 사용합니다: $runtime_input_classifier" >&2
        runtime_input_classifier="$bundled_input_classifier"
    fi
fi

if [[ -f "$bundled_product_detector" && ! -f "$runtime_product_detector" ]]; then
    if ! mkdir -p "$(dirname "$runtime_product_detector")" 2>/dev/null || ! cp "$bundled_product_detector" "$runtime_product_detector" 2>/dev/null; then
        echo "상품 검출 모델 경로를 쓸 수 없어 이미지 내 체크포인트를 사용합니다: $runtime_product_detector" >&2
        runtime_product_detector="$bundled_product_detector"
    fi
fi

export INPUT_CLASSIFIER_MODEL_PATH="$runtime_input_classifier"
export WORKER_PRODUCT_DETECTOR_MODEL_PATH="$runtime_product_detector"

if [[ "${INPUT_CLASSIFIER_BACKEND:-auto}" == "onnx" && ! -f "$runtime_input_classifier" ]]; then
    echo "MobileNet 입력 분류 체크포인트가 없습니다: $runtime_input_classifier" >&2
    exit 1
fi

if [[ "${WORKER_PRODUCT_DETECTOR_BACKEND:-auto}" == "yolo_world" && ! -f "$runtime_product_detector" ]]; then
    echo "YOLO-World 상품 탐지 체크포인트가 없습니다: $runtime_product_detector" >&2
    exit 1
fi

if [[ "${REQUIRE_GPU:-true}" == "true" ]]; then
    command -v nvidia-smi >/dev/null 2>&1 || { echo "nvidia-smi를 찾지 못했습니다." >&2; exit 1; }
    nvidia-smi >/dev/null || { echo "NVIDIA GPU를 사용할 수 없습니다." >&2; exit 1; }
fi

ollama_pid=""
worker_pid=""
restart_requested=false

stop_children() {
    if [[ -n "$worker_pid" ]]; then
        kill "$worker_pid" 2>/dev/null || true
    fi
    if [[ -n "$ollama_pid" ]]; then
        kill "$ollama_pid" 2>/dev/null || true
    fi
    [[ -n "$worker_pid" ]] && wait "$worker_pid" 2>/dev/null || true
    [[ -n "$ollama_pid" ]] && wait "$ollama_pid" 2>/dev/null || true
    worker_pid=""
    ollama_pid=""
}

cleanup() {
    stop_children
    if [[ -f "$supervisor_pid_file" ]] && [[ "$(cat "$supervisor_pid_file")" == "$$" ]]; then
        rm -f "$supervisor_pid_file"
    fi
}

request_restart() {
    restart_requested=true
    stop_children
}

request_shutdown() {
    restart_requested=false
    cleanup
    exit 0
}

trap cleanup EXIT
trap request_shutdown INT TERM
trap request_restart HUP

start_children() {
    ollama serve &
    ollama_pid=$!

    for _ in $(seq 1 120); do
        if curl -fsS http://127.0.0.1:11434/api/tags >/dev/null; then
            break
        fi
        if ! kill -0 "$ollama_pid" 2>/dev/null; then
            echo "Ollama가 준비되기 전에 종료됐습니다." >&2
            return 1
        fi
        sleep 1
    done
    curl -fsS http://127.0.0.1:11434/api/tags >/dev/null || { echo "Ollama 준비 시간 초과" >&2; return 1; }

    model_name="${OLLAMA_MODEL:-gemma4:e2b}"
    if ! ollama show "$model_name" >/dev/null 2>&1; then
        if [[ "${PULL_MODEL_ON_START:-true}" != "true" ]]; then
            echo "Ollama 모델이 없습니다: $model_name" >&2
            return 1
        fi
        ollama pull "$model_name"
    fi

    if [[ "${WARM_MODEL_ON_START:-true}" == "true" ]]; then
        python - <<'PY'
import json
import os
import urllib.request

payload = json.dumps(
    {
        "model": os.getenv("OLLAMA_MODEL", "gemma4:e2b"),
        "prompt": "",
        "stream": False,
        "keep_alive": os.getenv("OLLAMA_KEEP_ALIVE", "30m"),
    }
).encode("utf-8")
request = urllib.request.Request(
    "http://127.0.0.1:11434/api/generate",
    data=payload,
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(request, timeout=600) as response:
    if response.status != 200:
        raise SystemExit(f"Ollama 모델 워밍업 실패: HTTP {response.status}")
print("Ollama 모델 GPU 워밍업 완료", flush=True)
PY
    fi

    python - <<'PY'
import os
import paddle

if os.getenv("REQUIRE_GPU", "true").lower() == "true" and paddle.device.cuda.device_count() < 1:
    raise SystemExit("PaddlePaddle가 NVIDIA GPU를 찾지 못했습니다.")
print(f"Paddle GPU count: {paddle.device.cuda.device_count()}", flush=True)
PY

    python -m uvicorn gpu_worker.main:app \
        --host 0.0.0.0 \
        --port "${WORKER_PORT:-9000}" \
        --workers 1 &
    worker_pid=$!
}

while true; do
    restart_requested=false
    start_children
    set +e
    wait -n "$ollama_pid" "$worker_pid"
    child_status=$?
    set -e
    stop_children
    if [[ "$restart_requested" == "true" ]]; then
        echo "GPU Worker를 재시작합니다." >&2
        continue
    fi
    echo "GPU Worker 하위 프로세스가 종료됐습니다: status=$child_status" >&2
    exit "$child_status"
done
