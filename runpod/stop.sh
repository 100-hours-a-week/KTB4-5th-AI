#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$project_dir"

if [[ -f .env && "${LOAD_DOTENV:-true}" == "true" ]]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

runtime_dir="${WORKER_RUNTIME_DIR:-/tmp/dameokja-ai-worker}"
pid_file="$runtime_dir/supervisor.pid"

if [[ ! -s "$pid_file" ]]; then
    echo "GPU Worker가 실행 중이 아닙니다."
    exit 0
fi

pid="$(cat "$pid_file")"
if [[ ! "$pid" =~ ^[0-9]+$ ]] || ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$pid_file"
    echo "종료된 GPU Worker의 PID 파일을 정리했습니다."
    exit 0
fi

# supervisor가 컨테이너 진입점의 유일한 하위 프로세스인 환경에서는 supervisor 종료가
# 컨테이너 종료로 이어진다. supervisor는 유지하고 Ollama/Uvicorn만 중지한다.
kill -USR1 "$pid"
for _ in $(seq 1 30); do
    if ! curl -fsS "http://127.0.0.1:${WORKER_PORT:-9000}/health/ready" >/dev/null 2>&1; then
        echo "GPU Worker 하위 프로세스를 중지했습니다."
        exit 0
    fi
    sleep 1
done

echo "GPU Worker가 30초 안에 종료되지 않았습니다: PID $pid" >&2
exit 1
