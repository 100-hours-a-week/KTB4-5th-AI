# 레시피 비동기 API / 백엔드 연동

이 계약은 레시피에만 적용한다. 기존 사진 분석 API/콜백과 레시피 원천 동기화 API는 유지한다. 이전 동기 POST의 HTTP 200 추천 응답은 HTTP 202 접수 응답으로 변경된다. 공통 API-명세.md를 수정한 문서는 아니며 백엔드 연동은 이 계약과 OpenAPI를 기준으로 맞춘다.

## 접수

`POST /ai/v1/recipe-recommendations`

인증: `X-Internal-API-Key: INTERNAL_API_KEY`. 백엔드는 사용자/냉장고 소유권을 검증하고 확정 재고 스냅샷을 보낸다.

```json
{
  "requestId": "recipe-request-20261028-001",
  "refrigeratorId": 12,
  "inventoryFingerprint": "fb9317e41709357ffd835bc85e3042619b94b4c3a90559f94ce3ded59a16a0fe",
  "inventory": [
    {"ingredientId":1,"name":"당근","category":"VEGETABLE","storageType":"REFRIGERATED","measureType":"COUNT","quantity":1,"weightValue":null,"weightUnit":"NONE","expirationDate":"2026-10-28"},
    {"ingredientId":2,"name":"당근","category":"VEGETABLE","storageType":"REFRIGERATED","measureType":"COUNT","quantity":3,"weightValue":null,"weightUnit":"NONE","expirationDate":"2026-11-02"}
  ],
  "preferenceTags": ["매운거", "칼칼한거"],
  "limit": 10,
  "targetServings": 2,
  "excludedIngredientNames": [],
  "recentRecipeIds": []
}
```

| 필드 | 규칙 |
| --- | --- |
| requestId | 필수, 최대 500자. 같은 내용 재시도는 같은 값, 새 추천은 새 값 |
| refrigeratorId | 필수, 양의 정수 |
| inventoryFingerprint | 필수, 확정 재고 JSON의 SHA-256 |
| inventory | 필수, 최대 300개, ingredientId는 양수/중복 금지 |
| ingredientKey | 재고별 선택 필드, 검토된 기본 식재료 키. 없으면 상품명 사전 사용 |
| preferenceTags | 선택, 기본 [], 최대 12개, 각 1~40자. 선행 #와 중복은 정리 |
| limit | 기본 10, 1~10. 적합 후보가 적으면 그 미만 |
| targetServings | 기본 2, 1~12 정수 |
| recommendationMode | 기본 EXCLUDE_EXPIRED. INCLUDE_EXPIRED는 명시적 요청이며 섭취 안전을 보증하지 않음 |
| strategies | 기존 3종 유지. USE_EXPIRING은 서버가 항상 포함하며 비임박 조합도 가능 |
| excludedIngredientNames | 최대 100개. 취향과 별도이며 제외 조건 우선 |
| pantryIngredientNames | 기존 선택 필드 유지. 검토된 양념은 기본 구매 필요 목록에서 제외 |
| recentRecipeIds | 최대 100개. SOURCE:sourceRecipeId 형식 |

COUNT는 quantity=1~100 정수, weightValue=null, weightUnit=NONE이다. WEIGHT는 quantity=null, weightValue>0, weightUnit=G/ML이다. 재고 사진 분석의 미인식 0과 다르게 추천에는 확정된 사용 가능 재고를 보낸다.

fingerprint는 재고 ID 오름차순, 이름 NFKC/소문자/공백 제거, JSON 키 정렬, 공백 없는 구분자, UTF-8 SHA-256으로 계산한다. quantity/weightValue null도 포함한다. ingredientKey가 null/미제공이면 그 키를 제외하고 제공됐으면 이름처럼 정규화한다. 태그, requestId, limit은 재고 fingerprint에 포함하지 않는다. 기준 구현은 schemas.inventory_fingerprint다.

202 응답 예시:

```json
{"jobId":"rjob_example","requestId":"recipe-request-20261028-001","status":"QUEUED","submittedAt":"2026-10-28T03:00:00Z"}
```

이 응답에는 추천 본문이 없다. QUEUED → PROCESSING → COMPLETED/FAILED로 진행한다. 이미 완료된 동일 요청을 다시 보내면 같은 jobId와 현재 상태를 202로 반환한다. 같은 requestId에 다른 재고/태그/인분 등이 들어오면 409 IDEMPOTENCY_CONFLICT다.

## 결과 조회

`GET /ai/v1/recipe-recommendations/{jobId}` / 동일 내부 인증

200 본문은 jobId, requestId, refrigeratorId, status, submittedAt, startedAt, completedAt, result, error다. 처리 중 result/error/completedAt은 null이다. 완료 시 result가 기존 RecommendationResponse이며 실패 시 result=null, error에 code/message/retryable을 담는다. UTC 날짜/시간은 Z로 직렬화한다.

result에는 최대 10개 recommendations, 별도 confirmationCandidates, 최종 미활용 임박 재료, 적용한 preferenceTags/context, 엔진/경고/trace가 있다. 각 레시피는 원문 재료/분량/조리 단계, 추가 구매 식재료, 분량 상태, preferenceMatch와 inventoryUsage를 포함한다.

quantityScope=FOOD_INGREDIENTS다. quantityStatus와 QUANTITY_VERIFIED는 검토된 양념/물을 제외한 식재료의 분량 기준이며, 양념/물은 개별 요구량에서 PANTRY_UNVERIFIED를 유지한다.

preferenceMatch는 AI 판단 MATCHED, 평가했지만 적합 근거가 없는 NOT_MATCHED, 미평가/실패 UNKNOWN, 무취향 NOT_REQUESTED다. 알레르기 안전 판정이 아니다. 알맞은 결과가 없어도 임의 요리를 생성하지 않으며 기존 후보와 경고를 반환할 수 있다.

MATCHED에는 preferenceEvidence로 원문 인용을 함께 제공한다. 인용이 원문에 없거나 알려진 취향 조건과 모순되면 UNKNOWN으로 처리한다. AI_INPUT_WINDOW_REDUCED는 입력 크기로 AI 평가 범위만 줄였다는 뜻이며 최종 원문/후보를 삭제했다는 뜻은 아니다. trace.aiInputBytes/aiCandidateCount/timingsMs로 마지막 AI 입력과 추천 생성 단계 시간을 확인한다. 큐 대기/콜백 시간은 job의 시각과 로그로 별도 확인한다.

웹 보충의 trace.webDiscoveredCount는 받은 검색 결과 수, webExtractionCalls는 규칙 실패 후 Gemma EXTRACT 시도 수, webParseFailureCounts는 최종 추출 실패 사유별 건수다. timingsMs.webExtractionMs는 해당 모델 호출 대기 포함 시간이다. aiCalls는 RANK와 EXTRACT 시도의 합계이며 recommendationEngine=AI는 최종 RANK 성공을 뜻한다. 추출만 AI를 사용하고 최종 정렬이 폴백인 경우 RULE_FALLBACK일 수 있다. 기존 요청 필드와 레시피 콜백 경로는 유지한다.

레시피 하나에 당근 3개가 필요한 경우의 inventoryUsage 부분 예시:

```json
[
  {"ingredientId":1,"name":"당근","unit":"EA","availableAmount":1,"usedAmount":1,"remainingAmount":0,"allocationStatus":"CALCULATED","allocations":[{"ingredientNo":1,"usedAmount":1}]},
  {"ingredientId":2,"name":"당근","unit":"EA","availableAmount":3,"usedAmount":2,"remainingAmount":1,"allocationStatus":"CALCULATED","allocations":[{"ingredientNo":1,"usedAmount":2}]}
]
```

이것은 실제 추천 요리/원문 분량을 제시한 예시가 아니라 ID별 배분 형식 예시다. 원문 분량이 있어야만 계산한다. CALCULATED는 해당 배분량 계산 가능, UNRESOLVED는 분량/인분/단위 미확정, UNUSED는 계산된 사용량이 없다는 뜻이다. UNRESOLVED의 usedAmount=0을 실제로 필요 없다는 뜻으로 해석하지 않는다. 단위 환산이 불가능하면 임의로 개수/G를 바꾸지 않는다.

사용자는 하나의 레시피를 선택한다. 10개의 사용량을 합산해 차감하면 안 된다. 백엔드는 선택 시 재고 fingerprint와 최신 수량을 확인하고 원자적으로 차감한다. AI 서버는 가상 배분만 반환한다.

## 완료 콜백

백엔드가 레시피 전용 수신 경로를 마련한 뒤 다음 설정을 등록한다. 경로는 설정값이며 AI가 임의로 가정하지 않는다.

- RECIPE_CALLBACK_URL: 백엔드 레시피 작업 수신 URL
- RECIPE_CALLBACK_API_KEY: AI → 백엔드 X-Internal-API-Key 값

콜백 본문은 GET의 최종 RecipeJobView와 같다. jobId/requestId/refrigeratorId로 요청을 연결하고 result 안에 모든 레시피와 재고 ID별 배분을 함께 전달한다. 영수증 CALLBACK_URL에는 보내지 않는다. 요청 본문에서 임의 콜백 URL을 받지 않는다.

2xx는 성공, 429/500/502/503/504와 전송 오류는 CALLBACK_MAX_ATTEMPTS 범위에서 재시도한다. 다른 HTTP 응답은 거부로 기록한다. 성공한 요청이 재시도되는 경우를 고려해 백엔드는 jobId 기준 멱등 upsert를 수행한다. 콜백 실패로 추천 결과를 FAILED로 뒤집지 않으며 GET으로 확인할 수 있다. 자동 무한 재전송은 하지 않는다.

## 오류/운영

| HTTP | 코드/상태 | 의미 |
| --- | --- | --- |
| 202 | QUEUED/PROCESSING/COMPLETED/FAILED | 신규 또는 동일 작업 접수 확인 |
| 200 | 작업 조회 | 진행/완료/실패 스냅샷 |
| 400 | INVALID_REQUEST | 재고 단위/fingerprint/태그/개수 등의 검증 오류 |
| 401 | UNAUTHORIZED_SERVICE | 내부 인증 실패 |
| 404 | RECIPE_JOB_NOT_FOUND | 작업/영속 결과 없음 |
| 409 | IDEMPOTENCY_CONFLICT | 동일 requestId에 다른 내용 |
| 429 | RECIPE_QUEUE_FULL | 대기 큐 최대 100건 또는 메모리 결과 한도 포화 |
| 503 | RECIPE_NOT_READY / DEPENDENCY_UNAVAILABLE | 접수 중지 또는 DB/022 스키마 오류 |

접수 후 작업 오류는 GET/콜백 status=FAILED로 알린다. RECIPE_TIMEOUT, RECIPE_INTERNAL_ERROR, RECIPE_WORKER_STOPPED 또는 외부 의존성 오류 코드다. 정상 DB 조회에서 후보가 없는 것은 실패가 아니라 COMPLETED + recommendationEngine=EMPTY다.

DB 사용 환경은 022_recipe_recommendation_results.sql 적용이 필요하다. terminal 결과만 보관하며 대기 작업은 메모리에 있다. 단일 FastAPI 프로세스/replica가 전제다. 정상 종료는 남은 작업을 실패로 저장/전달하려고 제한된 시간 동안 시도한다. 강제 종료 시 미완료 작업은 소실될 수 있으므로 큐 영속/재시작 자동 복구로 해석하지 않는다. 운영 보장은 [추천 설계](recipe_ai_recommendations.md)의 운영 절을 따른다.
