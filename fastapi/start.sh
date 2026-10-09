#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_DIR="${PROJECT_DIR}/var/run"
LOG_DIR="${PROJECT_DIR}/logs"
PID_FILE="${PID_DIR}/receipt-ai.pid"
LOG_FILE="${LOG_DIR}/server.out.log"
PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

# PID 파일이 배포 과정에서 사라져도 같은 프로젝트의 기존 uvicorn 프로세스를 찾는다.
find_running_server_pid() {
  local pid command cwd
  while read -r pid command; do
    [[ -z "${pid}" || "${pid}" == "$$" ]] && continue
    [[ "${command}" != *"uvicorn app.main:app"* ]] && continue
    cwd="$(readlink -f "/proc/${pid}/cwd" 2>/dev/null || true)"
    if [[ "${cwd}" == "${PROJECT_DIR}" ]]; then
      echo "${pid}"
      return 0
    fi
  done < <(ps -eo pid=,command=)
  return 1
}

mkdir -p "${PID_DIR}" "${LOG_DIR}" "${PROJECT_DIR}/var/uploads"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "오류: ${PYTHON_BIN}이 없습니다. 먼저 'uv sync --extra ocr'을 실행하세요."
  exit 1
fi

if [[ -f "${PID_FILE}" ]]; then
  EXISTING_PID="$(<"${PID_FILE}")"
  if [[ "${EXISTING_PID}" =~ ^[0-9]+$ ]] && kill -0 "${EXISTING_PID}" 2>/dev/null; then
    echo "이미 실행 중입니다. PID=${EXISTING_PID}"
    exit 0
  fi
  rm -f "${PID_FILE}"
fi

if DISCOVERED_PID="$(find_running_server_pid)"; then
  echo "${DISCOVERED_PID}" >"${PID_FILE}"
  echo "이미 실행 중입니다. PID=${DISCOVERED_PID} (프로세스에서 복구)"
  exit 0
fi

cd "${PROJECT_DIR}"
nohup "${PYTHON_BIN}" -m uvicorn app.main:app \
  --host "${HOST}" \
  --port "${PORT}" \
  --workers 1 \
  >>"${LOG_FILE}" 2>&1 &
SERVER_PID=$!
echo "${SERVER_PID}" >"${PID_FILE}"

for _ in {1..30}; do
  if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
    rm -f "${PID_FILE}"
    echo "서버 시작에 실패했습니다. 로그: ${LOG_FILE}"
    tail -n 50 "${LOG_FILE}" || true
    exit 1
  fi
  if curl --silent --fail --max-time 1 "http://127.0.0.1:${PORT}/health/live" >/dev/null; then
    echo "서버가 시작되었습니다. PID=${SERVER_PID}"
    echo "API: http://${HOST}:${PORT}"
    echo "API 로그(UTC 날짜별): ${LOG_DIR}/yyyy-mm-dd.log"
    echo "표준 출력: ${LOG_FILE}"
    exit 0
  fi
  sleep 1
done

echo "프로세스는 실행 중이지만 30초 안에 health check가 성공하지 않았습니다."
echo "로그를 확인하세요: ${LOG_FILE}"
exit 1
