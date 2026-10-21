#!/usr/bin/env bash
# FastAPI 표준 출력 로그(ECS → CloudWatch Logs)에 지표 필터와 경보를 만든다.
# Prometheus 수집 인프라가 없어도 콜백 실패·Worker 장애·큐 포화를 바로 감지하기 위한 1차 경보다.
# 같은 이름으로 다시 실행하면 필터와 경보를 덮어쓰므로 임계값을 바꾼 뒤 재실행하면 된다.
#
# 사용법:
#   LOG_GROUP=/ecs/<dev AI 로그 그룹> ALARM_TOPIC_ARN=arn:aws:sns:ap-northeast-2:<계정>:<토픽> \
#   ENV_NAME=dev ./monitoring/cloudwatch-log-alarms.sh
set -Eeuo pipefail

: "${LOG_GROUP:?LOG_GROUP(FastAPI 컨테이너 로그 그룹 이름)이 필요합니다}"
: "${ALARM_TOPIC_ARN:?ALARM_TOPIC_ARN(경보를 받을 SNS 토픽 ARN)이 필요합니다}"
ENV_NAME="${ENV_NAME:-dev}"
REGION="${AWS_REGION:-ap-northeast-2}"
NAMESPACE="Dameokja/AI"

# 이름|로그 패턴|평가 기간(초)|임계값(이상)|설명
# 패턴은 app/ 코드의 로그 메시지 첫 단어와 같아야 한다. 메시지를 바꾸면 이 표도 함께 고친다.
RULES=(
  "CallbackExhausted|\"callback.exhausted\"|300|1|콜백 재시도를 모두 실패해 백엔드가 결과를 받지 못함"
  "CallbackRejected|\"callback.rejected\"|300|1|백엔드가 콜백을 400·401·409 등으로 거부함(계약 불일치 의심)"
  "CallbackDeliveryError|\"callback.delivery.error\"|300|1|콜백 본문 생성·전송 중 예외"
  "WorkerCallFailed|\"remote_inference.failed\"|300|3|GPU Worker 호출 실패 반복(Worker 정지·키 불일치 의심)"
  "AnalysisFailed|\"analysis.failed\"|600|4|분석 실패가 10분 동안 3건 초과"
  "UnexpectedAnalysisFailure|\"unexpected analysis failure\"|300|1|처리되지 않은 예외로 분석 실패"
  "AnalysisQueueFull|\"analysis.queue_full\"|300|1|대기열 100건이 가득 차 요청을 거절함"
  "ReferenceLookupFailed|?\"product_catalog_lookup.failed\" ?\"shelf_life_lookup.failed\"|600|5|PostgreSQL 카탈로그·소비기한 조회 실패 반복"
)

for rule in "${RULES[@]}"; do
  IFS="|" read -r name pattern period threshold description <<<"$rule"
  # 비정형 로그라 dimension을 뽑을 수 없으므로 환경 이름을 지표 이름에 붙여 dev·stage·prod를 구분한다.
  metric="${name}-${ENV_NAME}"

  aws logs put-metric-filter \
    --region "$REGION" \
    --log-group-name "$LOG_GROUP" \
    --filter-name "dameokja-ai-${ENV_NAME}-${name}" \
    --filter-pattern "$pattern" \
    --metric-transformations "metricName=${metric},metricNamespace=${NAMESPACE},metricValue=1,unit=Count"

  aws cloudwatch put-metric-alarm \
    --region "$REGION" \
    --alarm-name "dameokja-ai-${ENV_NAME}-${name}" \
    --alarm-description "$description" \
    --namespace "$NAMESPACE" \
    --metric-name "$metric" \
    --statistic Sum \
    --period "$period" \
    --evaluation-periods 1 \
    --threshold "$threshold" \
    --comparison-operator GreaterThanOrEqualToThreshold \
    --treat-missing-data notBreaching \
    --alarm-actions "$ALARM_TOPIC_ARN" \
    --ok-actions "$ALARM_TOPIC_ARN"

  echo "적용: ${name} (${period}초 동안 ${threshold}건 이상)"
done
