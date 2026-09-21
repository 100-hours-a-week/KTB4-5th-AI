# 오조사마의 AI Wiki
---

# 다먹자 영수증 분석 AI

영수증 이미지를 PP-OCRv5로 읽고, DB 상품 카탈로그와 로컬 Ollama `gemma4:e2b`로 실제 식품
상품행만 골라 백엔드 ERD 형식의 재고 후보를 반환하는 FastAPI 서비스다.

**현재 구현 범위는 영수증 분석뿐이다.** 실물 식재료 이미지 분석과 레시피 추천은 포함하지
않는다. 원래 설계 배경은
[AI 설계 Wiki](https://github.com/100-hours-a-week/KTB4-5th-wiki/wiki/AI-Wiki)를 참고하되,
그 문서와 실제 구현이 갈리는 부분은 이 저장소의 `docs/`가 우선한다.

## 무엇을 하는가

1. 백엔드가 이미지 `objectKey`·`sha256`과 함께 분석을 요청한다(`POST /ai/v1/analyses`).
2. OCR로 텍스트를 읽고, 영수증인지 아닌지 판별한다.
3. 좌표를 보고 상품명·수량·금액을 행 단위로 복원한다.
4. 각 상품행을 정규식 → DB 카탈로그(정확 일치·유사도) → Gemma 순서로 카테고리를 확정한다.
5. 수량·용량을 표준화하고(고체는 g, 액체는 ml), 소비기한을 추정하며, 모든 결과를
   `NEEDS_REVIEW`로 반환한다 — 품질이 아직 승인되지 않았으므로 사용자 확인 없이 재고에
   자동 반영하지 않는다.

## 문서

- **[docs/architecture.md](docs/architecture.md)** — 요청 흐름, 파이프라인 단계, 큐·컨슈머
  동시성, OCR 메모리 관리, 데이터 계약(enum), 프로젝트 구조
- **[docs/database.md](docs/database.md)** — PostgreSQL 스키마, 마이그레이션, 시드 데이터
  출처와 근거, 조회 순서
- **[docs/deployment.md](docs/deployment.md)** — 로컬 설치, 환경변수, Docker, 로깅

## 빠른 시작

```bash
cp .env.example .env
uv sync --extra dev --extra ocr
ollama pull gemma4:e2b
mkdir -p var/uploads
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

```bash
uv run ruff check .
uv run pytest -q
```

## 설계 원칙

- AI의 추정과 이미지에서 직접 확인한 사실을 구분한다.
- 소비기한과 알레르기처럼 안전에 영향을 주는 값은 모델이 임의로 확정하지 않는다.
- 카테고리·소비기한·별칭은 근거(정규식 규칙, DB 정확 일치, 검수된 공개 데이터) 없이
  추측해서 채우지 않는다 — 애매하면 채우지 않고 `NEEDS_REVIEW`로 남긴다.
- 수량과 용량을 구분하며, 용량을 확인할 수 없는 경우 임의로 생성하지 않는다.
- 모델이 실패해도 사용자는 직접 입력으로 냉장고 재고를 등록할 수 있어야 한다.
