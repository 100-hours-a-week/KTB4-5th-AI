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
health_url="http://127.0.0.1:${WORKER_PORT:-9000}/health/ready"

if [[ -s "$pid_file" ]]; then
    pid="$(cat "$pid_file")"
else
    pid=""
fi

if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    kill -HUP "$pid"
    # 종료 직전의 기존 health 응답을 새 프로세스의 준비 완료로 오인하지 않게 잠시 기다린다.
    sleep 2
else
    rm -f "$pid_file"
    log_file="${WORKER_LOG_FILE:-$runtime_dir/worker.log}"
    mkdir -p "$runtime_dir"
    nohup "$project_dir/start.sh" >> "$log_file" 2>&1 &
fi

for _ in $(seq 1 600); do
    if curl -fsS "$health_url" 2>/dev/null | grep -q '"ready":true'; then
        echo "GPU Worker 재시작을 완료했습니다: $health_url"
        exit 0
    fi
    sleep 1
done

echo "GPU Worker가 600초 안에 준비되지 않았습니다: $health_url" >&2
exit 1
