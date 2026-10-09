import asyncio
import importlib.util
import io
import logging
from pathlib import Path
from time import perf_counter

from PIL import Image, ImageOps

from app.adapters.input_classifier import OnnxMobileNetInputClassifier
from app.adapters.ocr import PaddleTextExtractor
from app.adapters.ollama import OllamaFoodClassifier
from app.adapters.product_detector import YoloESegProductDetector, YoloWorldProductDetector
from app.config import Settings
from app.domain.models import ClassifiedFoodLine, LoadedImage, OcrLine, PipelineDiagnostics, ReceiptProductRow
from app.domain.receipt_rules import (
    build_food_candidate_lines,
    build_receipt_product_rows,
    is_non_food,
    known_food_category,
    sanitize_lines,
)
from app.schemas import (
    InputHint,
    RemoteInferenceRequest,
    RemoteInferenceResponse,
    RemoteInputPrediction,
    RemoteModelCandidate,
    RemoteOcrLine,
)
from gpu_worker.image_downloader import SecureImageDownloader

logger = logging.getLogger(__name__)


class GpuInferenceEngine:
    # 설정으로 이미지 다운로드·MobileNetV3·PP-OCR·Gemma 어댑터를 조립한다.
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._inference_semaphore = asyncio.Semaphore(settings.worker_max_concurrency)
        self._ocr_semaphore = asyncio.Semaphore(settings.worker_ocr_max_concurrency)
        self._llm_semaphore = asyncio.Semaphore(settings.worker_llm_max_concurrency)
        self._downloader = SecureImageDownloader(settings)
        self._ocr = PaddleTextExtractor(settings)
        classifier_enabled = settings.input_classifier_backend == "onnx" or (settings.input_classifier_backend == "auto" and Path(settings.input_classifier_model_path).is_file())
        self._input_classifier = OnnxMobileNetInputClassifier(settings) if classifier_enabled else None
        self._food_classifier = OllamaFoodClassifier(settings) if settings.worker_enable_llm else None
        detector_enabled = settings.worker_product_detector_backend == "yolo_world" or (
            settings.worker_product_detector_backend == "auto"
            and Path(settings.worker_product_detector_model_path).is_file()
            and importlib.util.find_spec("ultralytics") is not None
        )
        if settings.worker_product_detector_backend == "yoloe_seg":
            self._product_detector = YoloESegProductDetector(settings)
        else:
            self._product_detector = YoloWorldProductDetector(settings) if detector_enabled else None

    # 이미지 URL을 한 번 내려받아 입력 유형·OCR·식품행 모델 후보를 한 응답으로 반환한다.
    async def infer(self, request: RemoteInferenceRequest) -> RemoteInferenceResponse:
        queued_at = perf_counter()
        async with self._inference_semaphore:
            queue_wait_ms = (perf_counter() - queued_at) * 1000
            return await self._infer_once(request, queue_wait_ms)

    # 단일 요청의 다운로드·입력 판별·OCR·Gemma 분류를 순서대로 수행한다.
    async def _infer_once(self, request: RemoteInferenceRequest, queue_wait_ms: float = 0.0) -> RemoteInferenceResponse:
        total_started = perf_counter()
        stage_started = perf_counter()
        image = await self._downloader.load(request.image_url, request.sha256)
        download_ms = (perf_counter() - stage_started) * 1000
        prediction = None
        classifier_ms = 0.0
        if request.input_hint == InputHint.AUTO and self._input_classifier is not None:
            stage_started = perf_counter()
            inferred = await self._input_classifier.classify(image)
            classifier_ms = (perf_counter() - stage_started) * 1000
            prediction = RemoteInputPrediction(
                value=inferred.document_type,
                confidence=inferred.confidence,
                margin=inferred.margin,
                accepted=inferred.accepted,
            )
        effective_input_type = request.input_hint.value
        if request.input_hint == InputHint.AUTO and prediction is not None and prediction.accepted:
            effective_input_type = prediction.value
        if effective_input_type == "PRODUCT":
            ocr_image = self._prepare_product_ocr_image(image)
        elif effective_input_type == "RECEIPT":
            ocr_image = self._prepare_receipt_ocr_image(image, self._settings.worker_receipt_ocr_min_long_edge)
        else:
            ocr_image = image
        selected = []
        diagnostics = None
        product_detection_ms = 0.0
        product_detection_count = 0
        llm_queue_wait_ms = llm_ms = 0.0
        product_parallel = effective_input_type == "PRODUCT" and self._food_classifier is not None and self._settings.worker_product_parallel_inference
        if product_parallel:
            detector_started = perf_counter()
            ocr_result, detected = await asyncio.gather(
                self._extract_with_timing(ocr_image),
                self._prepare_product_model_image(image),
            )
            product_detection_ms = (perf_counter() - detector_started) * 1000
            product_detection_count = detected.detection_count
            document, ocr_queue_wait_ms, ocr_ms = ocr_result
            safe_lines = sanitize_lines(document.lines)
            ocr_hints = self._product_ocr_hints(safe_lines, detected.boxes, image.width, image.height, document.width, document.height)
            ocr_hints = self._append_detection_labels(ocr_hints, detected.labels)
            llm_wait_started = perf_counter()
            async with self._llm_semaphore:
                llm_queue_wait_ms = (perf_counter() - llm_wait_started) * 1000
                llm_started = perf_counter()
                product_lines, product_candidates, diagnostics = await self._classify_detected_products(detected.crops, [], ocr_hints)
                llm_ms = (perf_counter() - llm_started) * 1000
            safe_lines, selected = self._merge_product_candidates(safe_lines, product_lines, product_candidates)
            document_type = "PRODUCT"
            confidence = prediction.confidence if prediction is not None else 1.0
        else:
            document, ocr_queue_wait_ms, ocr_ms = await self._extract_with_timing(ocr_image, receipt_mode=effective_input_type == "RECEIPT")
            safe_lines = sanitize_lines(document.lines)
            if self._food_classifier is not None and effective_input_type != "OTHER":
                llm_wait_started = perf_counter()
                async with self._llm_semaphore:
                    llm_queue_wait_ms = (perf_counter() - llm_wait_started) * 1000
                    stage_started = perf_counter()
                    if effective_input_type == "RECEIPT":
                        receipt_lines = self._receipt_classifier_lines(safe_lines, document.width)
                        selected, diagnostics = await self._food_classifier.classify(receipt_lines)
                        document_type = "RECEIPT"
                        confidence = prediction.confidence if prediction is not None else 0.75
                    elif effective_input_type == "PRODUCT":
                        detector_started = perf_counter()
                        detected = await self._prepare_product_model_image(image)
                        product_detection_ms = (perf_counter() - detector_started) * 1000
                        product_detection_count = detected.detection_count
                        ocr_hints = self._product_ocr_hints(safe_lines, detected.boxes, image.width, image.height, document.width, document.height)
                        ocr_hints = self._append_detection_labels(ocr_hints, detected.labels)
                        safe_lines, selected, diagnostics = await self._classify_detected_products(detected.crops, safe_lines, ocr_hints)
                        document_type = "PRODUCT"
                        confidence = prediction.confidence if prediction is not None else 1.0
                    else:
                        document_type, confidence, safe_lines, selected, diagnostics = await self._food_classifier.classify_image(
                            image,
                            safe_lines,
                            request.input_hint.value,
                        )
                    llm_ms = (perf_counter() - stage_started) * 1000
        if self._food_classifier is not None and effective_input_type != "OTHER":
            if request.input_hint == InputHint.AUTO and (prediction is None or not prediction.accepted):
                prediction = RemoteInputPrediction(
                    value=document_type,
                    confidence=confidence,
                    margin=confidence,
                    accepted=confidence >= self._settings.input_classifier_min_confidence,
                )
        total_ms = (perf_counter() - total_started) * 1000
        timing_diagnostics = {
            "workerQueueWaitMs": round(queue_wait_ms, 2),
            "downloadMs": round(download_ms, 2),
            "inputClassifierMs": round(classifier_ms, 2),
            "ocrQueueWaitMs": round(ocr_queue_wait_ms, 2),
            "ocrMs": round(ocr_ms, 2),
            "llmQueueWaitMs": round(llm_queue_wait_ms, 2),
            "llmMs": round(llm_ms, 2),
            "workerTotalMs": round(total_ms, 2),
            "inputClassifierActive": self._input_classifier is not None,
            "productParallelInference": product_parallel,
            "productDetectionMs": round(product_detection_ms, 2),
            "productDetectionCount": product_detection_count,
        }
        return RemoteInferenceResponse(
            inputPrediction=prediction,
            width=document.width,
            height=document.height,
            lines=[
                RemoteOcrLine(
                    lineNo=line.line_no,
                    text=line.text,
                    confidence=line.confidence,
                    box=[[x, y] for x, y in line.box],
                )
                for line in safe_lines
            ],
            modelCandidates=[
                RemoteModelCandidate(
                    lineNo=item.line_no,
                    normalizedName=item.normalized_name,
                    category=item.category,
                    confidence=item.confidence,
                    reason=item.reason or "model_inference",
                )
                for item in selected
            ],
            diagnostics=(
                timing_diagnostics
                | {
                    "llmPromptTokens": diagnostics.llm_prompt_tokens,
                    "llmCompletionTokens": diagnostics.llm_completion_tokens,
                    "llmPromptDurationNs": diagnostics.llm_prompt_duration_ns,
                    "llmEvalDurationNs": diagnostics.llm_eval_duration_ns,
                    "llmLoadDurationNs": diagnostics.llm_load_duration_ns,
                    "warnings": diagnostics.warnings,
                }
                if diagnostics is not None
                else timing_diagnostics
            ),
        )

    # OCR 세마포어 대기와 실제 추출 시간을 각각 측정해 문서와 함께 반환한다.
    async def _extract_with_timing(self, image: LoadedImage, receipt_mode: bool = False):
        queued_at = perf_counter()
        async with self._ocr_semaphore:
            queue_wait_ms = (perf_counter() - queued_at) * 1000
            started_at = perf_counter()
            if receipt_mode and hasattr(self._ocr, "extract_receipt"):
                document = await self._ocr.extract_receipt(image)
            else:
                document = await self._ocr.extract(image)
            elapsed_ms = (perf_counter() - started_at) * 1000
        return document, queue_wait_ms, elapsed_ms

    # YOLO가 활성화된 경우 다중 상품 contact sheet를 만들고, 비활성화되면 원본을 그대로 반환한다.
    async def _prepare_product_model_image(self, image: LoadedImage):
        if self._product_detector is None:
            from app.adapters.product_detector import ProductDetectionResult

            return ProductDetectionResult(image=image, crops=(image,), boxes=(), labels=(), detection_count=0, used_contact_sheet=False)
        try:
            return await self._product_detector.prepare(image)
        except Exception as exc:
            from app.adapters.product_detector import ProductDetectionResult

            logger.exception("product_detector.failed fallback=original error_type=%s", type(exc).__name__)
            return ProductDetectionResult(image=image, crops=(image,), boxes=(), labels=(), detection_count=0, used_contact_sheet=False)

    # YOLO crop과 해당 영역 OCR 텍스트를 제한된 동시성으로 Gemma에 전달해 단일 상품 목록으로 합친다.
    async def _classify_detected_products(self, crops: tuple[LoadedImage, ...], base_lines: list[OcrLine], ocr_hints: tuple[str, ...]):
        crop_semaphore = asyncio.Semaphore(self._settings.worker_product_crop_concurrency)

        async def classify(crop: LoadedImage, ocr_hint: str):
            resolved = self._candidate_from_ocr_hint(ocr_hint)
            if resolved is not None:
                return [OcrLine(1, resolved.normalized_name, resolved.confidence)], [resolved], PipelineDiagnostics(warnings=["gemma_skipped_by_ocr_rule"])
            async with crop_semaphore:
                return await self._food_classifier.classify_product_image(crop, [], ocr_hint)

        hints = ocr_hints if len(ocr_hints) == len(crops) else tuple("" for _ in crops)
        results = await asyncio.gather(*(classify(crop, hint) for crop, hint in zip(crops, hints, strict=True)))
        output_lines = list(base_lines)
        selected: list[ClassifiedFoodLine] = []
        diagnostics = PipelineDiagnostics()
        for result_index, (_, candidates, current) in enumerate(results):
            diagnostics.llm_prompt_tokens = (diagnostics.llm_prompt_tokens or 0) + (current.llm_prompt_tokens or 0)
            diagnostics.llm_completion_tokens = (diagnostics.llm_completion_tokens or 0) + (current.llm_completion_tokens or 0)
            diagnostics.llm_prompt_duration_ns = (diagnostics.llm_prompt_duration_ns or 0) + (current.llm_prompt_duration_ns or 0)
            diagnostics.llm_eval_duration_ns = (diagnostics.llm_eval_duration_ns or 0) + (current.llm_eval_duration_ns or 0)
            diagnostics.llm_load_duration_ns = (diagnostics.llm_load_duration_ns or 0) + (current.llm_load_duration_ns or 0)
            diagnostics.warnings.extend(current.warnings)
            for candidate in candidates[:1]:
                line_no = max((line.line_no for line in output_lines), default=0) + 1
                normalized_name = self._prefer_ocr_product_name(candidate.normalized_name, hints[result_index] if result_index < len(hints) else "")
                category = known_food_category(normalized_name) or candidate.category
                output_lines.append(OcrLine(line_no=line_no, text=normalized_name, confidence=candidate.confidence))
                selected.append(
                    ClassifiedFoodLine(
                        line_no=line_no,
                        normalized_name=normalized_name,
                        category=category,
                        confidence=candidate.confidence,
                        reason=candidate.reason if candidate.reason == "ocr_keyword_rule" else "yolo_crop_vision_model",
                    )
                )
        return output_lines, selected, diagnostics

    @staticmethod
    # 명확한 OCR 식품 키워드가 있으면 Gemma 호출 없이 상품 후보를 생성한다.
    def _candidate_from_ocr_hint(ocr_hint: str) -> ClassifiedFoodLine | None:
        segments = [value.strip() for value in ocr_hint.split(" / ")]
        for index, segment in enumerate(segments):
            if not segment or segment.startswith("객체검출 유형:"):
                continue
            category = known_food_category(segment)
            if category is not None:
                name = segment
                if index > 0 and 1 <= len(segments[index - 1]) <= 2 and all("가" <= char <= "힣" for char in segments[index - 1]):
                    name = segments[index - 1] + segment
                return ClassifiedFoodLine(line_no=1, normalized_name=name, category=category, confidence=0.9, reason="ocr_keyword_rule")
        return None

    @staticmethod
    # 모델의 일반명이 OCR의 더 구체적인 상품명에 포함되면 구체적인 OCR 표기를 우선한다.
    def _prefer_ocr_product_name(model_name: str, ocr_hint: str) -> str:
        compact_model = "".join(model_name.lower().split())
        for segment in (value.strip() for value in ocr_hint.split(" / ")):
            if not segment or segment.startswith("객체검출 유형:") or not any("가" <= char <= "힣" for char in segment):
                continue
            compact_segment = "".join(segment.lower().split())
            if len(compact_segment) > len(compact_model) and compact_model in compact_segment:
                return segment
        return model_name

    @staticmethod
    # 전체 OCR 좌표의 중심점을 원본 좌표로 환산해 각 YOLO 상품 박스에 속한 참고 텍스트를 만든다.
    def _product_ocr_hints(
        lines: list[OcrLine],
        boxes: tuple[tuple[int, int, int, int], ...],
        source_width: int,
        source_height: int,
        ocr_width: int,
        ocr_height: int,
    ) -> tuple[str, ...]:
        if not boxes:
            combined = " / ".join(line.text.strip() for line in lines if line.text.strip())[:500]
            return (combined,)
        scale_x = source_width / max(1, ocr_width)
        scale_y = source_height / max(1, ocr_height)
        hints: list[str] = []
        for left, top, right, bottom in boxes:
            texts = []
            for line in lines:
                if not line.box:
                    continue
                center_x = sum(point[0] for point in line.box) / len(line.box) * scale_x
                center_y = sum(point[1] for point in line.box) / len(line.box) * scale_y
                if left <= center_x <= right and top <= center_y <= bottom:
                    texts.append(line.text.strip())
            hints.append(" / ".join(text for text in texts if text)[:300])
        return tuple(hints)

    @staticmethod
    # OCR이 비어 있거나 깨진 crop에서도 객체 종류를 놓치지 않도록 YOLO 유형을 보조 힌트로 덧붙인다.
    def _append_detection_labels(hints: tuple[str, ...], labels: tuple[str, ...]) -> tuple[str, ...]:
        if len(hints) != len(labels):
            return hints
        return tuple(f"{hint} / 객체검출 유형: {label}" if hint else f"객체검출 유형: {label}" for hint, label in zip(hints, labels, strict=True))

    @staticmethod
    # OCR과 동시에 생성된 실물 후보를 실제 OCR 줄 뒤에 다시 번호를 부여해 일관된 응답으로 합친다.
    def _merge_product_candidates(
        ocr_lines: list[OcrLine],
        product_lines: list[OcrLine],
        candidates: list,
    ) -> tuple[list[OcrLine], list]:
        names_by_line = {line.line_no: line.text for line in product_lines}
        merged_lines = list(ocr_lines)
        merged_candidates = []
        next_line_no = max((line.line_no for line in merged_lines), default=0) + 1
        for candidate in candidates:
            name = names_by_line.get(candidate.line_no, candidate.normalized_name)
            merged_lines.append(OcrLine(next_line_no, name, candidate.confidence))
            merged_candidates.append(
                type(candidate)(
                    line_no=next_line_no,
                    normalized_name=candidate.normalized_name,
                    category=candidate.category,
                    confidence=candidate.confidence,
                    reason=candidate.reason,
                )
            )
            next_line_no += 1
        return merged_lines, merged_candidates

    @staticmethod
    # 실물 상품의 고해상도 원본만 OCR용으로 축소해 작은 글자가 많은 영수증 품질은 유지한다.
    def _prepare_product_ocr_image(image: LoadedImage, max_side: int = 1600) -> LoadedImage:
        if max(image.width, image.height) <= max_side:
            return image
        with Image.open(io.BytesIO(image.content)) as source:
            prepared = ImageOps.exif_transpose(source).convert("RGB")
            prepared.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            prepared.save(output, format="JPEG", quality=90, optimize=True)
        return LoadedImage(
            content=output.getvalue(),
            media_type="image/jpeg",
            width=prepared.width,
            height=prepared.height,
            sha256=image.sha256,
        )

    @staticmethod
    # 작은 영수증의 긴 변을 목표 크기까지 확대해 잘린 한글 초성·받침의 인식률을 높인다.
    def _prepare_receipt_ocr_image(image: LoadedImage, min_long_edge: int = 1280) -> LoadedImage:
        long_edge = max(image.width, image.height)
        if min_long_edge <= 0 or long_edge >= min_long_edge:
            return image
        scale = min_long_edge / long_edge
        target_size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        with Image.open(io.BytesIO(image.content)) as source:
            prepared = ImageOps.exif_transpose(source).convert("RGB").resize(target_size, Image.Resampling.LANCZOS)
            output = io.BytesIO()
            prepared.save(output, format="JPEG", quality=95, optimize=True)
        return LoadedImage(
            content=output.getvalue(),
            media_type="image/jpeg",
            width=prepared.width,
            height=prepared.height,
            sha256=image.sha256,
        )

    @staticmethod
    # OCR 좌표로 복원한 실제 상품 행만 추려 수량·금액 힌트를 붙인 Gemma 입력을 반환한다.
    def _receipt_classifier_lines(lines: list[OcrLine], image_width: int) -> list[OcrLine]:
        rows = build_receipt_product_rows(lines, image_width)
        if not rows:
            return [line for line in build_food_candidate_lines(lines) if not is_non_food(line.text) and known_food_category(line.text) is None]
        return [GpuInferenceEngine._with_receipt_hint(row.product_line, row) for row in rows if not is_non_food(row.product_line.text) and known_food_category(row.product_line.text) is None]

    @staticmethod
    # 복원된 상품 행에 실제 수량·금액을 추가해 낯선 상품명도 구매 행으로 판별할 수 있게 한다.
    def _with_receipt_hint(line: OcrLine, row: ReceiptProductRow) -> OcrLine:
        if row.amount is None:
            return line
        name = line.text.lstrip("*#≠ ")
        detail = f"수량 {row.quantity}개, {row.amount:,}원" if row.quantity is not None else f"{row.amount:,}원"
        return OcrLine(
            line_no=line.line_no,
            text=f"{name} ({detail})",
            confidence=line.confidence,
            box=line.box,
        )

    # 구성된 OCR·입력 분류·LLM의 준비 상태를 반환한다.
    async def readiness(self) -> dict[str, bool]:
        result = {"ocr": await self._ocr.ready()}
        if self._input_classifier is not None:
            result["inputClassifier"] = await self._input_classifier.ready()
        if self._food_classifier is not None:
            result["ollama"] = await self._food_classifier.ready()
        if self._product_detector is not None:
            result["productDetector"] = await self._product_detector.ready()
        return result

    # 다운로드 HTTP 연결과 모델 클라이언트를 정리한다.
    async def close(self) -> None:
        await self._downloader.close()
        if self._food_classifier is not None:
            await self._food_classifier.close()
