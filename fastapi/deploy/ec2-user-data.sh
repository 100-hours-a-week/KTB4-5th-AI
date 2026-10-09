#!/bin/bash
# EC2 Spot 인스턴스 시작 시 자동 실행되는 User Data 스크립트 (Amazon Linux 2023 기준).
# 스팟 요청/시작 템플릿의 "고급 세부 정보 > 사용자 데이터"에 이 파일 내용을 그대로 붙여넣는다.
# 코드 배포는 하지 않고, 인스턴스마다 반복되는 OS·Ollama 준비만 담당한다.
set -Eeuo pipefail

dnf update -y
dnf install -y python3.11 git rsync libglvnd-glx
# curl은 AL2023에 curl-minimal로 이미 있음. 별도 설치 시 충돌하므로 생략.

# uv 설치 (root로 실행되므로 ec2-user 홈에 설치되도록 sudo -u 사용)
sudo -u ec2-user bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'

mkdir -p /home/ec2-user/dameokja/fastapi
chown -R ec2-user:ec2-user /home/ec2-user/dameokja
