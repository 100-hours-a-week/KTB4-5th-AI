FROM python:3.11-slim

ARG OLLAMA_VERSION=0.34.3
# GitHub 릴리스 자산(ollama-linux-amd64.tar.zst)의 sha256. 버전을 바꾸면 함께 바꾼다.
ARG OLLAMA_SHA256=e83a089fd0cd2f79ee2933cca2085846a2065f497adbc6467c402177c68423f9
ARG PADDLE_CUDA_VARIANT=cu126
ARG TORCH_CUDA_VARIANT=cu126

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OLLAMA_HOST=127.0.0.1:11434 \
    OLLAMA_MODELS=/app/.ollama/models

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git libgl1 libglib2.0-0 libgomp1 zstd \
    && rm -rf /var/lib/apt/lists/* \
    # ollama.com/install.sh는 항상 최신 스크립트라 .tar.zst 확인이 실패하면 없는 .tgz로 넘어가 빌드가 깨졌다(2026-11-08).
    # 고정 버전의 GitHub 릴리스 자산을 직접 받아 체크섬을 확인한다. 압축 안의 bin/ollama, lib/ollama가 /usr/local 아래에 풀린다.
    # GitHub 릴리스 CDN이 큰 자산에 504를 간헐적으로 낸다(2026-11-08 실측). 모든 오류를 재시도한다.
    && curl -fsSL --retry 8 --retry-all-errors --retry-delay 15 --connect-timeout 30 -o /tmp/ollama.tar.zst "https://github.com/ollama/ollama/releases/download/v${OLLAMA_VERSION}/ollama-linux-amd64.tar.zst" \
    && echo "${OLLAMA_SHA256}  /tmp/ollama.tar.zst" | sha256sum -c - \
    && zstd -d -c /tmp/ollama.tar.zst | tar -x -C /usr/local \
    && rm -f /tmp/ollama.tar.zst \
    && test -x /usr/local/bin/ollama

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
COPY gpu_worker ./gpu_worker
COPY models ./models
COPY start.sh ./start.sh
COPY stop.sh restart.sh ./

RUN python -m pip install --upgrade pip \
    && python -m pip install 'torch==2.6.0' 'torchvision==0.21.0' --index-url "https://download.pytorch.org/whl/${TORCH_CUDA_VARIANT}" \
    && python -m pip install '.[yolo]' 'paddleocr>=3,<4' 'onnxruntime>=1.20,<2' \
    && (python -m pip uninstall -y paddlepaddle paddlepaddle-gpu >/dev/null 2>&1 || true)
RUN python -m pip install 'paddlepaddle-gpu==3.3.0' \
    --index-url "https://www.paddlepaddle.org.cn/packages/stable/${PADDLE_CUDA_VARIANT}/"

# 이미지로 실행하면 리셋마다 컨테이너 디스크가 지워지므로, 볼륨에 있는 기존 .env를 연결한다.
RUN chmod +x start.sh stop.sh restart.sh \
    && ln -s /workspace/KTB4-5th-AI/runpod/.env /app/.env

EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=10s --start-period=10m --retries=3 \
    CMD curl -fsS http://127.0.0.1:9000/health/ready | grep -q '"ready":true' || exit 1

CMD ["./start.sh"]
