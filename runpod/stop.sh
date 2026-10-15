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

kill -TERM "$pid"
for _ in $(seq 1 30); do
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "GPU Worker를 종료했습니다."
        exit 0
    fi
    sleep 1
done

echo "GPU Worker가 30초 안에 종료되지 않았습니다: PID $pid" >&2
exit 1
