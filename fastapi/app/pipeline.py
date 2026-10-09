import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from difflib import SequenceMatcher
from time import perf_counter
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import Settings
from app.domain.models import (
    ClassifiedFoodLine,
    FoodCandidateClassifier,
    ImageLoader,
    InputTypeClassifier,
    OcrLine,
    PipelineDiagnostics,
    ProductCatalogLookup,
    ProductCatalogMatch,
    ReceiptProductRow,
    RemoteInferenceProvider,
    ShelfLifeLookup,
    TextExtractor,
)
from app.domain.receipt_rules import (
    build_food_candidate_lines,
    build_receipt_product_rows,
    build_supplemental_receipt_rows,
    correct_common_ocr_errors,
    is_non_food,
    is_receipt_metadata,
    known_food_category,
    normalize_product_name,
    parse_expiration,
    parse_measurement,
    parse_purchase_date,
    receipt_likelihood,
    sanitize_lines,
    storage_for_category,
)
from app.errors import ModelUnavailable
from app.schemas import (
    AnalysisResult,
    Category,
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

# receipt_likelihood()가 이 값보다 낮으면 AUTO 요청을 영수증이 아닌 것(OTHER)으로 끝낸다.
_RECEIPT_LIKELIHOOD_THRESHOLD = 0.45
# 점수가 낮아도 좌표로 "상품명 | 단가·수량·금액" 행이 이만큼 복원되면 영수증으로 인정한다.
# 위·아래가 잘린 영수증은 "합계"·"카드"·날짜가 화면 밖이라 점수가 가격 패턴 몫(최대 0.39)만
# 남는다(실측: 편의점 영수증 13행 복원인데 0.39로 OTHER, 과거 배치 img_044 마트 영수증도 6행·0.39).
# 과거 배치에서 OTHER로 판정된 79장에 행 복원을 돌리면 73장은 0행이었고, 비영수증 중 최대는
# 안내 페이지 2행·영수증 그림이 든 활동지 3행이라 4행을 기준으로 잡았다. 가격 패턴도 전혀 없는
# 이미지(점수 0.3 미만, 활동지 0.15)는 행 수와 관계없이 제외한다.
_STRUCTURAL_RECEIPT_MIN_ROWS = 4
_STRUCTURAL_RECEIPT_MIN_LIKELIHOOD = 0.3

# 신선식품 전용 소비기한 규칙(shelf_life_rules.fresh_only)을 적용해도 되는 상품 분류.
_FRESH_CATEGORIES = {Category.VEGETABLE, Category.FRUIT, Category.MEAT, Category.SEAFOOD}

# 배치 분류가 빈 결과일 때 한 줄씩 다시 물어볼 최대 줄 수(한 줄에 Gemma 호출 약 2~4초).
_MAX_SINGLE_LINE_RETRIES = 8

_CATEGORY_SOURCE_BY_REASON = {
    "keyword_rule": Source.BUSINESS_RULE,
    "catalog": Source.CATALOG,
    "catalog_fuzzy": Source.CATALOG_FUZZY,
}

_PRODUCT_NAME_SOURCE_BY_REASON = {
    "keyword_rule": Source.OCR,
    "catalog": Source.CATALOG,
    "catalog_fuzzy": Source.CATALOG_FUZZY,
    "vision_model": Source.VISION,
    "yolo_crop_vision_model": Source.VISION,
    "ocr_keyword_rule": Source.OCR,
}

_GENERIC_ENGLISH_PRODUCT_WORDS = {
    "beverage",
    "candy",
    "confectionery",
    "cookie",
    "cookies",
    "drink",
    "food",
    "korean",
    "milk",
    "noodle",
    "noodles",
    "product",
    "snack",
    "traditional",
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
        input_type_classifier: InputTypeClassifier | None = None,
        remote_inference_provider: RemoteInferenceProvider | None = None,
    ) -> None:
        self._settings = settings
        self._image_loader = image_loader
        self._text_extractor = text_extractor
        self._classifier = classifier
        self._shelf_life_lookup = shelf_life_lookup
        self._product_catalog_lookup = product_catalog_lookup
        self._input_type_classifier = input_type_classifier
        self._remote_inference_provider = remote_inference_provider

    # OCR과 Ollama 어댑터의 준비 상태를 점검해 이름별 boolean으로 반환한다.
    async def readiness(self) -> dict[str, bool]:
        if self._remote_inference_provider is not None:
            return {"remoteInference": await self._remote_inference_provider.ready()}
        readiness = {
            "ocr": await self._text_extractor.ready(),
            "ollama": await self._classifier.ready(),
        }
        if self._input_type_classifier is not None:
            readiness["inputClassifier"] = await self._input_type_classifier.ready()
        return readiness

    # 분류기·보관기간 조회기·상품 카탈로그 조회기가 close를 제공하면 자원을 비동기로 정리한다.
    async def close(self) -> None:
        for closeable in (
            self._classifier,
            self._shelf_life_lookup,
            self._product_catalog_lookup,
            self._input_type_classifier,
            self._remote_inference_provider,
        ):
            close = getattr(closeable, "close", None)
            if close is not None:
                await close()

    # 분석 레코드와 단계 콜백을 받아 OCR·분류·검증을 수행한 결과를 반환한다.
    async def run(self, record: AnalysisRecord, set_stage: Callable[[InternalStage], Awaitable[None]]) -> AnalysisResult:
        request = record.request
        started_at = perf_counter()
        log_context = "analysis_id=%s request_id=%s"
        logger.info("pipeline.started " + log_context, record.analysis_id, request.request_id)
        inferred_input_type: str | None = None
        inferred_input_confidence: float | None = None
        remote_model_candidates: tuple[ClassifiedFoodLine, ...] | None = None
        remote_diagnostics: PipelineDiagnostics | None = None

        if self._remote_inference_provider is not None:
            await set_stage(InternalStage.CLASSIFYING)
            step_started = perf_counter()
            logger.info("pipeline.remote_inference.started " + log_context, record.analysis_id, request.request_id)
            bundle = await self._remote_inference_provider.infer(
                request.image.object_key,
                request.image.sha256,
                request.input_hint.value,
            )
            document = bundle.document
            remote_model_candidates = bundle.model_candidates
            remote_diagnostics = bundle.diagnostics
            prediction = bundle.input_prediction
            if prediction is not None and request.input_hint == InputHint.AUTO:
                inferred_input_type = prediction.document_type if prediction.accepted else None
                inferred_input_confidence = prediction.confidence if prediction.accepted else None
            safe_lines = sanitize_lines(document.lines)
            logger.info(
                "pipeline.remote_inference.finished " + log_context + " line_count=%s model_candidate_count=%s elapsed_ms=%.2f "
                "worker_queue_wait_ms=%s download_ms=%s input_classifier_ms=%s ocr_queue_wait_ms=%s ocr_ms=%s "
                "llm_queue_wait_ms=%s llm_ms=%s worker_total_ms=%s input_classifier_active=%s",
                record.analysis_id,
                request.request_id,
                len(safe_lines),
                len(remote_model_candidates),
                (perf_counter() - step_started) * 1000,
                remote_diagnostics.worker_queue_wait_ms,
                remote_diagnostics.download_ms,
                remote_diagnostics.input_classifier_ms,
                remote_diagnostics.ocr_queue_wait_ms,
                remote_diagnostics.ocr_ms,
                remote_diagnostics.llm_queue_wait_ms,
                remote_diagnostics.llm_ms,
                remote_diagnostics.worker_total_ms,
                remote_diagnostics.input_classifier_active,
            )
        else:
            await set_stage(InternalStage.EXTRACTING)
            step_started = perf_counter()
            logger.info("pipeline.image_load.started " + log_context, record.analysis_id, request.request_id)
            image = await self._image_loader.load(request.image.object_key, request.image.sha256)
            logger.info(
                "pipeline.image_load.finished " + log_context + " media_type=%s width=%s height=%s elapsed_ms=%.2f",
                record.analysis_id,
                request.request_id,
                image.media_type,
                image.width,
                image.height,
                (perf_counter() - step_started) * 1000,
            )
            if request.input_hint == InputHint.AUTO and self._input_type_classifier is not None:
                await set_stage(InternalStage.CLASSIFYING)
                step_started = perf_counter()
                try:
                    prediction = await self._input_type_classifier.classify(image)
                    inferred_input_type = prediction.document_type if prediction.accepted else None
                    inferred_input_confidence = prediction.confidence if prediction.accepted else None
                    logger.info(
                        "pipeline.input_classification.finished " + log_context + " prediction=%s confidence=%.3f margin=%.3f accepted=%s elapsed_ms=%.2f",
                        record.analysis_id,
                        request.request_id,
                        prediction.document_type,
                        prediction.confidence,
                        prediction.margin,
                        prediction.accepted,
                        (perf_counter() - step_started) * 1000,
                    )
                except Exception:
                    logger.exception(
                        "pipeline.input_classification.failed " + log_context,
                        record.analysis_id,
                        request.request_id,
                    )
                await set_stage(InternalStage.EXTRACTING)
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

        if request.input_hint == InputHint.PRODUCT or inferred_input_type == "PRODUCT":
            return await self._run_product(
                record,
                safe_lines,
                quality,
                set_stage,
                started_at,
                inferred_input_confidence,
                remote_model_candidates,
            )
        if inferred_input_type == "OTHER":
            return self._empty_result(1 - (inferred_input_confidence or 0.0), quality)

        receipt_rows: list[ReceiptProductRow] | None = None
        if request.input_hint == InputHint.AUTO and inferred_input_type is None and document_confidence < _RECEIPT_LIKELIHOOD_THRESHOLD:
            receipt_rows = build_receipt_product_rows(safe_lines, document.width)
            if len(receipt_rows) >= _STRUCTURAL_RECEIPT_MIN_ROWS and document_confidence >= _STRUCTURAL_RECEIPT_MIN_LIKELIHOOD:
                logger.info(
                    "pipeline.receipt_by_rows " + log_context + " receipt_confidence=%.3f row_count=%s",
                    record.analysis_id,
                    request.request_id,
                    document_confidence,
                    len(receipt_rows),
                )
                # 점수는 기준 미달이었지만 구조로 인정했다는 뜻으로 기준값을 신뢰도로 쓴다.
                document_confidence = _RECEIPT_LIKELIHOOD_THRESHOLD
            else:
                logger.info(
                    "pipeline.non_receipt " + log_context + " row_count=%s elapsed_ms=%.2f",
                    record.analysis_id,
                    request.request_id,
                    len(receipt_rows),
                    (perf_counter() - started_at) * 1000,
                )
                return self._empty_result(document_confidence, quality)

        await set_stage(InternalStage.ENRICHING)
        step_started = perf_counter()
        logger.info("pipeline.classification.started " + log_context, record.analysis_id, request.request_id)
        if receipt_rows is None:
            receipt_rows = build_receipt_product_rows(safe_lines, document.width)
        rows_by_line_number = {row.product_line.line_no: row for row in receipt_rows}
        classification_lines = [row.product_line for row in receipt_rows] or build_food_candidate_lines(safe_lines)
        if receipt_rows and any(row.product_line.box for row in receipt_rows):
            # OCR이 상품명과 가격을 한 줄로 합치면 좌표 열 복원은 그 행을 놓칠 수 있다.
            # 이미 알고 있는 식품 키워드이고 쉼표 가격이 있거나 보수적인 강한 상품 키워드인 줄만 보충해,
            # 결제 안내문을 상품으로 늘리지 않으면서 누락된 상품 행을 복구한다.
            reconstructed_line_numbers = set(rows_by_line_number)
            supplemental_lines = [
                line
                for line in build_food_candidate_lines(safe_lines)
                if line.line_no not in reconstructed_line_numbers
                and known_food_category(line.text) is not None
                and (
                    re.search(r"(?<!\d)\d{1,3}(?:,\d{3})+(?!\d)", line.text)
                    or re.search(r"미원|와플|브라우니|케익|케이크", line.text, re.I)
                )
            ]
            classification_lines.extend(supplemental_lines)
            supplemental_rows = build_supplemental_receipt_rows(
                safe_lines,
                document.width,
                supplemental_lines,
                len(receipt_rows) + 1,
            )
            receipt_rows.extend(supplemental_rows)
            rows_by_line_number.update({row.product_line.line_no: row for row in supplemental_rows})
        # 생활용품(티슈·화장지·봉투 등)은 분류 전에 뺀다 — Gemma가 가공식품으로 포함시키는 경우가 있었다.
        non_food_count = sum(is_non_food(line.text) or is_receipt_metadata(line.text) for line in classification_lines)
        classification_lines = [line for line in classification_lines if not is_non_food(line.text) and not is_receipt_metadata(line.text)]
        known_selected: list[ClassifiedFoodLine] = []
        unresolved_lines: list[OcrLine] = []
        for line in classification_lines:
            rule_category = known_food_category(line.text)
            catalog_match = await self._lookup_catalog_category(line.text) if self._product_catalog_lookup is not None else None
            # 정확 매칭은 카탈로그를 우선하되, 유사 매칭은 명확한 키워드 규칙을 덮지 않는다.
            use_catalog = catalog_match is not None and (catalog_match.matched_via != "fuzzy" or rule_category is None)
            resolved = catalog_match.category if use_catalog else rule_category
            normalized_name = catalog_match.canonical_name if use_catalog else correct_common_ocr_errors(line.text)
            reason = "keyword_rule"
            confidence = min(line.confidence, 0.98)
            if use_catalog:
                if catalog_match.matched_via == "fuzzy":
                    reason = "catalog_fuzzy"
                    # 유사도 매칭은 정확 일치보다 신뢰도를 낮춰 review에서 구분되게 한다.
                    confidence = min(confidence, catalog_match.similarity or 0.6)
                else:
                    reason = "catalog"
            if resolved is None:
                unresolved_lines.append(self._with_price_hint(line, rows_by_line_number.get(line.line_no)))
            else:
                known_selected.append(
                    ClassifiedFoodLine(
                        line_no=line.line_no,
                        normalized_name=normalized_name,
                        category=resolved,
                        confidence=confidence,
                        reason=reason,
                    )
                )
        logger.info(
            "pipeline.rows.reconstructed " + log_context + " row_count=%s classification_line_count=%s non_food_count=%s known_food_count=%s unresolved_count=%s",
            record.analysis_id,
            request.request_id,
            len(receipt_rows),
            len(classification_lines),
            non_food_count,
            len(known_selected),
            len(unresolved_lines),
        )
        unresolved_line_numbers = {line.line_no for line in unresolved_lines}
        if remote_model_candidates is not None:
            model_selected = [item for item in remote_model_candidates if item.line_no in unresolved_line_numbers]
            diagnostics = remote_diagnostics or PipelineDiagnostics()
        else:
            model_selected, diagnostics = await self._classifier.classify(unresolved_lines)
            model_selected = [item for item in model_selected if item.line_no in unresolved_line_numbers]
        hinted_lines = [line for line in unresolved_lines if line.line_no in rows_by_line_number and line.text.endswith("원)")]
        if remote_model_candidates is None and not model_selected and hinted_lines:
            # Gemma는 여러 줄을 한 번에 받을 때 그중 한 줄이 애매하면 전부 버리고 {"items":[]}를 돌려주는
            # 경우가 있다(실측: "소 척아이롤 (수량 1개, 12,900원)"만 넣으면 MEAT로 고르는데, 깨진 줄
            # "종이 특100원 갑 계"와 같이 넣으면 빈 결과). 좌표로 가격까지 확인된 상품행만 한 줄씩 다시 묻는다.
            # 호출이 늘어나는 걸 막으려고 배치 결과가 완전히 비었을 때만, 최대 _MAX_SINGLE_LINE_RETRIES줄까지만 한다.
            for line in hinted_lines[:_MAX_SINGLE_LINE_RETRIES]:
                retried, _ = await self._classifier.classify([line])
                model_selected.extend(item for item in retried if item.line_no == line.line_no)
            logger.info(
                "pipeline.classification.single_line_retry " + log_context + " retried=%s selected=%s",
                record.analysis_id,
                request.request_id,
                min(len(hinted_lines), _MAX_SINGLE_LINE_RETRIES),
                len(model_selected),
            )
        selected = sorted([*known_selected, *model_selected], key=lambda item: item.line_no)
        fallback_used = False
        if remote_model_candidates is None and not selected and receipt_rows:
            # receipt_rows가 비어 있으면 classification_lines가 이미 build_food_candidate_lines
            # 결과이므로 여기서 다시 만들어도 완전히 같은 입력으로 Gemma를 중복 호출하게 된다.
            # 좌표 기반 복원이 성공했는데 아무것도 못 골랐을 때만, 아직 시도 안 한 단순 후보로 재시도한다.
            fallback_lines = [line for line in build_food_candidate_lines(safe_lines) if not is_non_food(line.text) and not is_receipt_metadata(line.text)]
            if fallback_lines and len(fallback_lines) < len(safe_lines):
                logger.info(
                    "pipeline.classification.fallback analysis_id=%s request_id=%s candidate_line_count=%s",
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
        logger.info("pipeline.validation.started " + log_context, record.analysis_id, request.request_id)
        lines_by_number = {line.line_no: line for line in classification_lines}
        today = _local_today(request.timezone)
        purchase_date = parse_purchase_date(safe_lines, today)
        estimation_basis = None
        estimate_confidence = 0.5
        if purchase_date is None:
            purchase_date = today
            estimation_basis = f"영수증 구매일을 확인할 수 없어 분석일 {today.isoformat()}을 임시 기준일로 사용함"
            estimate_confidence = 0.35
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
                estimation_basis=estimation_basis,
                estimate_confidence=estimate_confidence,
                image_needs_review=bool(quality.issues),
            )
            if item is not None:
                items.append(item)

        result = AnalysisResult(
            documentType=DocumentTypeResult(
                value="RECEIPT",
                confidence=max(
                    document_confidence,
                    inferred_input_confidence or 0,
                    0.75 if request.input_hint == InputHint.RECEIPT else 0,
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

    # 실물 이미지의 OCR 줄을 상품 사전·명확한 식품 규칙으로만 판별해 검토용 재고 후보를 반환한다.
    # 영수증용 Gemma 프롬프트는 실물 패키지에 일반화됐다는 근거가 없어 이 경로에서 호출하지 않는다.
    async def _run_product(
        self,
        record: AnalysisRecord,
        safe_lines: list[OcrLine],
        quality: ImageQuality,
        set_stage: Callable[[InternalStage], Awaitable[None]],
        started_at: float,
        input_confidence: float | None = None,
        remote_model_candidates: tuple[ClassifiedFoodLine, ...] | None = None,
    ) -> AnalysisResult:
        await set_stage(InternalStage.ENRICHING)
        step_started = perf_counter()
        request = record.request
        # 비전 Worker가 후보를 돌려준 실물 사진은 OCR 조각을 독립 상품으로 다시 해석하지 않는다.
        # 포장 전면의 브랜드·맛·용량 문구가 각각 상품으로 중복되는 문제를 막는다.
        candidates = [] if remote_model_candidates else [line for line in build_food_candidate_lines(safe_lines) if not is_non_food(line.text)]
        selected: list[ClassifiedFoodLine] = []
        seen_names: set[str] = set()
        vision_line_numbers = {candidate.line_no for candidate in remote_model_candidates or () if candidate.reason in {"vision_model", "yolo_crop_vision_model"}}

        for line in candidates:
            # Worker가 이미지 자체에서 만든 합성 줄을 OCR·카탈로그 결과로 다시 해석하면 출처가
            # CATALOG로 바뀐다. 비전 후보는 아래에서 그대로 추가해 실제 판별 출처를 보존한다.
            if line.line_no in vision_line_numbers:
                continue
            rule_category = known_food_category(line.text)
            catalog_match = await self._lookup_catalog_category(line.text) if self._product_catalog_lookup is not None else None
            use_catalog = catalog_match is not None and (catalog_match.matched_via != "fuzzy" or rule_category is None)
            category = catalog_match.category if use_catalog else rule_category
            normalized_name = catalog_match.canonical_name if use_catalog else normalize_product_name(correct_common_ocr_errors(line.text))
            reason = "keyword_rule"
            confidence = min(line.confidence, 0.9)

            if use_catalog:
                reason = "catalog_fuzzy" if catalog_match.matched_via == "fuzzy" else "catalog"
                confidence = min(
                    line.confidence,
                    catalog_match.similarity or (0.98 if reason == "catalog" else 0.6),
                )

            deduplication_key = normalize_product_name(normalized_name)
            if category is None or not deduplication_key or deduplication_key in seen_names:
                continue
            seen_names.add(deduplication_key)
            selected.append(
                ClassifiedFoodLine(
                    line_no=line.line_no,
                    normalized_name=normalized_name,
                    category=category,
                    confidence=confidence,
                    reason=reason,
                )
            )

        # 원격 GPU Worker가 실물 이미지를 직접 본 비전 후보를 추가한다. OCR·카탈로그로 이미
        # 고른 상품은 우선 유지하고, 글자가 없거나 낯선 포장 상품만 vision_model 후보로 보완한다.
        if remote_model_candidates:
            for candidate in _prefer_korean_product_candidates(remote_model_candidates):
                catalog_match = await self._lookup_catalog_category(candidate.normalized_name) if self._product_catalog_lookup is not None else None
                if catalog_match is not None:
                    candidate = ClassifiedFoodLine(
                        line_no=candidate.line_no,
                        normalized_name=catalog_match.canonical_name,
                        category=catalog_match.category,
                        confidence=min(candidate.confidence, catalog_match.similarity or 0.98),
                        reason="catalog_fuzzy" if catalog_match.matched_via == "fuzzy" else "catalog",
                    )
                deduplication_key = normalize_product_name(candidate.normalized_name)
                if not deduplication_key or any(_similar_product_names(deduplication_key, seen_name) for seen_name in seen_names):
                    continue
                if candidate.line_no not in {line.line_no for line in safe_lines}:
                    continue
                seen_names.add(deduplication_key)
                selected.append(candidate)

        logger.info(
            "pipeline.product.classification.finished analysis_id=%s request_id=%s candidate_count=%s selected=%s elapsed_ms=%.2f",
            record.analysis_id,
            request.request_id,
            len(candidates),
            len(selected),
            (perf_counter() - step_started) * 1000,
        )

        await set_stage(InternalStage.VALIDATING)
        lines_by_number = {line.line_no: line for line in candidates}
        lines_by_number.update({line.line_no: line for line in safe_lines})
        # 단일 상품일 때만 이미지 전체의 날짜 줄을 그 상품에 연결한다. 여러 상품이 잡힌 사진에서는
        # 어느 날짜가 어느 상품 것인지 알 수 없으므로 공통 날짜를 모든 항목에 복제하지 않는다.
        expiration_text = " ".join(line.text for line in safe_lines) if len(selected) == 1 else None
        measurement_text = expiration_text
        analysis_date = _local_today(request.timezone)
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
                purchase_date=analysis_date,
                expiration_text=expiration_text,
                measurement_text=measurement_text,
                document_type="PRODUCT",
                estimation_basis=(f"실물 사진 분석일 {analysis_date.isoformat()}을 임시 기준일로 사용함(구매일·제조일 미확인)"),
                estimate_confidence=0.35,
                # 비전 모델이 상품을 직접 식별했다면 낮은 OCR 신뢰도는 상품 판정과 무관하다.
                # 흐림·노출 등 나머지 품질 문제는 기존처럼 사용자 확인 대상으로 유지한다.
                image_needs_review=bool(quality.issues if selected_line.reason not in {"vision_model", "yolo_crop_vision_model"} else [issue for issue in quality.issues if issue != "LOW_OCR_CONFIDENCE"]),
            )
            if item is not None:
                items.append(item)

        result = AnalysisResult(
            documentType=DocumentTypeResult(
                value="PRODUCT",
                confidence=input_confidence if input_confidence is not None else 0.75,
            ),
            imageQuality=quality,
            items=items,
            modelTrace=self._trace(),
        )
        logger.info(
            "pipeline.product.finished analysis_id=%s request_id=%s item_count=%s elapsed_ms=%.2f",
            record.analysis_id,
            request.request_id,
            len(items),
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
        expiration_text: str | None = None,
        measurement_text: str | None = None,
        document_type: str = "RECEIPT",
        estimation_basis: str | None = None,
        estimate_confidence: float = 0.5,
        image_needs_review: bool = False,
    ) -> ReceiptItem | None:
        normalized = normalize_product_name(selected_line.normalized_name)
        if not normalized:
            return None
        measure_type, quantity, weight_value, weight_unit, measure_evidence = parse_measurement(
            correct_common_ocr_errors(measurement_text or original_text),
            product_row.quantity if product_row else None,
        )
        expiration_date, expiration_evidence = parse_expiration(expiration_text or original_text, datetime.now(UTC).date())
        storage_type = storage_for_category(selected_line.category.value)
        reasons = ["현재 실물 분석 결과는 사용자 확인이 필요합니다." if document_type == "PRODUCT" else "현재 영수증 모델은 사용자 확인이 필요합니다."]
        expiration_extra: dict = {}
        expiration_was_estimated = False
        if expiration_date is None:
            reasons.append("실물 이미지에서 확정 가능한 소비기한을 찾지 못했습니다." if document_type == "PRODUCT" else "영수증에서 확정 가능한 소비기한을 찾지 못했습니다.")
            expiration_extra = await self._estimate_expiration(
                normalized,
                storage_type,
                purchase_date,
                selected_line.category,
                estimation_basis,
                estimate_confidence,
            )
            if expiration_extra:
                expiration_was_estimated = True
                reasons.append(f"표시된 소비기한이 없어 보관 조건을 기준으로 예상했습니다. 출처: {expiration_extra.pop('source_name')}")
                if document_type == "PRODUCT":
                    reasons.append("구매일·제조일을 확인할 수 없어 분석일 기준의 참고 범위이며 확정 소비기한이 아닙니다.")
                elif estimation_basis:
                    reasons.append("구매일을 확인할 수 없어 분석일 기준의 참고 범위이며 확정 소비기한이 아닙니다.")
        if measure_evidence is None:
            reasons.append("수량 표기가 없어 1개를 기본 후보로 제안했습니다.")
        if selected_line.reason == "catalog_fuzzy":
            reasons.append("카테고리는 정확히 일치하지 않고 유사한 상품명을 기준으로 추정했습니다.")

        # 승인된 보관기간 규칙으로 날짜 범위만 추정한 경우는 API 명세대로 AI_ESTIMATED다.
        # 단, 이미지 품질 문제나 퍼지 카탈로그 매칭은 더 높은 검토 우선순위를 가져 NEEDS_REVIEW를 유지한다.
        display_status = DisplayStatus.AI_ESTIMATED if expiration_was_estimated else DisplayStatus.NEEDS_REVIEW
        if image_needs_review or selected_line.reason == "catalog_fuzzy":
            display_status = DisplayStatus.NEEDS_REVIEW

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
            quantity_value = (
                QuantityValue(
                    value=product_row.quantity,
                    source=Source.OCR,
                    confidence=ocr_confidence,
                )
                if product_row is not None and product_row.quantity is not None
                else None
            )
            weight = WeightValue(
                value=weight_value,
                unit=weight_unit,
                source=Source.OCR,
                confidence=ocr_confidence,
                evidenceText=measure_evidence,
            )

        # 영수증은 모델·카탈로그가 고른 이름으로 OCR 상품명을 덮어쓰지 않는다(위키 2단계: Gemma가
        # "불향차돌떡볶이"를 "떡볶이"로 줄이는 문제). 표시값은 행번호·가격·용량만 뗀 OCR 상품명이고,
        # 모델·카탈로그 표준명은 normalizedValue로만 전달한다. 실물 사진은 비전 모델이 이미지에서
        # 직접 읽은 이름이 OCR 조각보다 정확해 기존처럼 정규화 이름을 표시한다.
        display_name = normalized
        if document_type == "RECEIPT":
            display_name = normalize_product_name(correct_common_ocr_errors(original_text).lstrip("*#≠ ")) or normalized

        return ReceiptItem(
            itemId=f"item_{index:03d}",
            displayStatus=display_status,
            name=SourcedString(
                value=display_name[:100],
                normalizedValue=normalized[:100],
                source=(_PRODUCT_NAME_SOURCE_BY_REASON.get(selected_line.reason, Source.OCR) if document_type == "PRODUCT" else Source.RECEIPT_LINE),
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
                date=expiration_extra.get("date", expiration_date),
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
    # 영수증은 확인된 구매일, 실물 사진은 명시한 분석일 가정을 기준으로 보관기간 예상 범위를 반환한다.
    # 지식 규칙은 실제 포장 표시가 아니므로 date는 비워 두고 estimatedRange와 가정만 제공한다.
    async def _estimate_expiration(
        self,
        normalized_name: str,
        storage_type: str,
        purchase_date: date | None,
        category: Category | None = None,
        estimation_basis: str | None = None,
        estimate_confidence: float = 0.5,
    ) -> dict:
        if self._shelf_life_lookup is None or purchase_date is None:
            return {}
        try:
            fresh_item = category is None or category in _FRESH_CATEGORIES
            estimate = await self._shelf_life_lookup.find(normalized_name, storage_type, fresh_item)
        except Exception:
            logger.exception("shelf_life_lookup.failed normalized_name=%s storage_type=%s", normalized_name, storage_type)
            return {}
        if estimate is None:
            return {}
        estimated_from = purchase_date + timedelta(days=estimate.duration_min_days)
        estimated_to = purchase_date + timedelta(days=estimate.duration_max_days)
        return {
            "dateType": "ESTIMATED_CONSUMPTION_WINDOW",
            "source": Source.KNOWLEDGE_ESTIMATE,
            "confidence": estimate_confidence,
            "estimatedRange": EstimatedDateRange(from_date=estimated_from, to_date=estimated_to),
            "assumptions": [
                estimation_basis or f"영수증에서 확인한 구매일 {purchase_date.isoformat()}",
                f"보관 가능 기간 {estimate.duration_min_days}~{estimate.duration_max_days}일 적용",
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
            return await self._product_catalog_lookup.find(normalize_product_name(correct_common_ocr_errors(line_text)))
        except Exception:
            logger.exception("product_catalog_lookup.failed line_text=%s", line_text)
            return None

    # 좌표 기반으로 복원된 수량·금액이 있으면 Gemma 입력 줄 끝에 힌트로 붙여 반환한다.
    # "낯선 상표명이라도 가격이 붙어 있으면 구매한 상품"이라는 신호를 준다 — GS25처럼
    # 사전에 없는 브랜드명(예: "마운틴틀러스트")을 Gemma가 그냥은 못 알아보지만, 실제 가격이
    # 붙어 있는 줄이라는 걸 알려주면 인식한다(직접 검증). 좌표로 금액이 확인된 행에만 붙여서,
    # 후보 텍스트뿐인 receipt_rows 미복원 상황(build_food_candidate_lines 폴백)에서는 원문 그대로 둔다.
    @staticmethod
    def _with_price_hint(line: OcrLine, row: ReceiptProductRow | None) -> OcrLine:
        if row is None or row.amount is None:
            return line
        # 줄 앞의 "*"(면세)·"#"(영세) 표시는 Gemma가 상품명으로 읽지 못하게 만든다(실측: "*정육"은 제외,
        # "정육"은 MEAT). 표시 기호는 Gemma에 보내는 문자열에서만 뗀다.
        name = line.text.lstrip("*#≠ ")
        # 수량 "1"은 OCR이 자주 놓친다. 금액이 확인됐으면 수량 없이도 힌트를 붙인다.
        detail = f"수량 {row.quantity}개, {row.amount:,}원" if row.quantity is not None else f"{row.amount:,}원"
        return OcrLine(line_no=line.line_no, text=f"{name} ({detail})", confidence=line.confidence, box=line.box)

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
            classifierVersion=self._settings.input_classifier_version,
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


# 공백·기호가 제거된 두 상품명이 철자 한두 글자만 다른 동일 후보인지 반환한다.
def _similar_product_names(left: str, right: str) -> bool:
    if left == right:
        return True
    if min(len(left), len(right)) < 4:
        return False
    return SequenceMatcher(None, left.casefold(), right.casefold()).ratio() >= 0.8


# 한 포장의 한글 상품명과 영문 설명 문구를 모델이 별도 상품으로 반환했을 때 한글 상품명만 남긴다.
def _prefer_korean_product_candidates(candidates: tuple[ClassifiedFoodLine, ...]) -> tuple[ClassifiedFoodLine, ...]:
    korean_categories = {candidate.category for candidate in candidates if re.search(r"[가-힣]", candidate.normalized_name)}
    preferred = tuple(candidate for candidate in candidates if not (candidate.category in korean_categories and _is_generic_english_product_label(candidate.normalized_name)))
    result: list[ClassifiedFoodLine] = []
    for candidate in sorted(preferred, key=lambda item: len(normalize_product_name(item.normalized_name)), reverse=True):
        key = normalize_product_name(candidate.normalized_name)
        if any(candidate.category == kept.category and key in normalize_product_name(kept.normalized_name) for kept in result):
            continue
        result.append(candidate)
    return tuple(result)


# 영문만으로 된 일반 식품 유형 설명인지 확인해 동일 포장의 번역·부제 중복 제거에 사용한다.
def _is_generic_english_product_label(value: str) -> bool:
    if re.search(r"[가-힣]", value):
        return False
    words = re.findall(r"[A-Za-z]+", value.casefold())
    return len(words) >= 2 and all(word in _GENERIC_ENGLISH_PRODUCT_WORDS for word in words)


# 요청 시간대 기준 오늘 날짜를 반환한다. 알 수 없는 시간대면 UTC 날짜로 대신한다.
def _local_today(timezone: str) -> date:
    try:
        return datetime.now(ZoneInfo(timezone)).date()
    except (ZoneInfoNotFoundError, ValueError):
        return datetime.now(UTC).date()
