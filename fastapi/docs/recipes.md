# 레시피 추천/동기화 구조

현행 정책은 recipe-rerank-v15, Worker 계약은 recipe-grounded-v6다. 확정 재고와 취향을 비동기로 받아 임박 재료 활용을 우선하는 최대 10개 레시피와 재고 ID별 사용량을 반환한다. AI 입력 예산에 맞춰 평가 창을 조정하며 원문은 보존한다. 취향 적합 주장은 실제 원문 인용으로 검증한다.

## 구성

| 모듈 | 역할 |
| --- | --- |
| routers/recipes.py | 인증된 202 접수/작업 조회/레시피 동기화 |
| recipes/jobs.py | 전용 인메모리 큐, 제한 소비자, 멱등 접수, 작업 상태 |
| recipes/job_repository.py | 완료/실패 결과 보관과 재조회 |
| recipes/job_callback.py | 레시피 전용 URL로 최종 결과 전달 |
| recipes/schemas.py | 재고, 취향, 원문, 사용량과 작업 계약 |
| recipes/context.py | 서버 계절/식사 시간대와 요청별 다양성 seed |
| recipes/service.py | DB 조회, 보충 검색, 분량과 취향 판단 조립 |
| recipes/planning.py | AI 순열 검증, 임박 재료 대표 후보 선택 |
| recipes/quantities.py | 같은 재료/단위의 소비기한순 ID별 배분 |
| recipes/retrieval.py / stock_names.py | 재료 별칭, 상품명 연결과 제외 조건 |
| recipes/food_safety.py / web_provider.py | 승인 원천 수집/파싱/재검증 |
| recipes/repository.py | 원천 PostgreSQL 읽기/저장 및 변경 cursor |

설계는 [추천 정책](recipe_ai_recommendations.md), 백엔드 요청/응답은 [비동기 API](recipe_async_api.md), 원천 확보는 [원천 검증](recipe_sources.md)을 따른다.

## 데이터 경계

백엔드는 재고와 사용자 설문을 소유한다. AI는 요청에 담긴 확정 재고와 태그만 사용한다. 냉장고별 재고 소유권은 백엔드가 확인한다. AI가 실제 재고를 추가하거나 차감하지 않는다.

AI PostgreSQL recipe_ai.recipes는 출처/원문 스냅샷, recipe_ai.changes는 append-only 동기화 이력이다. sourceRecipeId는 원천 내 식별자이며 SOURCE:sourceRecipeId가 전체 식별자다. 날짜가 다른 동일 재료는 재고 ID별로 유지한다. recipe_ai.recommendation_results는 비동기 작업 완료/실패 결과만 저장하고 큐는 저장하지 않는다.

백엔드는 GET /ai/v1/recipe-sync/changes의 불투명 cursor와 변경 페이지를 읽어 자체 MySQL에 반영한다. 페이지 전체 적용 후 cursor를 저장한다. sourceRevision/sourceUrl/ingredients/steps와 INACTIVE 상태를 보존한다. 이 동기화 API는 변경하지 않는다.

## 배포와 설정

1. 기존 020/021 이후 022_recipe_recommendation_results.sql을 AI PostgreSQL에 적용한다. 앱 시작 시 DDL은 실행하지 않는다.
   관리자 계정으로 생성했다면 앱 DB 계정에 새 테이블의 SELECT/INSERT/UPDATE와 recommendation_results_id_seq의 USAGE 권한을 별도로 부여한다. 기존 레시피 스키마의 접근 권한도 유지한다.
2. FastAPI와 RunPod의 v5 레시피 계약을 함께 배포한다.
3. RECIPE_CALLBACK_URL과 RECIPE_CALLBACK_API_KEY를 백엔드 레시피 수신 경로에 맞춰 설정한다. 영수증 CALLBACK_URL을 재사용하지 않는다.
4. 기본 큐 크기는 100, 소비자 2, 작업 처리 상한 120초, 메모리 결과 TTL 3600초다.
5. 웹 보충은 TAVILY_API_KEY가 있으면 도메인 제한 없이 검색한다. RECIPE_TAVILY_APPROVED_DOMAINS는 원천 DB 재사용 등록에만 적용한다. 미승인 원천도 실제 재료/조리 구획 검증을 통과하면 이번 추천에 포함할 수 있지만 원천 DB에는 등록하지 않는다. 작업 결과 스냅샷에는 해당 추천이 남는다.

상세한 장애/재시작 보장은 추천 정책의 운영 절을 따른다. 원문 확인 없이 수량 환산, 조리법 생성, 미검증 원천 자동 저장, 모델 가중치 학습은 수행하지 않는다.
