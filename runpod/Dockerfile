FROM python:3.11-slim

ARG OLLAMA_VERSION=0.34.3
ARG PADDLE_CUDA_VARIANT=cu126
ARG TORCH_CUDA_VARIANT=cu126

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OLLAMA_HOST=127.0.0.1:11434 \
    OLLAMA_MODELS=/workspace/ollama

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git libgl1 libglib2.0-0 zstd \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://ollama.com/install.sh | OLLAMA_VERSION="${OLLAMA_VERSION}" sh

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
COPY gpu_worker ./gpu_worker
COPY models ./models
COPY start.sh ./start.sh

RUN python -m pip install --upgrade pip \
    && python -m pip install 'torch==2.13.0' 'torchvision==0.28.0' --index-url "https://download.pytorch.org/whl/${TORCH_CUDA_VARIANT}" \
    && python -m pip install '.[yolo]' 'paddleocr>=3,<4' 'onnxruntime>=1.20,<2' \
    && (python -m pip uninstall -y paddlepaddle paddlepaddle-gpu >/dev/null 2>&1 || true)
RUN python -m pip install 'paddlepaddle-gpu==3.3.0' \
    --index-url "https://www.paddlepaddle.org.cn/packages/stable/${PADDLE_CUDA_VARIANT}/"

RUN chmod +x start.sh

EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=10s --start-period=10m --retries=3 \
    CMD curl -fsS http://127.0.0.1:9000/health/ready || exit 1

CMD ["./start.sh"]
