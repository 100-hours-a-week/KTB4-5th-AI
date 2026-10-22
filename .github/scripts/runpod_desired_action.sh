#!/usr/bin/env bash
# 현재 한국 시간 기준으로 Pod가 있어야 할 상태를 출력한다. 08:30~17:59는 start, 그 밖에는 stop.
set -euo pipefail
now=$((10#$(TZ=Asia/Seoul date +%H%M)))
if (( now >= 830 && now < 1800 )); then
  echo start
else
  echo stop
fi
