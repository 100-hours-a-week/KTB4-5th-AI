#!/usr/bin/env bash
# 스팟 EC2가 재시작·재할당될 때마다 로컬 코드를 새 인스턴스로 옮기고 서버를 띄우는 스크립트.
# 인스턴스별 OS·Python 준비는 ec2-user-data.sh(User Data)가 이미 끝냈다고 가정한다.
#
# 사용법: ./deploy/deploy.sh <EC2_PUBLIC_IP> [키파일 경로] [원격 사용자]
set -Eeuo pipefail

EC2_IP="${1:?사용법: ./deploy/deploy.sh <EC2_PUBLIC_IP> [키파일경로] [원격사용자]}"
KEY_PATH="${2:-$HOME/.ssh/id_rsa}"
REMOTE_USER="${3:-ec2-user}"
REMOTE_DIR="/home/${REMOTE_USER}/dameokja/fastapi"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH_OPTS=(-i "${KEY_PATH}" -o StrictHostKeyChecking=accept-new)

echo "==> 코드 동기화 (rsync) → ${REMOTE_USER}@${EC2_IP}:${REMOTE_DIR}"
rsync -avz --delete --progress \
  --exclude='.venv' \
  --exclude='.git' \
  --exclude='__pycache__' \
  --exclude='.pytest_cache' \
  --exclude='.ruff_cache' \
  --exclude='.env' \
  --exclude='*.pem' \
  --exclude='logs' \
  --exclude='var' \
  -e "ssh ${SSH_OPTS[*]}" \
  "${PROJECT_DIR}/" \
  "${REMOTE_USER}@${EC2_IP}:${REMOTE_DIR}/"

echo "==> 원격 의존성 설치 및 서버 (재)시작"
ssh "${SSH_OPTS[@]}" "${REMOTE_USER}@${EC2_IP}" bash -s <<'REMOTE'
set -Eeuo pipefail
source "$HOME/.local/bin/env"
cd ~/dameokja/fastapi
mkdir -p var/uploads
uv sync --extra dev --extra db
chmod +x start.sh stop.sh restart.sh
sudo cp deploy/receipt-ai.service /etc/systemd/system/receipt-ai.service
sudo systemctl daemon-reload
sudo systemctl enable --now receipt-ai.service
sudo systemctl restart receipt-ai.service
curl --silent --fail --retry 30 --retry-delay 1 --retry-connrefused http://127.0.0.1:8000/health/live >/dev/null
REMOTE

echo "==> 완료. 헬스체크:"
echo "curl http://${EC2_IP}:8000/health/live"
