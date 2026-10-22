#!/usr/bin/env bash
# EC2 crontab에서 RunPod 시작·정지 워크플로(runpod-schedule.yml)를 호출한다. 사용법: runpod-schedule-dispatch start|stop
# GitHub schedule은 몇 시간씩 늦거나 빠져서, 시각은 이 crontab이 정하고 워크플로는 받은 작업만 실행한다.
#
# 설치(한 대의 EC2에만 설치한다):
#   1. 이 레포 Actions: Read and write 권한만 가진 fine-grained PAT를 토큰 파일에 저장한다.
#        sudo install -m 600 /dev/null /root/github_runpod_dispatch_token
#        sudo vi /root/github_runpod_dispatch_token
#   2. 스크립트를 복사한다.
#        sudo install -m 755 runpod-schedule-dispatch.sh /usr/local/bin/runpod-schedule-dispatch
#   3. root crontab(sudo crontab -e)에 아래를 등록한다. 08:30 시작, 18:00 정지이고, 그 사이에도 30분마다
#      같은 작업을 다시 보내 낮에 꺼진 Pod는 켜고 밤에 켜진 Pod는 끈다. 이미 그 상태면 몇 초 만에 끝난다.
#        CRON_TZ=Asia/Seoul
#        30 8 * * *           /usr/local/bin/runpod-schedule-dispatch start
#        0,30 9-17 * * *      /usr/local/bin/runpod-schedule-dispatch start
#        0 8 * * *            /usr/local/bin/runpod-schedule-dispatch stop
#        0,30 0-7,18-23 * * * /usr/local/bin/runpod-schedule-dispatch stop
#   결과는 syslog(journalctl -t runpod-schedule)에 남는다.
set -euo pipefail

REPO="${GITHUB_REPOSITORY:-100-hours-a-week/KTB4-5th-AI}"
WORKFLOW="${RUNPOD_SCHEDULE_WORKFLOW:-runpod-schedule.yml}"
REF="${RUNPOD_SCHEDULE_REF:-main}"
TOKEN_FILE="${GITHUB_DISPATCH_TOKEN_FILE:-/root/github_runpod_dispatch_token}"
ACTION="${1:-}"

if [[ "${ACTION}" != "start" && "${ACTION}" != "stop" ]]; then
  echo "사용법: $0 start|stop" >&2
  exit 2
fi

log() {
  logger -t runpod-schedule -- "$*" 2>/dev/null || true
  echo "$*"
}

if [[ ! -r "${TOKEN_FILE}" ]]; then
  log "토큰 파일을 읽을 수 없습니다: ${TOKEN_FILE}"
  exit 1
fi
token="$(tr -d '[:space:]' < "${TOKEN_FILE}")"

# 일시적인 네트워크·GitHub 오류에 대비해 몇 번 재시도한다. 성공하면 204를 받는다.
for attempt in 1 2 3; do
  status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 -X POST \
    -H "Accept: application/vnd.github+json" \
    -H "Authorization: Bearer ${token}" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches" \
    -d "{\"ref\":\"${REF}\",\"inputs\":{\"action\":\"${ACTION}\"}}" || echo 000)"
  if [[ "${status}" == "204" ]]; then
    log "dispatch 성공 workflow=${WORKFLOW} action=${ACTION}"
    exit 0
  fi
  log "dispatch 실패 status=${status} attempt=${attempt}/3"
  sleep 20
done
exit 1
