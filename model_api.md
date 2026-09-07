# 단계 1: 모델 API 설계

## 1. 이 설계가 우리 서비스에 필요한 이유

우리 서비스에서 사진 인식 결과는 단순한 이미지 설명이 아니라 냉장고 재고, 소비기한 알림, 레시피 추천의 입력 데이터가 됩니다. 이미지 분석 형식이 바뀔 때마다 백엔드와 프론트엔드도 함께 수정해야 한다면 모델 개선 속도가 느려지고, 잘못 읽은 날짜가 그대로 사용자 알림에 반영될 수 있습니다.

따라서 AI 모델을 하나의 독립된 내부 서비스로 두고 다음 계약을 고정합니다.

- 어떤 이미지가 들어왔는지 판별합니다.
- 이미지에서 직접 확인한 값과 AI가 추정한 값을 구분합니다.
- 제품 개수와 식품 용량을 분리하고, 확인된 용량은 `g` 또는 `ml`로 표준화합니다.
- 서비스가 처리할 수 있는 고정된 카테고리와 상태값만 반환합니다.
- 불확실한 결과는 사용자 확인 대상으로 전달합니다.
- 레시피 추천은 MySQL에서 후보를 조회한 뒤 안전 필터와 규칙 점수의 근거를 함께 반환합니다.

이 계약을 지키면 문자 인식·분류 모델이나 제한적으로 사용하는 외부 인공지능 서비스를 교체하더라도 앱은 같은 응답 형식을 사용할 수 있습니다. 외부 API 사용량을 줄이고 자체 모델의 처리 비중을 높일 때도 서비스 코드의 변경 범위를 최소화할 수 있습니다.

## 2. 서비스 내 역할

```mermaid
flowchart TD
    U[PWA 사용자] --> B[서비스 백엔드]
    B -->|업로드 주소 발급| U
    U -->|이미지 업로드| S[(S3)]
    B -->|분석 요청| A[AI Model API]
    A --> TQ[입력 유형 판별 큐]
    TQ --> T{MobileNetV3 유형 판별}
    T -- 영수증 --> RQ[영수증 분석 큐]
    T -- 식품 실물 --> PQ[실물 분석 큐]
    T -- 미인식 --> N[재촬영·직접 입력]
    RQ --> R[영수증 분석<br/>OCR·상품 사전·필요 시 Gemma]
    PQ --> P[실물 분석<br/>바코드·포장 문자·식품 후보]
    R --> VQ[공통 결과 검증 큐]
    P --> VQ
    VQ --> X[임시 분석 결과]
    X --> B
    B -->|결과 확인·수정| U
    U -->|사용자 확정| B
    B --> I[(냉장고 재고)]
```

그림에서는 전체 서비스 경계와 유형별 분기만 보여줍니다. 캐시 조회, MySQL 별칭·비식품 사전, 외부 인공지능 API 보완, 단계별 재시도와 피드백 저장은 아래 모델 호출 조건과 API 명세에서 설명합니다.

AI Model API는 외부 사용자에게 직접 공개하지 않습니다. 인증, 냉장고 소유권 확인, 요청량 제한은 서비스 백엔드가 담당하고 AI API는 내부 서비스 인증을 통과한 요청만 받습니다.

이미지 분석은 수 초가 걸리거나 재시도가 발생할 수 있으므로 비동기 작업으로 처리합니다. 사용자는 업로드 직후 `처리 중` 상태를 받고, 완료 후 결과 확인 화면으로 이동합니다.

### PWA와 EC2의 책임 경계

| 영역 | 담당 기능 | 담당하지 않는 기능 |
| --- | --- | --- |
| PWA | 촬영·파일 선택, 이미지 크기 조절·압축, S3 업로드, 작업 상태 조회, 결과 확인·수정 | 분류, 문자 인식, 상품 판별, 보관기간 계산, 추천 순위 계산 |
| 서비스 백엔드 | 인증·권한, Presigned URL 발급, 분석 작업 생성, 재고 저장, 결과 조회 | 모델 추론 |
| EC2 AI 워커 | 전처리, MobileNetV3 입력 유형 분류, PP-OCRv5 문자 인식, 바코드, MySQL 사전 조회, 필요한 경우 Gemma, 결과 정규화·상태 판정 | 사용자 인증과 냉장고 권한 판단 |
| 레시피 추천 모듈 | MySQL 레시피 조회, 알레르기·비선호 제외, 임박도·보유율·취향 규칙 점수 계산 | 이미지 업로드와 재고 원본 변경 |

브라우저 기기 성능에 따라 결과가 달라지지 않도록 자체 모델 추론은 EC2에서 수행합니다. 외부 API로 보완하는 요청의 추론은 해당 공급자 환경에서 실행하며, 호출과 결과 검증은 서버에서 담당합니다. PWA의 이미지 리사이즈·압축은 추론이 아니라 전송량을 줄이기 위한 입력 준비 단계입니다.

초기 출시에서는 AWS 그래픽 처리장치를 사용하지 않습니다. EC2 `m7i.xlarge` 중앙처리장치에 입력 유형, 영수증, 실물, Gemma 보완 작업자를 모델별로 하나씩 실행합니다. 동일 모델은 한 번에 한 요청만 처리하지만 서로 다른 모델 작업자는 파이프라인처럼 동시에 동작합니다. Qwen-VL과 vLLM은 운영 구성에 포함하지 않으며, 자체 경로가 품질 또는 처리시간 기준을 충족하지 못한 요청만 월 12만 원 한도에서 외부 인공지능 API로 보완합니다.

### 2.1 모델별 역할과 호출 조건

| 순서 | 모델·기술 | 역할 | 호출 조건 |
| --- | --- | --- | --- |
| 1 | MobileNetV3-Small | `RECEIPT`, `PRODUCT`, `OTHER` 입력 유형 판별 | 판독 가능한 모든 이미지 |
| 2 | PP-OCRv5 모바일 검출·한국어 모바일 인식 | 문자, 신뢰도와 좌표 추출 | 영수증 및 글자가 있는 포장 제품 |
| 3 | 바코드와 MySQL 상품·별칭·비식품 사전 | 표준 상품명, 식품 여부, 카테고리와 기본 용량 확정 | 바코드 또는 OCR 후보가 있는 경우 |
| 4 | Ollama로 실행하는 Gemma 4 5.1B 4비트 모델 | 사전에 없는 OCR 상품행의 식품 여부와 카테고리 후보 제안 | 사전으로 확정할 수 없는 영수증 후보만 |
| 5 | 결정 규칙 | 원문 일치, 중복, 필수값, 날짜와 상태 검증 | 모든 자체 분석 결과 |
| 6 | 외부 인공지능 API | 자체 경로가 실패한 어려운 이미지 보완 | 예산·요청 제한과 사용자 동의를 통과한 일부 요청 |

Gemma는 OCR에 없는 상품명을 새로 만들거나 최종 상품명을 자유롭게 요약하지 않습니다. OCR 원문 중 후보를 선택하는 역할로 제한하고, 최종 표준화 근거가 부족하면 `NEEDS_REVIEW`로 반환합니다.

### 2.2 현재 검증 상태가 API에 미치는 영향

2단계의 외부 영수증 21장 최초 평가에서는 상품행 정밀도 95.6%, 재현율 81.1%, 종합점수 87.8%였으며, OCR부터 후처리까지 전체 95% 완료시간은 18.44초였습니다. 이후 세 번 반복한 현재 기준은 정밀도 95.1%, 재현율 73.6%, 종합점수 83.0%입니다. 반복 실험은 기존 OCR 결과를 사용한 후처리 비교이며, 후처리만의 95% 완료시간은 약 11.9~15.7초입니다. 이를 최초 실험의 전체 처리시간이나 실제 EC2 측정값으로 해석하지 않습니다. 정확도 목표인 재현율·종합점수 90%에 미달하고, 최초 실험의 전체 처리시간도 긴 영수증 목표 12초를 넘었으므로 현재 파이프라인을 자동 등록 모델로 승인하지 않습니다.

따라서 API는 다음 원칙을 지킵니다.

- 모델 출력만으로 재고를 바로 저장하지 않고 항상 결과 확인 단계를 거칩니다.
- 긴 영수증, 상품행 누락 가능성, 비식품 후보, 깨진 상품명은 `NEEDS_REVIEW`로 반환합니다.
- `RECOGNIZED`는 별도 촬영 평가 자료와 실제 EC2 측정에서 승인 기준을 통과한 경로에만 허용합니다.
- `modelTrace`에 파이프라인·모델·정책·상품 사전 버전과 외부 보완 사용 여부를 남깁니다.
- 처리시간을 넘긴 작업은 무한 재시도하지 않고 외부 보완 또는 직접 입력으로 전환합니다.

## 3. 상태와 근거 설계

### 3.1 사용자 표시 상태

| API 값 | 사용자 표시 | 결정 기준 | 사용자 행동 |
| --- | --- | --- | --- |
| `RECOGNIZED` | 표시 없음 | 이미지에서 필수값을 직접 확인했고 검증 규칙을 통과함 | 결과를 그대로 등록하거나 수정 |
| `UNRECOGNIZED` | 미인식 | 입력 유형 또는 식재료를 식별할 근거를 찾지 못함 | 다시 촬영하거나 직접 입력 |
| `NEEDS_REVIEW` | 확인 필요 | 일부 값 누락, 낮은 화질, 모델 간 충돌, 비정상 날짜 등으로 자동 확정할 수 없음 | 표시된 필드를 확인·수정 |
| `AI_ESTIMATED` | AI 예상 | 식재료는 식별했지만 날짜가 이미지에 없어 MySQL의 검증된 보관기간 자료로 기간을 추정함 | 추정 근거를 보고 날짜 확정 또는 수정 |

### 3.2 상태 우선순위

한 항목에 여러 조건이 동시에 발생하면 다음 순서로 최종 상태를 결정합니다.

```text
UNRECOGNIZED > NEEDS_REVIEW > AI_ESTIMATED > RECOGNIZED
```

예를 들어 우유는 식별했지만 소비기한이 보이지 않고 이미지도 심하게 흐리다면 `AI_ESTIMATED`가 아니라 `NEEDS_REVIEW`를 반환합니다. 품질 문제를 감춘 채 추정값을 제공하지 않기 위함입니다.

### 3.3 필드별 출처

항목 전체 상태와 별개로 각 필드는 출처를 가집니다.

| 출처 | 의미 |
| --- | --- |
| `OCR` | 이미지의 문자 영역에서 직접 읽음 |
| `VISION` | 실물 형태 또는 패키지 특징으로 인식함 |
| `BARCODE` | 바코드와 상품 DB로 확인함 |
| `RECEIPT_LINE` | 영수증 상품 행에서 읽음 |
| `KNOWLEDGE_ESTIMATE` | MySQL의 검증된 보관기간 자료에서 추정함 |
| `USER_CONFIRMED` | 사용자가 확인하거나 수정함 |

소비기한 알림은 `OCR`, `BARCODE`, `USER_CONFIRMED` 값을 우선합니다. `KNOWLEDGE_ESTIMATE`는 실제 표시 날짜가 아니며 화면에서도 반드시 `AI 예상`으로 구분합니다.

### 3.4 수량과 용량 규칙

| 필드 | 의미 | 예시 |
| --- | --- | --- |
| `quantity` | 제품 또는 묶음의 개수 | 우유 2팩이면 `2` |
| `capacity.value` | 확인된 용량의 숫자 | `500` |
| `capacity.unit` | 내부 표준 용량 단위 | `G` 또는 `ML` |
| `capacity.scope` | 총용량인지 개별용량인지 구분 | `TOTAL`, `PER_ITEM`, `UNKNOWN` |
| `capacity.source` | 용량을 확인한 근거 | `OCR`, `BARCODE`, `RECEIPT_LINE`, `USER_CONFIRMED` |

- `500g`, `1kg` 등 중량 표시는 각각 `500 G`, `1000 G`로 저장합니다.
- `500ml`, `1L` 등 부피 표시는 각각 `500 ML`, `1000 ML`로 저장합니다.
- 대소문자와 공백 차이는 정규화하지만 원문은 `evidenceText`에 보관합니다.
- 단위 없는 숫자를 용량으로 추정하지 않습니다.
- `kg ↔ g`, `L ↔ ml` 이외의 밀도 기반 환산은 하지 않습니다.
- `2개입 500g`처럼 총용량만 표시된 경우 `quantity=2`, `capacity.scope=TOTAL`로 저장합니다.
- 총용량과 개별용량을 구분하지 못하면 `capacity.scope=UNKNOWN` 및 `NEEDS_REVIEW`로 처리합니다.

## 4. 엔드포인트 목록

기본 경로는 `/ai/v1`이며, 모델 변경이 아니라 API 계약이 깨질 때만 버전을 올립니다.

| Method | Endpoint | 기능 |
| --- | --- | --- |
| `POST` | `/ai/v1/analyses` | 이미지 분석 작업 생성 |
| `GET` | `/ai/v1/analyses/{analysisId}` | 작업 상태와 분석 결과 조회 |
| `POST` | `/ai/v1/analyses/{analysisId}/feedback` | 사용자가 확정한 값을 모델 개선 데이터로 수집 |
| `POST` | `/ai/v1/recipe-recommendations` | 재고·취향·제약조건을 이용해 레시피 추천 |
| `GET` | `/ai/v1/models/status` | 배포 모델 버전과 서비스 준비 상태 확인 |

이미지는 AI API에 Base64로 직접 전달하지 않습니다. 서비스 백엔드가 접근 시간이 제한된 저장소 참조를 전달하여 요청 크기와 메모리 사용량을 줄입니다.

## 5. 이미지 분석 API 명세

### 5.1 분석 작업 생성

`POST /ai/v1/analyses`

#### 요청

```json
{
  "requestId": "req_01JEXAMPLE",
  "image": {
    "url": "s3://uploads/2026/08/31/image-001.webp",
    "sha256": "72d88f..."
  },
  "inputHint": "AUTO",
  "locale": "ko-KR",
  "timezone": "Asia/Seoul"
}
```

| 필드 | 필수 | 설명 |
| --- | --- | --- |
| `requestId` | Y | 중복 요청 방지를 위한 서비스 요청 ID |
| `image.url` | Y | 내부 S3저장소의 이미지 참조 |
| `image.sha256` | Y | 중복 분석 캐시와 무결성 확인 |
| `inputHint` | Y | `AUTO`, `RECEIPT`, `PRODUCT` 중 하나. 기본은 `AUTO` |
| `locale` | Y | OCR 언어 및 날짜 형식 해석에 사용 |
| `timezone` | Y | 날짜 검증 기준 시간대 |

#### 응답

```json
{
  "analysisId": "ana_01JEXAMPLE",
  "status": "QUEUED",
  "submittedAt": "2026-08-31T10:15:00+09:00",
  "pollAfterMs": 1000
}
```

성공 시 HTTP `202 Accepted`를 반환합니다.

내부 단계는 `TYPE_QUEUED`, `CLASSIFYING`, `RECEIPT_QUEUED`, `PRODUCT_QUEUED`, `EXTRACTING`, `GEMMA_QUEUED`, `ENRICHING`, `VALIDATING`으로 추적합니다. PWA에는 내부 모델 이름을 노출하지 않고 완료 전 상태를 모두 `PROCESSING`으로 단순화해 보여줍니다. 각 단계 전환은 같은 `analysisId`를 사용하며 중복 메시지가 와도 결과를 두 번 생성하지 않습니다.

### 5.2 분석 결과 조회

`GET /ai/v1/analyses/{analysisId}`

#### 성공 예시: 실물 사진

```json
{
  "analysisId": "ana_01JEXAMPLE",
  "status": "COMPLETED",
  "documentType": {"value": "PRODUCT", "confidence": 0.98},
  "imageQuality": {"score": 0.91, "issues": []},
  "items": [
    {
      "itemId": "item_001",
      "displayStatus": "RECOGNIZED",
      "name": {
        "value": "서울우유 나100%",
        "normalizedValue": "우유",
        "source": "VISION",
        "confidence": 0.94
      },
      "category": {"value": "DAIRY", "confidence": 0.99},
      "quantity": {"value": 1, "unit": "PACK", "confidence": 0.88},
      "capacity": {
        "value": 1000,
        "unit": "ML",
        "scope": "PER_ITEM",
        "source": "OCR",
        "confidence": 0.96,
        "evidenceText": "1 L"
      },
      "expiration": {
        "date": "2026-09-07",
        "dateType": "USE_BY",
        "source": "OCR",
        "confidence": 0.93,
        "evidenceText": "소비기한 26.09.07"
      },
      "reviewReasons": []
    }
  ],
  "modelTrace": {
      "pipelineVersion": "image-analysis-1.0.0",
      "classifierVersion": "doc-classifier-0.3.0",
      "ocrVersion": "pp-ocrv5-korean-mobile-1.0.0",
      "normalizerVersion": "food-normalizer-0.3.0",
      "dictionaryVersion": "product-dictionary-0.2.0",
      "policyVersion": "confidence-policy-1.0.0",
      "externalFallbackUsed": false
  }
}
```

#### 성공 예시: 영수증과 AI 예상

```json
{
  "analysisId": "ana_02JEXAMPLE",
  "status": "COMPLETED",
  "documentType": {"value": "RECEIPT", "confidence": 0.97},
  "items": [
    {
      "itemId": "item_101",
      "displayStatus": "AI_ESTIMATED",
      "name": {
        "value": "친환경 애호박",
        "normalizedValue": "애호박",
        "source": "RECEIPT_LINE",
        "confidence": 0.92
      },
      "category": {"value": "VEGETABLE", "confidence": 0.98},
      "quantity": {"value": 1, "unit": "EA", "confidence": 0.76},
      "capacity": null,
      "expiration": {
        "date": null,
        "estimatedRange": {"from": "2026-09-03", "to": "2026-09-07"},
        "dateType": "ESTIMATED_CONSUMPTION_WINDOW",
        "source": "KNOWLEDGE_ESTIMATE",
        "confidence": 0.68,
        "assumptions": ["구매일 2026-08-31", "냉장 보관", "미절단 상태"],
        "evidenceIds": ["storage-guide-vegetable-014"]
      },
      "reviewReasons": ["표시된 소비기한이 없어 보관 조건을 기준으로 예상했습니다."]
    }
  ]
}
```

AI 예상은 단일 확정 날짜보다 기간으로 반환합니다. 사용자가 보관 방식과 개봉 여부를 확인하면 서비스 백엔드가 알림 기준일을 선택합니다.

## 6. 피드백 API 명세

`POST /ai/v1/analyses/{analysisId}/feedback`

```json
{
  "itemId": "item_101",
  "action": "CORRECTED",
  "confirmed": {
    "normalizedName": "애호박",
    "category": "VEGETABLE",
    "quantity": 2,
    "unit": "EA",
    "capacityValue": 500,
    "capacityUnit": "G",
    "capacityScope": "TOTAL",
    "expirationDate": "2026-09-05",
    "expirationSource": "USER_CONFIRMED"
  },
  "consentForModelImprovement": true
}
```

피드백은 원본 추론 결과를 덮어쓰지 않습니다. `모델 예측`, `사용자 확정값`, `수정 차이`를 별도로 보관해야 모델별 오류율을 계산하고 학습 데이터를 만들 수 있습니다.

## 7. 레시피 추천 API 명세

`POST /ai/v1/recipe-recommendations`

### 요청

```json
{
  "requestId": "req_recipe_001",
  "inventoryVersion": 42,
  "inventory": [
    {
      "ingredientId": "ing_tofu",
      "name": "두부",
      "quantity": 1,
      "unit": "PACK",
      "capacity": {"value": 300, "unit": "G", "scope": "PER_ITEM"},
      "expiresAt": "2026-09-01",
      "expirationSource": "OCR"
    },
    {
      "ingredientId": "ing_green_onion",
      "name": "대파",
      "quantity": 0.5,
      "unit": "EA",
      "capacity": null,
      "expiresAt": "2026-09-03",
      "expirationSource": "AI_ESTIMATED"
    }
  ],
  "preferences": {
    "allergens": ["PEANUT"],
    "dislikedIngredients": ["고수"],
    "preferredTags": ["한식", "매운맛 낮음"],
    "maxCookingMinutes": 30,
    "servings": 2
  },
  "limit": 5
}
```

### 응답

```json
{
  "recommendationId": "rec_01JEXAMPLE",
  "inventoryVersion": 42,
  "recommendations": [
    {
      "recipeId": "recipe_1024",
      "name": "두부 대파 조림",
      "score": 0.89,
      "scoreBreakdown": {
        "expiringIngredient": 0.35,
        "inventoryCoverage": 0.23,
        "preference": 0.18,
        "cookingTime": 0.08,
        "variety": 0.05
      },
      "usesInventory": ["두부", "대파"],
      "usesExpiringIngredients": ["두부"],
      "missingIngredients": ["진간장"],
      "allergenCheck": "PASSED",
      "reason": "내일 소비기한이 도래하는 두부를 모두 사용할 수 있고 추가 재료가 한 가지뿐입니다.",
      "source": {"type": "CURATED_RECIPE", "referenceId": "recipe_1024"}
    }
  ],
  "generatedAt": "2026-08-31T10:20:00+09:00"
}
```

초기 출시의 레시피 원문과 조리법은 MySQL의 검증된 레시피 자료에서 가져옵니다. 후보 검색, 알레르기 제외, 추천 순위와 추천 이유는 규칙과 템플릿으로 처리합니다. 자연어 의미 검색이나 생성 모델은 이 방식의 품질이 부족하다는 평가 결과가 있을 때 이후 버전에서 별도로 검증합니다.

## 8. 오류 처리

| HTTP | 오류 코드 | 발생 조건 | 서비스 대응 |
| --- | --- | --- | --- |
| `400` | `INVALID_REQUEST` | 필수 필드 누락, 잘못된 날짜·카테고리 | 요청 수정 후 재전송 |
| `401` | `UNAUTHORIZED_SERVICE` | 내부 서비스 인증 실패 | 호출 차단 및 보안 로그 기록 |
| `404` | `ANALYSIS_NOT_FOUND` | 존재하지 않는 분석 ID | 사용자에게 재요청 안내 |
| `413` | `IMAGE_TOO_LARGE` | 크기·해상도 제한 초과 | 클라이언트 압축 또는 재촬영 |
| `415` | `UNSUPPORTED_IMAGE_TYPE` | 지원하지 않는 파일 형식 | JPEG·PNG·WebP로 변환 |
| `422` | `UNUSABLE_IMAGE` | 이미지가 너무 흐리거나 대상이 없음 | `미인식` 또는 재촬영 안내 |
| `429` | `RATE_LIMITED` | 사용자·서비스 한도 초과 | 대기 후 재시도 |
| `503` | `MODEL_UNAVAILABLE` | 자체 모델과 외부 대체 모델 모두 실패 | 직접 입력으로 전환 |

모든 재시도 가능한 응답에는 `retryable`과 `retryAfterMs`를 포함합니다. 동일한 `requestId`는 중복 작업을 만들지 않는 멱등성을 보장합니다.

## 9. 인증과 개인정보

- AI API는 사설 네트워크 또는 서비스 간 인증을 통해서만 호출합니다.
- 이미지 참조 URL은 짧은 만료시간을 사용하고 분석 워커에 최소 권한만 부여합니다.
- 영수증의 카드번호, 전화번호, 주소, 멤버십 번호는 OCR 후 즉시 마스킹합니다.
- 로그에는 원본 이미지, 인증 토큰, 전체 OCR 문자열을 남기지 않습니다.
- 모델 개선용 이미지는 사용자 동의 여부와 보관기간을 별도 기록합니다.
- 모델 추적 정보는 남기되 외부 공급자의 내부 응답 전체를 그대로 저장하지 않습니다.

## 10. 검증 시나리오

| 시나리오 | 기대 결과 |
| --- | --- |
| 선명한 우유 패키지와 날짜 | `RECOGNIZED`, `DAIRY`, OCR 날짜와 근거 반환 |
| 날짜가 없는 애호박 실물 | 식별 성공 시 `AI_ESTIMATED`, 보관조건과 추정 기간 반환 |
| 흐린 냉장고 전체 사진 | `NEEDS_REVIEW`, 화질 문제와 재촬영 안내 |
| 음식과 무관한 풍경 사진 | `UNRECOGNIZED`, 빈 항목 목록 반환 |
| 영수증의 축약 상품명 | 표준 식재료 후보와 신뢰도 반환, 모호하면 `NEEDS_REVIEW` |
| `1kg` 두부 포장 | `capacity.value=1000`, `capacity.unit=G`로 정규화 |
| `1L` 우유 포장 | `capacity.value=1000`, `capacity.unit=ML`로 정규화 |
| `2개입 500g` 포장 | 수량 2개와 총용량 500g을 분리하고 `scope=TOTAL` 반환 |
| 숫자만 보이고 단위가 잘린 사진 | 용량을 추정하지 않고 `NEEDS_REVIEW` 반환 |
| 과거 5년 전 날짜로 인식 | 날짜 검증에서 차단하고 `NEEDS_REVIEW` |
| 알레르기 포함 레시피 | 추천 후보 생성 전에 제외 |
| 외부 AI API 장애 | 자체 OCR·분류 결과 사용 또는 직접 입력으로 전환 |
