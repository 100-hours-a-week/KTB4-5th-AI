import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from time import perf_counter

from app.config import Settings
from app.domain.models import (
    ClassifiedFoodLine,
    FoodCandidateClassifier,
    ImageLoader,
    OcrLine,
    PipelineDiagnostics,
    ProductCatalogLookup,
    ProductCatalogMatch,
    ReceiptProductRow,
    ShelfLifeLookup,
    TextExtractor,
)
from app.domain.receipt_rules import (
    build_food_candidate_lines,
    build_receipt_product_rows,
    known_food_category,
    normalize_product_name,
    parse_expiration,
    parse_measurement,
    parse_purchase_date,
    receipt_likelihood,
    sanitize_lines,
    storage_for_category,
)
from app.errors import InvalidRequest, ModelUnavailable
from app.schemas import (
    AnalysisResult,
    CategoryValue,
    DisplayStatus,
    DocumentTypeResult,
    EstimatedDateRange,
    ExpirationValue,
    ImageQuality,
    InputHint,
    InternalStage,
    ModelTrace,
    QuantityValue,
    ReceiptItem,
    Source,
    SourcedString,
    WeightValue,
)
from app.store import AnalysisRecord

logger = logging.getLogger(__name__)

_CATEGORY_SOURCE_BY_REASON = {
    "keyword_rule": Source.BUSINESS_RULE,
    "catalog": Source.CATALOG,
    "catalog_fuzzy": Source.CATALOG_FUZZY,
}


class ReceiptAnalysisPipeline:
    # 설정과 이미지·OCR·식품행 분류·보관기간 조회 포트를 받아 영수증 파이프라인을 구성한다.
    def __init__(
        self,
        settings: Settings,
        image_loader: ImageLoader,
        text_extractor: TextExtractor,
        classifier: FoodCandidateClassifier,
        shelf_life_lookup: ShelfLifeLookup | None = None,
        product_catalog_lookup: ProductCatalogLookup | None = None,
    ) -> None:
        self._settings = settings
        self._image_loader = image_loader
        self._text_extractor = text_extractor
        self._classifier = classifier
        self._shelf_life_lookup = shelf_life_lookup
        self._product_catalog_lookup = product_catalog_lookup

    # OCR과 Ollama 어댑터의 준비 상태를 점검해 이름별 boolean으로 반환한다.
    async def readiness(self) -> dict[str, bool]:
        return {
            "ocr": await self._text_extractor.ready(),
            "ollama": await self._classifier.ready(),
        }

    # 분류기·보관기간 조회기·상품 카탈로그 조회기가 close를 제공하면 자원을 비동기로 정리한다.
    async def close(self) -> None:
        for closeable in (self._classifier, self._shelf_life_lookup, self._product_catalog_lookup):
            close = getattr(closeable, "close", None)
            if close is not None:
                await close()

    # 분석 레코드와 단계 콜백을 받아 OCR·분류·검증을 수행한 결과를 반환한다.
    async def run(self, record: AnalysisRecord, set_stage: Callable[[InternalStage], Awaitable[None]]) -> AnalysisResult:
        request = record.request
        started_at = perf_counter()
        log_context = "analysis_id=%s request_id=%s"
        logger.info("pipeline.started " + log_context, record.analysis_id, request.request_id)
        if request.input_hint == InputHint.PRODUCT:
            raise InvalidRequest("현재 구현은 영수증 분석만 지원합니다.")

        await set_stage(InternalStage.EXTRACTING)
        step_started = perf_counter()
        logger.info(
            "pipeline.image_load.started " + log_context, record.analysis_id, request.request_id
        )
        image = await self._image_loader.load(
            request.image.object_key,
            request.image.sha256,
        )
        logger.info(
            "pipeline.image_load.finished "
            + log_context
            + " media_type=%s width=%s height=%s elapsed_ms=%.2f",
            record.analysis_id,
            request.request_id,
            image.media_type,
            image.width,
            image.height,
            (perf_counter() - step_started) * 1000,
        )
        step_started = perf_counter()
        logger.info("pipeline.ocr.started " + log_context, record.analysis_id, request.request_id)
        document = await self._text_extractor.extract(image)
        safe_lines = sanitize_lines(document.lines)
        logger.info(
            "pipeline.ocr.finished " + log_context + " line_count=%s elapsed_ms=%.2f",
            record.analysis_id,
            request.request_id,
            len(safe_lines),
            (perf_counter() - step_started) * 1000,
        )
        document_confidence = receipt_likelihood(safe_lines)
        quality = self._image_quality(safe_lines)
        logger.info(
            "pipeline.document_checked " + log_context + " receipt_confidence=%.3f quality=%.3f",
            record.analysis_id,
            request.request_id,
            document_confidence,
            quality.score,
        )

        if request.input_hint == InputHint.AUTO and document_confidence < 0.45:
            logger.info(
                "pipeline.non_receipt " + log_context + " elapsed_ms=%.2f",
                record.analysis_id,
                request.request_id,
                (perf_counter() - started_at) * 1000,
            )
            return self._empty_result(document_confidence, quality)

        await set_stage(InternalStage.ENRICHING)
        step_started = perf_counter()
        logger.info(
            "pipeline.classification.started " + log_context, record.analysis_id, request.request_id
        )
        receipt_rows = build_receipt_product_rows(safe_lines, document.width)
        rows_by_line_number = {row.product_line.line_no: row for row in receipt_rows}
        classification_lines = [row.product_line for row in receipt_rows] or build_food_candidate_lines(safe_lines)
        known_selected: list[ClassifiedFoodLine] = []
        unresolved_lines: list[OcrLine] = []
        for line in classification_lines:
            resolved = known_food_category(line.text)
            reason = "keyword_rule"
            confidence = min(line.confidence, 0.98)
            if resolved is None and self._product_catalog_lookup is not None:
                catalog_match = await self._lookup_catalog_category(line.text)
                if catalog_match is not None:
                    resolved = catalog_match.category
                    if catalog_match.matched_via == "fuzzy":
                        reason = "catalog_fuzzy"
                        # 유사도 매칭은 정확 일치보다 신뢰도를 낮춰 review에서 구분되게 한다.
                        confidence = min(confidence, catalog_match.similarity or 0.6)
                    else:
                        reason = "catalog"
            if resolved is None:
                unresolved_lines.append(self._with_price_hint(line, rows_by_line_number.get(line.line_no)))
            else:
                known_selected.append(ClassifiedFoodLine(line_no=line.line_no, normalized_name=line.text, category=resolved, confidence=confidence, reason=reason))
        logger.info(
            "pipeline.rows.reconstructed " + log_context + " row_count=%s classification_line_count=%s known_food_count=%s unresolved_count=%s",
            record.analysis_id,
            request.request_id,
            len(receipt_rows),
            len(classification_lines),
            len(known_selected),
            len(unresolved_lines),
        )
        model_selected, diagnostics = await self._classifier.classify(unresolved_lines)
        unresolved_line_numbers = {line.line_no for line in unresolved_lines}
        model_selected = [item for item in model_selected if item.line_no in unresolved_line_numbers]
        selected = sorted([*known_selected, *model_selected], key=lambda item: item.line_no)
        fallback_used = False
        if not selected and receipt_rows:
            # receipt_rows가 비어 있으면 classification_lines가 이미 build_food_candidate_lines
            # 결과이므로 여기서 다시 만들어도 완전히 같은 입력으로 Gemma를 중복 호출하게 된다.
            # 좌표 기반 복원이 성공했는데 아무것도 못 골랐을 때만, 아직 시도 안 한 단순 후보로 재시도한다.
            fallback_lines = build_food_candidate_lines(safe_lines)
            if fallback_lines and len(fallback_lines) < len(safe_lines):
                logger.info(
                    "pipeline.classification.fallback analysis_id=%s request_id=%s "
                    "candidate_line_count=%s",
                    record.analysis_id,
                    request.request_id,
                    len(fallback_lines),
                )
                selected, fallback_diagnostics = await self._classifier.classify(fallback_lines)
                fallback_used = True
                diagnostics.llm_prompt_tokens = fallback_diagnostics.llm_prompt_tokens
                diagnostics.llm_completion_tokens = fallback_diagnostics.llm_completion_tokens
                diagnostics.llm_prompt_duration_ns = fallback_diagnostics.llm_prompt_duration_ns
                diagnostics.llm_eval_duration_ns = fallback_diagnostics.llm_eval_duration_ns
                diagnostics.llm_load_duration_ns = fallback_diagnostics.llm_load_duration_ns
        logger.info(
            "pipeline.classification.finished analysis_id=%s request_id=%s ocr_lines=%s "
            "selected=%s fallback_used=%s elapsed_ms=%.2f prompt_tokens=%s "
            "completion_tokens=%s prompt_duration_ns=%s eval_duration_ns=%s load_duration_ns=%s",
            record.analysis_id,
            request.request_id,
            len(safe_lines),
            len(selected),
            fallback_used,
            (perf_counter() - step_started) * 1000,
            diagnostics.llm_prompt_tokens,
            diagnostics.llm_completion_tokens,
            diagnostics.llm_prompt_duration_ns,
            diagnostics.llm_eval_duration_ns,
            diagnostics.llm_load_duration_ns,
        )

        await set_stage(InternalStage.VALIDATING)
        step_started = perf_counter()
        logger.info(
            "pipeline.validation.started " + log_context, record.analysis_id, request.request_id
        )
        lines_by_number = {line.line_no: line for line in classification_lines}
        purchase_date = parse_purchase_date(safe_lines, datetime.now(UTC).date())
        items: list[ReceiptItem] = []
        for index, selected_line in enumerate(selected, start=1):
            original = lines_by_number.get(selected_line.line_no)
            if original is None:
                continue
            item = await self._to_item(
                index,
                original.text,
                original.confidence,
                selected_line,
                rows_by_line_number.get(selected_line.line_no),
                purchase_date,
            )
            if item is not None:
                items.append(item)

        result = AnalysisResult(
            documentType=DocumentTypeResult(
                value="RECEIPT",
                confidence=max(
                    document_confidence, 0.75 if request.input_hint == InputHint.RECEIPT else 0
                ),
            ),
            imageQuality=quality,
            items=items,
            modelTrace=self._trace(),
        )
        logger.info(
            "pipeline.validation.finished " + log_context + " item_count=%s elapsed_ms=%.2f",
            record.analysis_id,
            request.request_id,
            len(items),
            (perf_counter() - step_started) * 1000,
        )
        logger.info(
            "pipeline.finished " + log_context + " elapsed_ms=%.2f",
            record.analysis_id,
            request.request_id,
            (perf_counter() - started_at) * 1000,
        )
        return result

    # 선택된 OCR 상품행과 신뢰도를 받아 ERD 호환 재고 후보 또는 None을 반환한다.
    async def _to_item(
        self,
        index: int,
        original_text: str,
        ocr_confidence: float,
        selected_line: ClassifiedFoodLine,
        product_row: ReceiptProductRow | None = None,
        purchase_date: date | None = None,
    ) -> ReceiptItem | None:
        normalized = normalize_product_name(selected_line.normalized_name)
        if not normalized:
            return None
        measure_type, quantity, weight_value, weight_unit, measure_evidence = parse_measurement(original_text, product_row.quantity if product_row else None)
        expiration_date, expiration_evidence = parse_expiration(
            original_text, datetime.now(UTC).date()
        )
        storage_type = storage_for_category(selected_line.category.value)
        reasons = ["현재 영수증 모델은 사용자 확인이 필요합니다."]
        expiration_extra: dict = {}
        if expiration_date is None:
            reasons.append("영수증에서 확정 가능한 소비기한을 찾지 못했습니다.")
            expiration_extra = await self._estimate_expiration(normalized, storage_type, purchase_date)
            if expiration_extra:
                reasons.append(
                    f"표시된 소비기한이 없어 보관 조건을 기준으로 예상했습니다. 출처: {expiration_extra.pop('source_name')}"
                )
        if measure_evidence is None:
            reasons.append("수량 표기가 없어 1개를 기본 후보로 제안했습니다.")
        if selected_line.reason == "catalog_fuzzy":
            reasons.append("카테고리는 정확히 일치하지 않고 유사한 상품명을 기준으로 추정했습니다.")

        if measure_type == "COUNT":
            quantity_value = QuantityValue(
                value=quantity or 1,
                source=Source.OCR if measure_evidence else Source.BUSINESS_RULE,
                confidence=ocr_confidence if measure_evidence else 0.5,
            )
            weight = WeightValue(
                value=None,
                unit="NONE",
                source=Source.BUSINESS_RULE,
                confidence=1.0,
                evidenceText=measure_evidence,
            )
        else:
            quantity_value = None
            weight = WeightValue(
                value=weight_value,
                unit=weight_unit,
                source=Source.OCR,
                confidence=ocr_confidence,
                evidenceText=measure_evidence,
            )

        return ReceiptItem(
            itemId=f"item_{index:03d}",
            displayStatus=DisplayStatus.NEEDS_REVIEW,
            name=SourcedString(
                value=original_text[:100],
                normalizedValue=normalized[:100],
                source=Source.RECEIPT_LINE,
                confidence=min(ocr_confidence, selected_line.confidence),
                evidenceText=original_text,
            ),
            category=CategoryValue(
                value=selected_line.category,
                source=_CATEGORY_SOURCE_BY_REASON.get(selected_line.reason, Source.MODEL_INFERENCE),
                confidence=selected_line.confidence,
            ),
            storageType=storage_type,
            measureType=measure_type,
            quantity=quantity_value,
            weight=weight,
            expiration=ExpirationValue(
                date=expiration_date,
                dateType=expiration_extra.get("dateType", "USE_BY" if expiration_date else "UNKNOWN"),
                source=expiration_extra.get("source", Source.OCR if expiration_date else None),
                confidence=expiration_extra.get("confidence", ocr_confidence if expiration_date else None),
                evidenceText=expiration_evidence,
                estimatedRange=expiration_extra.get("estimatedRange"),
                assumptions=expiration_extra.get("assumptions", []),
                evidenceIds=expiration_extra.get("evidenceIds", []),
            ),
            reviewReasons=reasons,
        )

    # 정규화 이름과 보관 방식으로 검수된 보관기간 규칙을 조회해 예상 소비기한 필드 초안을 반환한다.
    async def _estimate_expiration(
        self, normalized_name: str, storage_type: str, purchase_date: date | None
    ) -> dict:
        if self._shelf_life_lookup is None or purchase_date is None:
            return {}
        try:
            estimate = await self._shelf_life_lookup.find(normalized_name, storage_type)
        except Exception:
            logger.exception("shelf_life_lookup.failed normalized_name=%s storage_type=%s", normalized_name, storage_type)
            return {}
        if estimate is None:
            return {}
        return {
            "dateType": "ESTIMATED_CONSUMPTION_WINDOW",
            "source": Source.KNOWLEDGE_ESTIMATE,
            "confidence": 0.5,
            "estimatedRange": EstimatedDateRange(
                from_date=purchase_date + timedelta(days=estimate.duration_min_days),
                to_date=purchase_date + timedelta(days=estimate.duration_max_days),
            ),
            "assumptions": [
                f"구매일 {purchase_date.isoformat()}",
                f"보관 방식: {storage_type}",
                "포장 개봉 여부 확인 안 됨",
            ],
            "evidenceIds": [f"shelf-life-rule-{estimate.rule_id}"],
            "source_name": estimate.source_name,
        }

    # OCR 상품행 텍스트로 상품 카탈로그를 조회해 일치하는 항목 또는 None을 반환한다.
    # 조회가 실패해도 전체 분석을 실패시키지 않고 Gemma로 넘기도록 None을 반환한다.
    async def _lookup_catalog_category(self, line_text: str) -> ProductCatalogMatch | None:
        try:
            return await self._product_catalog_lookup.find(normalize_product_name(line_text))
        except Exception:
            logger.exception("product_catalog_lookup.failed line_text=%s", line_text)
            return None

    # 좌표 기반으로 복원된 수량·금액이 있으면 Gemma 입력 줄 끝에 힌트로 붙여 반환한다.
    # "낯선 상표명이라도 가격이 붙어 있으면 구매한 상품"이라는 신호를 준다 — GS25처럼
    # 사전에 없는 브랜드명(예: "마운틴틀러스트")을 Gemma가 그냥은 못 알아보지만, 실제 가격이
    # 붙어 있는 줄이라는 걸 알려주면 인식한다(직접 검증). 수량·금액이 둘 다 있을 때만 붙여서,
    # 후보 텍스트뿐인 receipt_rows 미복원 상황(build_food_candidate_lines 폴백)에서는 원문 그대로 둔다.
    @staticmethod
    def _with_price_hint(line: OcrLine, row: ReceiptProductRow | None) -> OcrLine:
        if row is None or row.quantity is None or row.amount is None:
            return line
        hint = f"{line.text} (수량 {row.quantity}개, {row.amount:,}원)"
        return OcrLine(line_no=line.line_no, text=hint, confidence=line.confidence, box=line.box)

    # 영수증 신뢰도와 화질을 받아 OTHER 문서의 빈 분석 결과를 반환한다.
    def _empty_result(self, document_confidence: float, quality: ImageQuality) -> AnalysisResult:
        return AnalysisResult(
            documentType=DocumentTypeResult(value="OTHER", confidence=1 - document_confidence),
            imageQuality=quality,
            items=[],
            modelTrace=self._trace(),
        )

    # 현재 설정의 파이프라인·OCR·LLM·정규화·정책 버전 정보를 반환한다.
    def _trace(self) -> ModelTrace:
        return ModelTrace(
            pipelineVersion=self._settings.pipeline_version,
            ocrVersion=self._settings.ocr_version,
            llmModel=self._settings.ollama_model,
            normalizerVersion=self._settings.normalizer_version,
            policyVersion=self._settings.policy_version,
        )

    @staticmethod
    # 마스킹된 OCR 줄을 받아 평균 신뢰도 기반 이미지 품질 요약을 반환한다.
    def _image_quality(lines: list[OcrLine]) -> ImageQuality:
        if not lines:
            return ImageQuality(score=0.0, issues=["NO_TEXT"])
        average = sum(line.confidence for line in lines) / len(lines)
        issues = [] if average >= 0.7 else ["LOW_OCR_CONFIDENCE"]
        return ImageQuality(score=max(0.0, min(average, 1.0)), issues=issues)


class UnavailableClassifier:
    # 사용 불가능한 대체 분류기이므로 항상 False를 반환한다.
    async def ready(self) -> bool:
        return False

    # 입력 OCR 줄과 무관하게 MODEL_UNAVAILABLE 예외를 발생시킨다.
    async def classify(self, lines: list[OcrLine]) -> tuple[list[ClassifiedFoodLine], PipelineDiagnostics]:
        raise ModelUnavailable("Ollama 분류기를 사용할 수 없습니다.")
