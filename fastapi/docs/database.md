# PostgreSQL 스키마 (`receipt_ai`)

## 책임 범위

이 스키마는 현재 런타임에서 영수증 AI의 **보조 참조 데이터**만 관리한다. 백엔드 MySQL의 `ingredients`가
사용자 냉장고 재고의 원본이며, PostgreSQL의 `product_knowledge`는 재고나 재료 마스터가
아니다. 레시피 관련 테이블은 이번 범위에서 제외했다. 분석 큐와 진행 상태는 프로세스 메모리에 두고,
완료·실패한 분석 결과와 사용자 피드백, 피드백에서 승격한 학습 지식은 이 스키마에 저장한다 — 자세한
내용은 [architecture.md](./architecture.md).

`002_receipt_analysis_history.sql`의 `receipt_analyses`는 완료·실패한 분석 결과를 `final_payload`
(GET 응답과 같은 JSON)로 저장하는 데 사용한다. 같은 마이그레이션의 `receipt_analysis_items`와 이를
보완하는 `004`, `013`, `014`의 항목 제약은 현재 응답 규칙(예: `WEIGHT` 항목의 구매 수량)과 맞지 않아
**사용하지 않는다**. 피드백은 원본 항목을 `receipt_item_feedback.original_item`에 복사해 두므로 항목
테이블 없이도 비교·학습할 수 있다.

마이그레이션은 목적별로 분리되어 있다.

| 마이그레이션 | 테이블 | 역할 | 원본 여부 |
| --- | --- | --- | --- |
| `001_shelf_life_and_product_knowledge.sql` | `product_knowledge` | 상품 정규화·카테고리 분류 보조 자료 | 보조 자료 |
| | `product_aliases` | OCR 오인식·축약 상품명 별칭 | 보조 자료 |
| | `shelf_life_rules` | 출처가 검수된 관리용 소비기한 추정 규칙 | 보조 자료 |
| `002_receipt_analysis_history.sql` | `receipt_analyses` | 완료·실패 분석 결과를 저장해 인메모리 TTL 이후의 결과 조회와 피드백 원본 확인에 사용 | 분석 이력 원본 |
| | `receipt_analysis_items` | 과거 검토용 항목 이력 스키마. 현재 코드 미사용 | 미사용 |
| `003_product_catalog_seed.sql` | (INSERT만) | `product_knowledge` 초기 시드 31건 | 시드 데이터 |
| `004_catalog_fuzzy_source.sql` | (제약 변경만) | `receipt_analysis_items.category_source`에 `CATALOG_FUZZY` 허용 | 스키마 변경 |
| `005_product_catalog_seed_2.sql` | (INSERT만) | `product_knowledge` 2차 시드 3건(무/깻잎/브로콜리, "양파"는 003에 이미 있음) | 시드 데이터 |
| `006_product_catalog_seed_public.sql` | (INSERT만) | `product_knowledge` 대량 시드 238,800건(식품안전나라 공개 가공식품 DB) | 시드 데이터 |
| `007_deactivate_public_dataset_category_conflicts.sql` | (UPDATE만) | 006 시드 중 카테고리를 신뢰할 수 없는 763건 비활성화(`is_active=false`) | 데이터 정정 |
| `008_produce_catalog_seed.sql` | (INSERT만) | 흔한 생과일·생채소·곡류 정확 일치 큐레이션 46건(수박·귤·토마토·쌀 등) | 시드 데이터 |
| `009_product_aliases_seed.sql` | (INSERT만) | `product_aliases` 첫 시드 2건(브로커리→브로콜리, 챗잎→깻잎) | 시드 데이터 |
| `010_shelf_life_rules_mfds.sql` | (INSERT·UPDATE) | `shelf_life_rules`에 식약처 소비기한 참고값 23건 추가, 겹치는 USDA 5건 비활성화, 무(USDA) 1건 추가 | 시드 데이터 |
| `011_shelf_life_rules_milk.sql` | (INSERT·UPDATE) | 흰우유를 USDA 멸균우유 값에서 식약처 강화우유·유당분해우유 참고값(대리)으로 교체, 강화우유·유당분해우유 계열 5건 추가 | 시드 데이터 |
| `012_shelf_life_rules_fresh_only.sql` | `shelf_life_rules` 컬럼 추가 | `fresh_only` — 신선식품(채소·과일·육류·수산) 규칙은 상품이 그 분류일 때만 적용(감자깡에 "감자" 규칙이 붙던 문제) | 스키마 변경 |
| `013_product_analysis_contract.sql` | 분석 이력 제약 변경 | 과거 검토용 이력 테이블에 `PRODUCT`·`VISION` 출처 허용. 현재 코드 미사용 | 미사용 |
| `014_expiration_use_by_only.sql` | `receipt_analysis_items` 제약 변경 | 기존 `SELL_BY`를 `USE_BY`로 변환하고 날짜 용어를 소비기한으로 통일 | 스키마 변경 |
| `015_product_vision_catalog_seed.sql` | (INSERT만) | 다중 실물 상품 실험에서 확인한 상품명 8건과 반복 오인식 별칭 6건 | 시드 데이터 |
| `016_rename_categories_to_backend_codes.sql` | (제약·UPDATE) | 카테고리 코드를 백엔드 명세와 통일: `GRAIN_NOODLE→GRAINS_NOODLE`, `PROCESSED→PROCESSED_FOOD`, `ETC→OTHER`. 앱 배포 전에 적용 | 스키마·데이터 변경 |
| `017_shelf_life_rules_leafy_greens_broccoli.sql` | (INSERT만) | 깻잎·브로콜리 냉장 보관 참고 범위(USDA ARS Handbook 66) | 시드 데이터 |
| `018_feedback_learning.sql` | `receipt_item_feedback`, `learned_corrections` | 사용자 피드백과 피드백에서 승격한 학습 지식. 상품 카탈로그 조회 쿼리가 `learned_corrections`를 읽으므로 **앱 배포 전에 적용** | 스키마 변경 |

아래 본문의 실험 기록에 나오는 `PROCESSED` 등 이전 카테고리 코드는 016 적용 전 기록이다.
| `015_product_vision_catalog_seed.sql` | (INSERT만) | 실물 상품 실험에서 확인한 상품명과 OCR 별칭 8건 보강 | 시드 데이터 |

```mermaid
erDiagram
    RECEIPT_ANALYSES ||--o{ RECEIPT_ANALYSIS_ITEMS : contains
    PRODUCT_KNOWLEDGE ||--o{ PRODUCT_ALIASES : has
    PRODUCT_KNOWLEDGE ||--o{ SHELF_LIFE_RULES : optionally_has
```

## 저장하지 않는 데이터

- 이미지 바이너리: 객체 저장소에 두고 검증된 `object_key`와 SHA-256만 저장한다.
- 사용자 냉장고·재고: 백엔드 MySQL이 관리한다.
- 영수증 수량의 상품 기본값: 수량은 현재 영수증에서 매번 읽는다.
- 사용자·관리자 교정 피드백: 동의·보관 정책 승인 전까지는 테이블 자체를 만들지 않는다.
- 단계별 지연시간·토큰 관측: Phoenix/OpenTelemetry와 역할이 겹치므로 어느 쪽을 쓸지 정하기
  전까지는 테이블을 만들지 않는다.
- 작업 큐: 큐는 FastAPI 프로세스 메모리에 있고 DB 상태 행을 작업 선점 용도로 사용하지 않는다.

## 조회 순서

1. 바코드가 있으면 `product_knowledge.barcode`를 정확히 조회한다.
2. 정규화한 OCR 문자열로 `product_aliases.normalized_alias`를 정확히 조회한다.
3. 정확한 값이 없으면 `pg_trgm` 유사도 조회로 후보만 만든다.
4. 후보가 확정 기준을 넘지 못하면 해당 행만 Gemma에 전달한다.
5. 영수증에서 소비기한이 확인되지 않은 경우에만 검수된 `shelf_life_rules`를 조회한다.

유사도 검색 결과는 자동 확정값이 아니라 모델 입력 후보이다. 임계값은 평가 데이터로 정한 뒤
애플리케이션 설정에 둔다.

**현재 연동 상태**: 5번(소비기한 미확인 시 `shelf_life_rules` 조회)에 이어 **2번(별칭·표준명
정확 일치)**과 **3번(`pg_trgm` 유사도, 정확 일치 실패 시 폴백)**도 앱에 연결했다
([app/adapters/product_catalog.py](../app/adapters/product_catalog.py)의
`PostgresProductCatalogLookup`). `DATABASE_URL`이 비어 있으면 두 조회 모두 자동으로
비활성화된다.

- **1번(바코드)**: 파이프라인이 바코드를 별도 필드로 추출하지 않아 이번 범위에서 제외했다.
- **2번(별칭·표준명)**: 구현함. `product_aliases.normalized_alias` 정확 일치를
  `product_knowledge.normalized_name` 정확 일치보다 우선하고, 하나의 쿼리(`UNION ALL` +
  `ORDER BY priority`)로 처리한다.
- **3번(`pg_trgm` 유사도)**: 구현함, 단 정확 일치가 실패했을 때만 쓰는 폴백이다.
  `product_knowledge.normalized_name`에 대해서만 `%` 연산자와
  `settings.catalog_fuzzy_similarity_threshold`(기본 0.55)로 후보를 찾고 유사도 내림차순
  1건만 쓴다. 조회 결과도 애플리케이션에서 같은 임계값으로 다시 검증한다. `product_aliases`는 2건뿐이라
  유사도 대상에서 뺐다(거의 빈 테이블을 UNION해도 의미 없음) — 별칭이 더 쌓이면 그때
  포함한다. 매칭되면 `category.source = "CATALOG_FUZZY"`로 정확 일치(`CATALOG`)와
  구분되고, 신뢰도를 유사도 값으로 낮춘다. `reviewReasons`에는
  `field=CATEGORY`, `code=CATEGORY_FUZZY_MATCHED`, `messages=["유사한 상품명을 기준으로
  추정했습니다"]`를 추가한다 — 정확 일치와 같은 신뢰도로 자동 확정하지 않는다.
- **4번(Gemma에 카탈로그 근거 전달)**: 제외했다. 지금은 "카탈로그가 확정 → Gemma 건너뜀"
  또는 "카탈로그도 못 찾음 → 기존처럼 원문만 Gemma에" 두 가지뿐이고, 카탈로그 후보를
  Gemma 프롬프트에 힌트로 얹는 것은 하지 않는다.

파이프라인에서는 `known_food_category()`(정규식) → `product_catalog_lookup.find()`(DB,
정확 일치 → 유사도 폴백) → Gemma 순서로 확인한다. DB 카탈로그로 확정된 항목은
`category.source = "CATALOG"`(정확 일치) 또는 `"CATALOG_FUZZY"`(유사도 폴백)로 표시되어
정규식 매칭(`BUSINESS_RULE`)과 구분된다.

임계값은 초기 OCR 오차 사례를 기준으로 0.4를 사용했으나, 100장 회귀 시험에서 주소·깨진 OCR
문자열이 식품 카탈로그로 연결되는 오검출을 확인해 0.55로 높였다. 임계값 아래 항목은 삭제하는
대신 미해결 행으로 남겨 규칙 또는 Gemma가 판단하게 하며, 이후 홀드아웃 Precision과 Recall을
함께 측정해 재조정한다.

추정된 소비기한은 실제 포장 표시 날짜가 아니므로 `ExpirationValue.date`를 채우지 않는다.
규칙의 보관 가능 기간(`duration_min_days`~`duration_max_days`)을 확인된 구매일에 각각 더해
`estimatedRange`로 반환한다(예: 3~6일이면 구매일 +3일 ~ +6일). `dateType=ESTIMATED_CONSUMPTION_WINDOW`,
`source=KNOWLEDGE_ESTIMATE`로 표시돼 영수증에 찍힌 날짜(`USE_BY`, `source=OCR`)와 구분된다.
`displayStatus`는 이 경우 `AI_ESTIMATED`로 반환한다. 단, OCR 저신뢰도 같은 이미지
품질 문제가 있거나 유사 상품명으로만 카탈로그를 매칭한 경우에는 명세의 우선순위에 따라
`NEEDS_REVIEW`를 유지한다.

추정은 이미지에 소비기한이 찍혀 있지 않을 때만 한다. 영수증은 확인한 구매일을 기준으로 하며,
구매일이 없거나 믿을 수 없으면(미래 날짜, 1년 넘게 지난 날짜) 영수증 분석일을 임시 기준일로
쓰되, 신뢰도 0.35와 해당 가정을 응답에 명시한 참고 범위로만 반환한다. 실물 상품 사진도
구매일·제조일을 알 수 없으므로 같은 방식으로 분석일을 임시 기준일로 사용한다. 두 경우 모두
승인된 보관기간 규칙이 있는 상품에만 적용하며 `date`는 채우지 않는다. "1년"은 날짜 기준 365일이다.

## `shelf_life_rules` 시드 데이터

활성 규칙은 두 출처로 구성된다: 가공식품은 **식약처 식품유형별 소비기한 참고값**(010, 23건),
그 외 신선식품은 **USDA FSIS FoodKeeper**(24건, 011에서 우유 제외). 같은 이름이 두 출처에 동시에 살아 있지 않게,
국내 기준이 생긴 USDA 행(두부·햄·소시지·요거트·식빵)은 비활성화했다.

### 식약처 소비기한 참고값 (010)

2023-01-01부터 「식품 등의 표시·광고에 관한 법률」에 따라 가공식품에는 유통기한 대신 소비기한을
표시하고, 식약처가 식품유형별 참고값을 공개했다
([서울지방식약청 2023-01-19](https://www.mfds.go.kr/brd/m_827/view.do?seq=3616)).
원문 표의 10개 유형(가공유·과채주스·두부·발효유·빵류·소시지·어묵·유산균음료·프레스햄·햄)을
영수증에 나오는 일반명 23개로 펼쳐 넣었다(예: 가공유 → 맛우유·초코우유·바나나우유 등).

참고값은 **제조일부터** 일수인데 영수증에는 구매일만 있다. 그래서 원문의 두 숫자(평균 유통기한,
소비기한 참고값)로 구매일 이후 남은 기간을 범위로 잡았다.

- `duration_min_days` = 소비기한 − 유통기한 (판매 허용 기한 마지막 날에 산 경우)
- `duration_max_days` = 소비기한 (제조 당일 산 경우)
- 예: 두부(유통 17일, 소비 23일) → 관리용 예상 범위는 구매일 +6일 ~ +23일

**법정 기준값이 없는 품목**:
- 계란을 제외한 자연상태식품(양파·무 등 신선 농산물)은 소비기한 표시 대상이 아니다. 농촌진흥청
  농사로의 가정 보관법에도 일수는 없어 USDA 값을 쓴다(무 → USDA Radishes 10~14일, 적환무 기준).
- 맥주·장류·식초·절임류(장아찌)는 품질유지기한 표시 대상이라 추정하지 않는다.
- 냉장 흰우유(우유류)는 2031-01-01부터 소비기한이 적용돼 자체 참고값이 없다(보고서의 '우유'
  항목은 멸균·실온 제품 140일뿐). 그래서 011에서 원유·저지방우유를 살균해 냉장 유통하는 점이 같은
  강화우유(24일/기존 16일)·유당분해우유(24일/기존 16일) 참고값을 대리 적용했다 → 구매일 + 8일.
  출처명에 "대리 적용, 흰우유 자체 기준 없음"을 남겼다.

### USDA FoodKeeper

`shelf_life_rules`는 스키마만으로는 비어 있어 아무 추정도 하지 못한다.
[database/seed_shelf_life_seed.py](../database/seed_shelf_life_seed.py)가 실시간
웹검색/LLM이 아니라 **USDA FSIS FoodKeeper 공개 데이터**
([data.gov 카탈로그](https://catalog.data.gov/dataset/fsis-foodkeeper-data))에서 한국
영수증에 자주 등장하는 29개 식재료의 보관기간을 뽑아
`database/seed_data/shelf_life_rules_seed.sql`을 생성한다. 소비기한처럼 안전에 영향을 주는
값은 모델이 임의로 확정하지 않는다는 원칙에 따라 실시간 생성값이 아닌 정부 공개 자료만
사용했다.

- 원본 데이터: `database/seed_data/foodkeeper_ingredients_raw.csv` (GitHub 미러
  https://github.com/jelera/food-shelflife-db 에서 원본 그대로 내려받음, FSIS 공식 서버는
  자동화 요청을 차단함)
- 매핑 규칙: `app/domain/receipt_rules.py`의 `storage_for_category()`가 `MEAT`/`SEAFOOD`는
  항상 `FROZEN`, 그 외는 항상 `REFRIGERATED`로만 조회하므로, 각 식재료는 실제로 조회될
  `storage_type` 한 가지만 채웠다(반대쪽은 조회되지 않으므로 시드하지 않음).
- 전부 `package_state='UNOPENED'`, `reference_date_type='PURCHASE_DATE'`만 사용한다 —
  파이프라인이 아직 "개봉일"을 별도로 추적하지 않아 `Refrigerate_After_Opening` 계열 값은
  정확히 계산할 수 없으므로 제외했다.
- 재생성하려면: `python3 database/seed_shelf_life_seed.py` (raw CSV만 읽고 DB에는 아무것도
  쓰지 않음)
- 실제 적용: 생성된 `.sql`을 검토한 뒤 직접 실행한다.
  ```bash
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f database/seed_data/shelf_life_rules_seed.sql
  ```

**브랜드 무관 매칭**: `normalize_product_name()`은 "서울우유"처럼 브랜드명을 지우지 않으므로,
[app/adapters/shelf_life.py](../app/adapters/shelf_life.py)의 조회는 정확히 일치(`=`)가
아니라 **부분 문자열 포함**(`strpos(정규화이름, shelf_life_rules.normalized_ingredient_name) > 0`)
으로 매칭한다. 즉 시드에 넣은 일반명 "우유"가 "서울우유", "매일우유" 같은 정규화 결과 안에
포함되어 있으면 매칭된다. 여러 규칙이 동시에 포함 관계를 만족하면 더 긴(구체적인)
`normalized_ingredient_name`을 우선한다(예: "마늘빅프랑크"는 "마늘"보다 긴 "프랑크"가 걸려 소시지
기준을 쓴다). 1글자 이름("무", "햄")은 어디서나 포함되면 "단무지"·"햄버거"에 걸리므로 상품명
끝에 올 때만 매칭한다. `fresh_only=true`인 신선식품 규칙(양파·감자·사과·돼지고기 등, 012)은 상품이
VEGETABLE·FRUIT·MEAT·SEAFOOD로 분류됐을 때만 쓴다 — "감자깡"·"사과주스"에 신선식품 보관기간이 붙지 않게
하려는 것이다. 가공식품 기준(식약처 참고값)과 계란은 분류와 관계없이 쓴다.

이 방식은 `shelf_life_rules`처럼 **항목 수가 적고 이름이 짧은 일반명 테이블**에서만 적합하다.
항목이 많아지거나 이름이 다른 단어의 부분 문자열이 되는 경우가 늘면, `product_aliases`처럼
별도 별칭 테이블 + `pg_trgm` 유사도 조회로 옮겨야 한다. `product_knowledge`/`product_aliases`는
부분 문자열이 아니라 **정확 일치**만 쓰므로 이 오탐 위험이 없다.

## `product_knowledge` 시드 데이터

[database/migrations/003_product_catalog_seed.sql](../database/migrations/003_product_catalog_seed.sql)에
31건이 들어있다. 앞의 29건은 `shelf_life_rules`와 같은 명확한 식재료 일반명 목록을
재사용했고, 뒤의 2건("마늘빅프랑크", "예거라들러레몬")은 실제 GS25 영수증 벤치마크로 확인한
OCR 원문이다. 카테고리를 확신할 수 없었던 "마운틴클래식/마운틴틀러스트"는 추측해서 넣지
않고 뺐다. 전부 `verification_status='MODEL_INFERRED'`로 표시했다(사람이 검수 화면에서
확인한 적 없음 — `known_food_category()` 정규식 규칙과 동일한 신뢰 수준).

`product_aliases`는 실제로 반복 관찰된 OCR 오인식만 넣는다. 처음에는 관찰한 OCR 원문이 전부 표준명
자체와 같아서(예: "마늘빅프랑크") 비워뒀다가, 영수증2에서 "브로콜리"→"브로커리", "깻잎"→"챗잎" 오인식이
여러 배치에 걸쳐 반복 확인돼 [009](../database/migrations/009_product_aliases_seed.sql)로 이 2건을
등록했다. 별칭은 정확 일치 단계에서 표준명보다 먼저 조회되므로 유사도 폴백에 도달하기 전에 잡힌다
("브로커리"는 실존 가공식품 "브로커리팩"과 유사도 0.57로 매칭돼 PROCESSED로 나오던 문제가 해결됨).
근거 없이 별칭 변형을 지어내 넣지는 않는다.

[database/migrations/005_product_catalog_seed_2.sql](../database/migrations/005_product_catalog_seed_2.sql)에
2차로 3건("무", "깻잎", "브로콜리")을 더했다("양파"도 같은 원인으로 놓쳤지만 003에 이미
있었다). 영수증2.jpg를 실제로 돌려보니 Gemma가 이 4개(양파 포함)를 5개 미해결 줄 중 전부
(0/5) 놓쳤는데, 원인은 두 가지였다 — (1) 상품번호 뒤에 붙는 단일문자 코드("P"=포인트적립
등)가 상품명과 공백 없이 붙어 있어 Gemma·정규식 둘 다 못 알아봤다(코드 제거는 시드가 아니라
[app/domain/receipt_rules.py](../app/domain/receipt_rules.py)의 `_strip_product_type_code()`로
고쳤다), (2) 코드를 뗀 뒤에도 이 4개는 `known_food_category()` 정규식 목록에 아예 없던
일반 채소명이었다. "무"는 한 글자라 정규식 substring 매칭으로 넣으면 "무료"/"무이자" 같은
무관한 단어와 오탐할 위험이 커서 정확 일치만 쓰는 `product_knowledge`에 넣었고, 나머지
2건도 일관성을 위해 같이 넣었다. "브로콜리"는 OCR이 "브로커리"로 오인식했는데, 처음엔 `pg_trgm` 유사도 폴백이
실존 가공식품 "브로커리팩"에 잘못 매칭했고, 지금은 별칭(009)으로 정확 일치 단계에서 잡힌다.

### 대량 시드(공개 데이터셋)

003/005는 실제 영수증 테스트로 발견한 항목만 하나씩 손으로 넣은 것이지만,
[database/migrations/006_product_catalog_seed_public.sql](../database/migrations/006_product_catalog_seed_public.sql)은
**식품안전나라 K-FIND 가공식품 DB**(공개 데이터셋, 로그인 없이 간단한 활용정보 설문만
쓰면 받을 수 있음 — https://various.foodsafetykorea.go.kr/nutrient/ > 영양성분 DB
내려받기 > 가공식품 DB)에서 238,800건을 한 번에 넣었다.

- [database/seed_product_catalog_public.py](../database/seed_product_catalog_public.py)가
  원본 엑셀(31만여 행, 약 200MB — 용량 문제로 저장소엔 커밋하지 않는다)을 읽어
  `database/seed_data/product_catalog_public_seed.csv`(마찬가지로 51MB라 `.gitignore`
  대상)를 생성한다. `python-calamine`(Rust 기반 xlsx 파서)이 필요하다 — openpyxl은 이
  크기에서 감당이 안 될 만큼 느렸다.
- 식품대분류명(및 "절임류 또는 조림류"는 식품중분류명까지)으로 카테고리를 **한 갈래로
  확신할 수 있는 것만** 골랐다. "농산가공식품류"(견과류·밀가루·전분 등이 뒤섞여 있음),
  "조림류"(멸치조림/고기조림 등 재료가 다양함), "특수영양식품", "알가공품류" 같은 대분류
  (전체의 약 12.6%, 3.4만여 건)는 근거 없이 카테고리를 추측해 넣지 않는다는 원칙에 따라
  제외했다.
- 정규화명이 기존 003/005의 34건과 겹치는 항목(17건)은 `ON CONFLICT ... DO NOTHING`으로
  기존 행을 덮어쓰지 않고 건너뛴다.
- 적용 시 `\copy`는 psql 클라이언트가 실행하는 파일 시스템을 기준으로 하므로,
  `sudo -u postgres psql`처럼 다른 유저로 실행하면 그 유저가 CSV 파일을 읽을 권한이
  있어야 한다(예: EC2에서 `ec2-user` 홈 디렉터리는 700 권한이라 `postgres` 유저가 못
  들어간다 — `/tmp`처럼 다른 유저도 읽을 수 있는 경로에 같은 상대경로 구조로 복사한 뒤 그
  디렉터리에서 실행해야 한다).
- **알려진 트레이드오프**: 데이터가 많아질수록 `pg_trgm` 유사도 폴백이 의도하지 않은
  후보와 우연히 더 비슷하게 매칭될 위험이 커진다. 예를 들어 OCR 오타 "브로커리"(의도:
  "브로콜리")가 이 대량 데이터셋의 실제 상품 "브로커리팩"과 문자열이 더 비슷해(유사도
  0.57 vs 브로콜리와는 정확 불일치) 카테고리가 VEGETABLE 대신 PROCESSED로 잘못 매칭된
  사례를 실제로 확인했다. `category.source = "CATALOG_FUZZY"`로 구분 표시되고
  해당 항목의 `displayStatus`도 `NEEDS_REVIEW`라 최종 확정 전 사용자 확인을 거치므로 잘못된 값이
  그대로 굳어지지는 않지만, 데이터가 늘수록 이런 사례가 더 나올 수 있다는 점은 감안해야
  한다.

### 카테고리 정정(006 데이터 정제)

웹에서 가져온 실제 영수증 이미지 113장으로 대량 시드를 스트레스 테스트하다가, **정확
일치(`CATALOG`, fuzzy 아님)인데도 카테고리가 틀린 사례**를 발견했다 — "수박"이 PROCESSED로
매칭됐다.
[database/migrations/007_deactivate_public_dataset_category_conflicts.sql](../database/migrations/007_deactivate_public_dataset_category_conflicts.sql)이
이런 006 시드 763건을 `is_active=false`로 비활성화한다(삭제가 아님 — 원본 근거를 남기고
복구 가능하게 함). 추측이 아니라 원본 데이터에서 기계적으로 도출한 두 가지 근거만 썼다.

1. **정규화명 충돌(745건)**: 원본 엑셀 안에서 같은 정규화명이 서로 다른 대분류(카테고리)로
   동시에 등장하는 경우. 예: "갈비탕"은 MEAT 15건·PROCESSED 19건으로 원본 데이터 자체가
   합의를 못 한다. 006의 dedup 로직(`seen.setdefault()`)은 먼저 나온 행을 그냥
   채택했는데, 이는 사실상 엑셀 내 등장 순서로 정해진 것과 같다.
2. **흔한 생과일·생채소 원물명(96건, 위와 중복 제외)**: "수박"은 원본 데이터에서 카테고리가
   하나로만 나오지만(=위 745건에는 안 잡힘), 그 하나가 구조적으로 틀렸다. 식품안전나라
   "가공식품 DB"는 애초에 생과일·생채소를 다루지 않는 가공식품 전용 데이터셋이라, "수박"
   이라는 이름의 가공식품(수박맛 빙과류 등)이 우연히 하나 있으면 그게 실제 생수박의
   정규화명과 충돌한다. 흔한 생과일·채소 98개를 손으로 검증해 31개가 이 문제였다(2개는
   우연히 정상이라 제외 — 깐마늘, 죽순).

`database/seed_data/public_dataset_category_exclusions.csv`(831개 정규화명)가 두 목록의
합집합이다 — 로컬에 남아있던 원본 엑셀(31만 행)을 다시 읽어 생성했다. 검증하지 않은 이름도
안전하게 포함했다(DB에 없으면 그냥 no-op).

### 흔한 생과일·생채소 정확 일치 큐레이션(008)

"수박"처럼 짧은 생과일·생채소는 007에서 오답 행을 없앤 뒤 정확 일치 행이 없어, 유사도 폴백이
이름에 그 단어가 들어간 가공식품("퓨어 수박"=BEVERAGE, "성주 참외"=PROCESSED,
"복숭아 잼"=PROCESSED)을 끌어왔다. 유사도 폴백은 이제 **질의문이 3글자 이하이거나 카탈로그
이름이 2글자 이하이면 건너뛴다**([app/adapters/product_catalog.py](../app/adapters/product_catalog.py)) —
트라이그램이 3~4개뿐인 짧은 문자열은 유사도가 우연히 높게 나오기 쉽기 때문이다(결제단말기
브랜드 "키스-제크"가 2글자 상품 "제크"와 매칭돼 영수증 9장에서 반복 오탐이 난 사례). 정확 일치는
길이와 무관하게 그대로 동작한다.

다만 건너뛴 짧은 줄은 Gemma로 넘어가는데, 상품행 복원이 실패한 영수증은 가격 힌트가 없어 Gemma가
힌트 없는 짧은 이름을 제외한다(실측: 가격 힌트가 붙은 짧은 식재료 13개는 전부 정확히 분류,
힌트가 없으면 전부 제외). 그래서
[008](../database/migrations/008_produce_catalog_seed.sql)이 이름만으로 카테고리가 명백한
생과일 30개·생채소 14개·쌀·콩을 정확 일치 행으로 등록한다(`source_type='INTERNAL_CURATION'`,
`verification_status='MODEL_INFERRED'`). 이미 활성 행이 있는 이름은 제외하고
`ON CONFLICT DO NOTHING`으로 이중 방어했다. 견과·꿀처럼 앱 카테고리에 딱 맞는 게 없는 이름은
뺐다. 등록 후 "수박"이 `FRUIT`(`CATALOG`, 신뢰도 0.98)로 정확 일치됨을 확인했다.

## 캐싱과 동시성

[app/adapters/shelf_life.py](../app/adapters/shelf_life.py),
[app/adapters/product_catalog.py](../app/adapters/product_catalog.py) 둘 다
[app/adapters/_cache.py](../app/adapters/_cache.py)의 `LazyPool`(연결 pool을
`asyncio.Lock`으로 한 번만 생성), `BoundedAsyncCache`(조회 결과를 없음 포함 LRU로 캐싱,
`settings.reference_cache_max_size` 기본 2000건)를 공유해서 쓴다. 두 조회 다 DB 조회 실패
시 예외를 잡아 로그만 남기고 `None`을 반환하도록 `app/pipeline.py`에서 감싸져 있다 —
참조 테이블 조회 하나가 실패했다고 전체 분석이 `FAILED`로 떨어지지 않는다.


## 보관기간과 정리 작업

`receipt_analyses.purge_after`는 `trg_receipt_analyses_default_purge_after` 트리거가
INSERT 시 `submitted_at + 90일`로 자동 채운다. **90일은 팀이 확정한 분석 결과 보관기간
정책값**이다. 애플리케이션이 INSERT 시 `purge_after`를 직접 지정하면 트리거는 그 값을
덮어쓰지 않는다.

실제 삭제는 `receipt_ai.purge_expired_analyses()` 함수가 수행하며, `purge_after`가 지난
분석을 무조건 지운다(`receipt_analysis_items`는 CASCADE로 함께 정리됨). 이 함수는
마이그레이션에 정의만 되어 있고 스스로 실행되지 않으므로, `pg_cron` 확장이나 외부
스케줄러(cron + `psql`)로 별도 등록해야 한다:

```sql
SELECT cron.schedule('purge-receipt-analyses', '0 * * * *',
  $$SELECT receipt_ai.purge_expired_analyses()$$);
```

`pg_cron`을 쓸 수 없는 관리형 DB라면 EC2 쪽 cron이
`psql -c "SELECT receipt_ai.purge_expired_analyses();"`를 주기 실행하도록 구성한다.

현재 `storageType`은 API와 백엔드 ERD를 따라 `REFRIGERATED`, `FROZEN`만 허용한다.

마이그레이션 013은 실물 사진 분석 결과도 이력 테이블에 저장할 수 있도록 `document_type`에
`PRODUCT`를 추가하고, API enum에는 있지만 초기 이력 테이블 제약에서 빠졌던 `VISION`, `BARCODE`,
`CATALOG_FUZZY` 출처를 동기화한다.

## 키와 인덱스 원칙

- 모든 독립 테이블의 내부 PK는 `BIGINT GENERATED ALWAYS AS IDENTITY`를 사용한다.
- API에 노출되는 `analysis_id`, `item_id`는 내부 PK와 분리하고 `UNIQUE` 제약으로 멱등성과
  조회 계약을 보장한다.
- PostgreSQL은 FK 열에 인덱스를 자동 생성하지 않으므로 삭제·조인에 사용하는 FK 인덱스를
  명시한다.
- 상태 조회, 정규화 이름, 바코드·별칭, 검수 대기 목록처럼 이미 확정된 접근 경로만 우선
  인덱싱한다.
- 추측성 복합 인덱스는 추가하지 않고 운영 쿼리의 `EXPLAIN (ANALYZE, BUFFERS)` 결과를 보고
  보완한다.
