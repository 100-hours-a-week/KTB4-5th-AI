#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$project_dir"

python3 - <<'PY'
import sys

if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11 이상이 필요합니다.")
PY

paddle_cuda_variant="${PADDLE_CUDA_VARIANT:-cu126}"
paddle_index_url="https://www.paddlepaddle.org.cn/packages/stable/${paddle_cuda_variant}/"
torch_cuda_variant="${TORCH_CUDA_VARIANT:-cu126}"
torch_index_url="https://download.pytorch.org/whl/${torch_cuda_variant}"

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install 'torch==2.6.0' 'torchvision==0.21.0' --index-url "$torch_index_url"
.venv/bin/python -m pip install '.[yolo]' 'paddleocr>=3,<4' 'onnxruntime>=1.20,<2'
.venv/bin/python -m pip uninstall -y paddlepaddle paddlepaddle-gpu >/dev/null 2>&1 || true
.venv/bin/python -m pip install 'paddlepaddle-gpu==3.3.0' --index-url "$paddle_index_url"

if ! command -v ollama >/dev/null 2>&1; then
    curl -fsSL https://ollama.com/install.sh | sh
fi

if [[ ! -f .env ]]; then
    cp .env.example .env
    echo ".env를 생성했습니다. WORKER_API_KEY와 WORKER_ALLOWED_IMAGE_HOSTS를 채우세요."
fi

echo "설치 완료: .env를 확인한 뒤 ./start.sh를 실행하세요."
