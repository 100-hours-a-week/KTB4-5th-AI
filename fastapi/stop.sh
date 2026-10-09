#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${PROJECT_DIR}/var/run/receipt-ai.pid"

# PID 파일이 없어도 같은 프로젝트 디렉터리에서 실행 중인 uvicorn을 찾아 종료한다.
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

if [[ ! -f "${PID_FILE}" ]]; then
  if SERVER_PID="$(find_running_server_pid)"; then
    mkdir -p "$(dirname "${PID_FILE}")"
    echo "${SERVER_PID}" >"${PID_FILE}"
    echo "PID 파일을 실행 중인 서버에서 복구했습니다. PID=${SERVER_PID}"
  else
    echo "실행 중인 서버 PID 파일이 없습니다."
    exit 0
  fi
fi

SERVER_PID="${SERVER_PID:-$(<"${PID_FILE}")}"
if [[ ! "${SERVER_PID}" =~ ^[0-9]+$ ]]; then
  echo "잘못된 PID 파일을 제거합니다: ${PID_FILE}"
  rm -f "${PID_FILE}"
  exit 1
fi

if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
  echo "PID 파일의 서버가 이미 종료되어 있어 실행 중인 서버를 다시 찾습니다."
  rm -f "${PID_FILE}"
  if SERVER_PID="$(find_running_server_pid)"; then
    echo "${SERVER_PID}" >"${PID_FILE}"
    echo "실행 중인 서버 PID를 복구했습니다. PID=${SERVER_PID}"
  else
    echo "실행 중인 서버가 없습니다."
    exit 0
  fi
fi

COMMAND="$(ps -p "${SERVER_PID}" -o command= 2>/dev/null || true)"
if [[ "${COMMAND}" != *"uvicorn app.main:app"* ]]; then
  echo "PID ${SERVER_PID}가 이 프로젝트의 서버인지 확인할 수 없어 종료하지 않습니다."
  echo "실행 명령: ${COMMAND}"
  exit 1
fi

kill -TERM "${SERVER_PID}"
for _ in {1..30}; do
  if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
    rm -f "${PID_FILE}"
    echo "서버가 종료되었습니다."
    exit 0
  fi
  sleep 1
done

echo "정상 종료 시간이 초과되어 PID ${SERVER_PID}를 강제 종료합니다."
kill -KILL "${SERVER_PID}"
rm -f "${PID_FILE}"
