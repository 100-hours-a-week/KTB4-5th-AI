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

v13의 trace.webSearchCacheHits와 webExtractionCacheHits는 프로세스 메모리의 검색/검증된 본문 재사용 횟수다. 캐시 재사용 시 webSearchCount 또는 webExtractionCalls가 0이어도 웹 추천이 포함될 수 있다. 취향이 있으면 MATCHED → UNKNOWN → NOT_MATCHED 순으로 비교하고 같은 상태에서 임박 활용을 우선합니다. 취향 일치 + 임박 활용이 최우선이며 취향 일치 비임박 후보도 취향 불일치 임박 후보보다 앞섭니다. 서로 다른 원문 근거 수, 새로운 임박 주재료 / 전체 임박 재료 활용, 부족 식재료 5종 이내와 분량 확인 상태를 뒤이어 비교합니다. 최종 순위는 FastAPI의 단일 선정 단계에서 결정하고 Gemma는 취향 원문 근거를 평가합니다.

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

## 태그별 취향 판정

각 recommendations/confirmationCandidates 항목에 preferenceAssessments를 추가한다. 요청 태그 순서를 유지하며 취향이 없으면 []다. GET result와 레시피 콜백 result에서 같은 스키마를 사용한다.

```json
"preferenceAssessments": [
  {"tag": "매운거", "status": "MATCHED", "evidence": "고춧가루"},
  {"tag": "칼칼한거", "status": "NOT_MATCHED", "evidence": null}
]
```

MATCHED는 해당 태그의 모델 주장/원문 검증 통과, NOT_MATCHED는 평가했으나 유효한 적합 근거 없음, UNKNOWN은 미평가/실패/근거 거부다. 요약 preferenceMatch=MATCHED는 태그 하나 이상 충족을 뜻한다. 원문에 고춧가루가 있어도 매운 강도까지 확정하지 않는다. 취향이 있으면 MATCHED → UNKNOWN → NOT_MATCHED 순으로 비교하고 같은 상태에서 임박 활용을 우선합니다. 취향 일치 + 임박 활용이 최우선이며 취향 일치 비임박 후보도 취향 불일치 임박 후보보다 앞섭니다. 서로 다른 원문 근거 수, 새로운 임박 주재료 / 전체 임박 재료 활용, 부족 식재료 5종 이내와 분량 확인 상태를 뒤이어 비교합니다. 최종 순위는 FastAPI의 단일 선정 단계에서 결정하고 Gemma는 취향 원문 근거를 평가합니다.

## 판정 재사용/오류 추적

trace.preferenceDecisionCacheHits는 해당 추천 실행에서 재사용한 레시피별 취향 판정 수다. 전부 재사용하면 recommendationEngine=AI여도 aiCalls=0일 수 있다. PREFERENCE_DECISION_CACHE_USED 경고로 구분한다. trace.recommendationCacheHit는 완료 추천 재사용 또는 동시 계산 공유입니다. 재사용 요청은 aiCalls / 검색 수 / 입력 크기 등의 작업 카운터가 0이며 timingsMs는 현재 요청의 recommendationMs입니다. recommendationId / generatedAt / 후보 진단은 원본 스냅샷입니다. RECOMMENDATION_SNAPSHOT_REUSED 경고로 구분합니다. trace.candidateCoverage는 임박 주재료별 eligible / shortlisted / evaluated / recommended 수이며 정확도나 전체 DB의 취향 적합 후보 수가 아닙니다.

AI_CIRCUIT_OPEN은 연결/과부하 일시 장애가 연속되어 원격 RANK 호출을 쉬는 상태이며 미평가 취향은 UNKNOWN이다. AI_TRANSIENT_RETRY는 같은 전체 시간 예산 안에서 실제 1회 재시도를 수행한 경우다. 입력/스키마/유효하지 않은 모델 출력 한 건으로 후속 사용자 요청을 막지 않는다. 캐시는 프로세스 내 최대 512개/10분이며 서버 재시작/다른 replica 간 지속성을 보증하지 않는다. SQL 변경은 없다.

### 생략 가능한 필드와 기본값

생략 시 기본값이 적용되는 것은 선택 필드입니다. requestId/refrigeratorId/inventoryFingerprint/inventory는 필수이며 생략하면 HTTP400 INVALID_REQUEST입니다. null과 생략은 다릅니다. 아래 배열/문자열/정수 필드에 null을 보내면 기본값을 쓰는 것이 아니라 검증 오류입니다. ingredientKey/quantity/weightValue처럼 스키마에서 null을 허용한 필드는 각 단위 계약을 따릅니다.

| 선택 필드 | 생략 시 값 | 의미 |
| --- | --- | --- |
| recommendationMode | EXCLUDE_EXPIRED | 이미 소비기한이 지난 재고를 후보 계산에서 제외 |
| excludedIngredientNames | [] | 별도 제외 재료 없음. 알레르기가 없다는 뜻은 아님 |
| limit | 10 | 최대 추천 수. 적합 후보가 적으면 그 미만 |
| preferenceTags | [] | 무취향 요청 |
| targetServings | 2 | 요청 인분. 원문 인분/분량이 있어야 소모량 계산 가능 |
| strategies | [USE_EXPIRING, MINIMIZE_SHOPPING, SIMPLE_COOKING] | 아래 전략 3종을 고려 |
| pantryIngredientNames | [] | 추가 보유 확인 양념 목록을 전달하지 않음 |
| recentRecipeIds | [] | 최근 추천 이력에 따른 재노출 조정 없음 |

### 전략 3종과 적용 범위

| strategies 값 | 뜻 | 적용 범위 |
| --- | --- | --- |
| USE_EXPIRING | 임박 재료 활용 | 오늘~3일 이내 소비기한의 재고 활용을 우선. 요청에서 빠져 있어도 서버가 추가 |
| MINIMIZE_SHOPPING | 추가 구매 부담 고려 | 같은 임박/취향 우선순위에서 부족한 식재료와 구매 부담을 고려 |
| SIMPLE_COOKING | 조리 수고 고려 | 원문 단계 수와 제공된 조리 정보를 모델이 참고. 단계 수를 조리시간으로 환산하지 않음 |

strategies는 별도 추천 API나 절대 정렬 모드가 아닙니다. 임박 활용 그룹, 취향 일치/충족 태그 수를 먼저 보호하고 나머지 조건을 비교합니다. 각 반환 레시피의 strategy는 해당 후보의 대표 전략입니다. 요청에서 중복 전략이나 빈 배열은 허용하지 않습니다.

### 양념과 최근 추천 이력

- pantryIngredientNames는 소금/간장 등 냉장고 재고와 별도로 보유를 확인한 양념 이름입니다. 선택 필드이며 검토된 기본 양념은 이 목록이 비어 있어도 구매 필요 목록에서 제외합니다. PANTRY_UNVERIFIED는 실제 보유량/분량을 보증하지 않습니다. 양파/두부 등 신선 식재료를 이 목록에 넣어 보유 재고로 처리할 수 없습니다. 제외 재료 조건은 양념에도 우선 적용합니다.
- recentRecipeIds는 최근 사용자에게 노출한 추천 레시피 식별자 목록입니다. 사용자가 선택/적용한 요리만을 의미하지 않습니다. 백엔드가 최근 추천 목록을 보관해 다음 요청에 전달합니다. 이력 보관 기간/횟수는 백엔드 정책이며 AI가 자동 수집하지 않습니다.
- 형식은 SOURCE:sourceRecipeId입니다. 예를 들어 응답 source=FOOD_SAFETY_KOREA, sourceRecipeId=123이면 FOOD_SAFETY_KOREA:123을 보냅니다. 백엔드 MySQL recipe_id나 AI recommendationId/jobId를 넣는 필드가 아닙니다. 현재 코드는 이 문자열 형식을 별도 정규식으로 검사하지 않고 후보 식별자와 정확히 비교합니다.
- 최근 추천 이력은 동일한 우선순위 후보의 재노출을 뒤로 미루는 힌트이며 강제 제외 목록이 아닙니다. 선호 학습/선택 이력 저장 API를 대신하지 않습니다.


### 일반 추천, 확인 후보와 미활용 임박 재료

| 실제 JSON 필드 | 의미 | 개수/주의사항 |
| --- | --- | --- |
| result.recommendations | 일반 추천 레시피 | 최대 limit, 상한 10개. 부족 재료가 있어도 포함 가능 |
| result.confirmationCandidates | 보유 식재료와 정확 매칭은 없지만 관련 계열의 주재료 재고가 있어 종류/부위/가공 상태 확인이 필요한 별도 후보 | 최대 3개. 10위 밖의 차순위 후보를 담는 배열이 아님 |
| result.uncoveredExpiringIngredients | 최종 recommendations에서 주재료로 활용되지 않은 임박 식재료 이름 | 문자열 배열. 재고 ID 배열이 아님 |

예를 들어 보유 재고의 소고기와 원문이 요구하는 쇠고기안심을 같은 재료/분량이라고 확정하지 않습니다. 정확 식품 매칭이 전혀 없고 이런 관련 계열 주재료만 있으면 confirmationCandidates로 분리하고 확인 사유를 제공합니다. 정확히 보유한 다른 식품이 있는 후보는 부족 주재료가 있어도 recommendations에 들어갈 수 있습니다. 단순 양념/물 보유만으로 정확 식품 매칭으로 보지 않습니다.

확인 후보의 feasibility는 NEEDS_CHECK이며 자동 대체/사용량 차감 대상으로 삼지 않습니다. 확인 후보만 있고 일반 추천이 없으면 recommendationEngine=EMPTY일 수 있습니다. 후보 자체가 없으면 두 배열 모두 []입니다.

문서의 한글은 설명/화면 표시 문구입니다. 실제 JSON 키는 아래 예시와 필드 표의 영문 camelCase를 그대로 사용합니다. 최종 미활용 임박 재료 등의 한글 설명을 JSON 키로 보내지 않습니다. name/reason/confirmationReasons 등 문자열 값에는 한글이 들어갈 수 있습니다.

### 결과 조회 JSON 예시

전체 형태가 확인되도록 완료 예시를 제공합니다. 아래는 실제 Dev 무취향 시험 응답에서 recommendations의 1개와 빈 확인 후보 배열만 발췌한 형식 예시입니다. trace/원천/필드 값은 해당 시험의 값이며 새로운 호출의 고정 결과가 아닙니다.

이 예시의 요청 재고는 순두부 400g / 애호박 200g / 대파 100g / 계란 200g / 팽이버섯 150g입니다. inventoryUsage는 레시피 재료 목록이 아니라 요청 당시 유효 재고 전체의 사용 계획이므로, 이 요리에 없는 애호박 / 대파 / 계란 / 팽이버섯도 allocationStatus=UNUSED로 포함됩니다. 순두부는 레시피에 매칭되지만 baseServings=null로 요청 인분의 필요량을 계산하지 못해 UNRESOLVED입니다. usedAmount=0은 실제로 필요하지 않다는 뜻이 아닙니다. ingredientId는 냉장고 재고 ID, ingredientNo는 레시피 내부 재료 번호이며 서로 다른 번호 체계입니다.

<details>
<summary>COMPLETED 응답 JSON 펼치기</summary>

```json
{
  "jobId": "rjob_d583a166728b4da8879842d17d2ac58f",
  "requestId": "v15-deployed-ad3f6dfd2d9c45019cc5e96f313b372a",
  "status": "COMPLETED",
  "submittedAt": "2026-10-29T05:14:11.313913Z",
  "refrigeratorId": 928401,
  "startedAt": "2026-10-29T05:14:11.315450Z",
  "completedAt": "2026-10-29T05:14:15.443814Z",
  "result": {
    "recommendationId": "rec_52b5019669684aab8f04a2197192bdff",
    "refrigeratorId": 928401,
    "inventoryFingerprint": "0533f5477dac0fba20408a9c0607e69dd69d623b952ed8ae8b86df41a580603c",
    "recommendationMode": "EXCLUDE_EXPIRED",
    "recommendations": [
      {
        "source": "FOOD_SAFETY_KOREA",
        "sourceRecipeId": "32",
        "sourceRevision": "924849b011b660a8defd8847e68e57632361eceee2663b4f7d92f69b9d09d540",
        "name": "순두부 사과 소스 오이무침",
        "imageUrl": "http://www.foodsafetykorea.go.kr/uploadimg/cook/10_00032_2.png",
        "sourceUrl": null,
        "baseServings": null,
        "ingredients": [
          {
            "ingredientNo": 1,
            "name": "오이",
            "amountText": "오이 70g(1/3개)",
            "measureType": "WEIGHT",
            "amountValue": 70.0,
            "unit": "G"
          },
          {
            "ingredientNo": 2,
            "name": "다진 땅콩",
            "amountText": "다진 땅콩 10g(1큰술)",
            "measureType": "WEIGHT",
            "amountValue": 10.0,
            "unit": "G"
          },
          {
            "ingredientNo": 3,
            "name": "순두부",
            "amountText": "순두부 40g(1/8봉지)",
            "measureType": "WEIGHT",
            "amountValue": 40.0,
            "unit": "G"
          },
          {
            "ingredientNo": 4,
            "name": "사과",
            "amountText": "사과 50g(1/3개)",
            "measureType": "WEIGHT",
            "amountValue": 50.0,
            "unit": "G"
          }
        ],
        "steps": [
          {
            "stepNo": 1,
            "instruction": "1. 사과, 순두부를 믹서에 넣고 곱게 갈아 소스를 만든다.",
            "imageUrl": "http://www.foodsafetykorea.go.kr/uploadimg/cook/20_00032_1.png"
          },
          {
            "stepNo": 2,
            "instruction": "2. 오이는 소금으로 문질러 씻어 반을 갈라 씨를 제거하고 어슷썰기를 한다.",
            "imageUrl": "http://www.foodsafetykorea.go.kr/uploadimg/cook/20_00032_2.png"
          },
          {
            "stepNo": 3,
            "instruction": "3. 썰어 놓은 오이에 순두부사과 소스를 넣고 버무린 후 다진 땅콩을 뿌려 마무리 한다.",
            "imageUrl": "http://www.foodsafetykorea.go.kr/uploadimg/cook/20_00032_3.png"
          }
        ],
        "score": 0.2,
        "scoreBreakdown": {
          "expiringIngredient": 0.4,
          "inventoryCoverage": 0.15,
          "missingIngredientPenalty": 0.35,
          "variety": 1.0
        },
        "usesInventory": [
          "순두부"
        ],
        "usesExpiringIngredients": [
          "순두부"
        ],
        "missingIngredients": [
          "다진 땅콩",
          "사과",
          "오이"
        ],
        "excludedIngredientCheck": "PASSED",
        "reason": "보유 재료 1종 사용, 추가로 확인할 재료 3종입니다. 3일 이내 소비기한 재료 순두부가 원문에 포함됩니다. 원문 조리 3단계이며 분량 상태는 NEEDS_CHECK입니다. 제목에 명시된 미보유 재료: 오이, 사과. 추가로 준비할 식재료: 다진 땅콩, 사과, 오이. 임박 재료 분산 활용: 순두부를 앞선 추천과 추가로 활용하도록 선정했습니다.",
        "strategy": "USE_EXPIRING",
        "targetServings": 2,
        "quantityStatus": "NEEDS_CHECK",
        "quantityScope": "FOOD_INGREDIENTS",
        "ingredientRequirements": [
          {
            "ingredientNo": 1,
            "name": "오이",
            "amountText": "오이 70g(1/3개)",
            "requiredAmount": null,
            "unit": "G",
            "allocatedAmount": null,
            "shortageAmount": null,
            "status": "UNKNOWN_AMOUNT"
          },
          {
            "ingredientNo": 2,
            "name": "다진 땅콩",
            "amountText": "다진 땅콩 10g(1큰술)",
            "requiredAmount": null,
            "unit": "G",
            "allocatedAmount": null,
            "shortageAmount": null,
            "status": "UNKNOWN_AMOUNT"
          },
          {
            "ingredientNo": 3,
            "name": "순두부",
            "amountText": "순두부 40g(1/8봉지)",
            "requiredAmount": null,
            "unit": "G",
            "allocatedAmount": null,
            "shortageAmount": null,
            "status": "UNKNOWN_AMOUNT"
          },
          {
            "ingredientNo": 4,
            "name": "사과",
            "amountText": "사과 50g(1/3개)",
            "requiredAmount": null,
            "unit": "G",
            "allocatedAmount": null,
            "shortageAmount": null,
            "status": "UNKNOWN_AMOUNT"
          }
        ],
        "inventoryUsage": [
          {
            "ingredientId": 1,
            "name": "순두부",
            "unit": "G",
            "availableAmount": 400.0,
            "usedAmount": 0.0,
            "remainingAmount": 400.0,
            "allocationStatus": "UNRESOLVED",
            "allocations": []
          },
          {
            "ingredientId": 2,
            "name": "애호박",
            "unit": "G",
            "availableAmount": 200.0,
            "usedAmount": 0.0,
            "remainingAmount": 200.0,
            "allocationStatus": "UNUSED",
            "allocations": []
          },
          {
            "ingredientId": 3,
            "name": "대파",
            "unit": "G",
            "availableAmount": 100.0,
            "usedAmount": 0.0,
            "remainingAmount": 100.0,
            "allocationStatus": "UNUSED",
            "allocations": []
          },
          {
            "ingredientId": 4,
            "name": "계란",
            "unit": "G",
            "availableAmount": 200.0,
            "usedAmount": 0.0,
            "remainingAmount": 200.0,
            "allocationStatus": "UNUSED",
            "allocations": []
          },
          {
            "ingredientId": 5,
            "name": "팽이버섯",
            "unit": "G",
            "availableAmount": 150.0,
            "usedAmount": 0.0,
            "remainingAmount": 150.0,
            "allocationStatus": "UNUSED",
            "allocations": []
          }
        ],
        "essentialIngredientNos": [
          1,
          3,
          4
        ],
        "missingEssentialIngredientNos": [
          1,
          4
        ],
        "feasibility": "NEEDS_SHOPPING",
        "confirmationReasons": [
          "원문 인분이 없어 요청 인분의 필요량을 확정할 수 없습니다."
        ],
        "usesExpiringMainIngredients": [
          "순두부"
        ],
        "preferenceMatch": "NOT_REQUESTED",
        "preferenceEvidence": null,
        "preferenceAssessments": [],
        "preparationNote": "원본 조리법을 확인하세요. 냉동 재료의 해동 조건은 원문·포장 기준을 확인하며 조리·해동시간을 별도 추정하지 않습니다."
      }
    ],
    "confirmationCandidates": [],
    "uncoveredExpiringIngredients": [],
    "generatedAt": "2026-10-29T05:14:15.435642Z",
    "recommendationEngine": "AI",
    "warnings": [
      "CONFIRMATION_CANDIDATES_SEPARATED",
      "MISSING_INGREDIENTS_REQUIRE_SHOPPING",
      "UNREVIEWED_MISSING_INGREDIENTS_NEED_CHECK",
      "AI_INPUT_WINDOW_REDUCED",
      "SOURCE_SERVINGS_UNAVAILABLE",
      "SOURCE_QUANTITIES_UNRESOLVED",
      "PARTIAL_STRATEGY_COVERAGE"
    ],
    "trace": {
      "policyVersion": "recipe-rerank-v15",
      "model": "gemma4:e2b",
      "promptVersion": "recipe-grounded-v6",
      "aiCalls": 1,
      "preferenceDecisionCacheHits": 0,
      "searchCount": 1,
      "candidateCount": 147,
      "externalSearchCount": 0,
      "webSearchCount": 0,
      "webDiscoveredCount": 0,
      "webExtractionCalls": 0,
      "webSearchCacheHits": 0,
      "webExtractionCacheHits": 0,
      "webParseFailureCounts": {},
      "aiCandidateCount": 16,
      "aiInputBytes": 15292,
      "timingsMs": {
        "databaseSearchMs": 23.479,
        "initialCandidateEvaluationMs": 182.095,
        "webExtractionMs": 0.0,
        "aiRankMs": 3880.322,
        "recommendationMs": 4119.036
      }
    },
    "preferenceTags": [],
    "context": {
      "localDate": "2026-10-29",
      "timezone": "Asia/Seoul",
      "season": "AUTUMN",
      "mealTime": "LUNCH",
      "variationSeed": "514d14d6bf0cf542",
      "personalizationMode": "CONTEXT_RANDOM"
    }
  },
  "error": null
}
```

</details>

QUEUED/PROCESSING 상태에서는 result/error/completedAt이 null입니다. FAILED에서는 result=null이고 error에 code/message/retryable을 제공합니다. 상태별 필드 목록은 RecipeJobView 표와 같습니다.

## 레시피 추천 처리 경계와 확정 정책

취향이 있으면 MATCHED → UNKNOWN → NOT_MATCHED 순으로 비교하고 같은 상태에서 임박 활용을 우선합니다. 취향 일치 + 임박 활용이 최우선이며 취향 일치 비임박 후보도 취향 불일치 임박 후보보다 앞섭니다. 서로 다른 원문 근거 수, 새로운 임박 주재료 / 전체 임박 재료 활용, 부족 식재료 5종 이내와 분량 확인 상태를 뒤이어 비교합니다. 최종 순위는 FastAPI의 단일 선정 단계에서 결정하고 Gemma는 취향 원문 근거를 평가합니다.

원천 자체가 없는 임박 주재료는 먼저 보충할 수 있습니다. 취향 부족 보충은 해당 재료의 적합 DB 후보를 모두 정상 평가했는데도 MATCHED가 없을 때 실행합니다. 평가 창 밖 UNKNOWN을 후보 부재로 단정하지 않습니다. 후보가 적은 임박 재료의 대표성을 먼저 확보하고 잔여 슬롯으로 작은 후보군의 평가를 완성합니다.

외부 보충은 한 추천에서 최대 한 번, RECIPE_SUPPLY_TIMEOUT_SECONDS 기본 8초 / 범위 1~20초이며 공식 원천 / Tavily / 추출이 공유합니다. 공식 원천 대기는 최대 3초이고 전체 AI 기본 45초 예산의 잔여시간도 넘지 않습니다. RANK는 최대 두 단계, 각 단계 기본 20초와 전체 잔여시간 중 작은 값으로 제한합니다. 보충 시간 초과는 RECIPE_SUPPLY_BUDGET_EXHAUSTED로 구분하고 검증된 추천을 적게 반환합니다. 전체 큐 / DB / 콜백 SLA를 뜻하지 않습니다.

취향이 있으면 requestId를 제외한 재고 스냅샷 / 냉장고 / 태그 집합 / 인분 / 제외 조건 / 최근 추천 / 정책 / 모델 / 공급자 설정 / 한국 날짜와 시간대를 기준으로 기본 300초 같은 추천을 재사용합니다. 무취향은 요청 seed를 구분해 동등 후보를 다양화합니다. 멱등 접수의 requestId 검증은 유지합니다. 캐시는 프로세스 메모리이며 재시작 / replica 공유는 보장하지 않습니다.

trace.recommendationCacheHit는 완료 추천 재사용 또는 동시 계산 공유입니다. 재사용 요청은 aiCalls / 검색 수 / 입력 크기 등의 작업 카운터가 0이며 timingsMs는 현재 요청의 recommendationMs입니다. recommendationId / generatedAt / 후보 진단은 원본 스냅샷입니다. RECOMMENDATION_SNAPSHOT_REUSED 경고로 구분합니다. trace.candidateCoverage는 임박 주재료별 eligible / shortlisted / evaluated / recommended 수이며 정확도나 전체 DB의 취향 적합 후보 수가 아닙니다.

부족 식재료가 있으면 SHOPPING_COST_UNVERIFIED와 2만원 충족 미확인 사유를 표시합니다. 5종은 비용 환산값이 아니며 실제 가격을 생성하지 않습니다. source_processing은 원문 지문별 성공 / 실패 유예와 동시 추출 공유, replenishment는 공급 예산과 저장 승인, result_cache는 추천 스냅샷, selection은 최종 선정을 담당합니다. 독립 수집 작업 / 영속 취향 프로필은 포함하지 않습니다. 새 SQL 없이 FastAPI와 Worker recipe-grounded-v8을 함께 배포합니다.
