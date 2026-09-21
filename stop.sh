#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${PROJECT_DIR}/var/run/receipt-ai.pid"

if [[ ! -f "${PID_FILE}" ]]; then
  echo "실행 중인 서버 PID 파일이 없습니다."
  exit 0
fi

SERVER_PID="$(<"${PID_FILE}")"
if [[ ! "${SERVER_PID}" =~ ^[0-9]+$ ]]; then
  echo "잘못된 PID 파일을 제거합니다: ${PID_FILE}"
  rm -f "${PID_FILE}"
  exit 1
fi

if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
  echo "서버가 이미 종료되어 있습니다."
  rm -f "${PID_FILE}"
  exit 0
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
