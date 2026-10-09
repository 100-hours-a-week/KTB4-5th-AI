import asyncio
import ctypes
import difflib
import gc
import io
import logging
import re
import tempfile
from typing import Any

from PIL import Image

from app.adapters.ort_runtime import install_no_arena_ort_backend
from app.config import Settings
from app.domain.models import LoadedImage, OcrDocument, OcrLine
from app.errors import ModelUnavailable

logger = logging.getLogger(__name__)


class PaddleTextExtractor:
    # PaddleOCR 언어·버전·장치 설정을 받아 지연 초기화 OCR 추출기를 구성한다.
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine: Any | None = None
        self._lock = asyncio.Lock()
        self._initialization_lock = asyncio.Lock()
        self._requests_since_recycle = 0

    # OCR 엔진 초기화를 시도해 사용 가능 여부를 boolean으로 반환한다.
    async def ready(self) -> bool:
        try:
            await self._get_engine()
            return True
        except ModelUnavailable:
            return False

    # 검증된 이미지를 받아 잠금 안에서 OCR을 실행하고 줄·좌표 문서를 반환한다.
    async def extract(self, image: LoadedImage) -> OcrDocument:
        async with self._lock:
            engine = await self._get_engine()
            try:
                lines = await asyncio.to_thread(self._predict, engine, image.content)
            except Exception as exc:
                logger.exception("ocr.inference.failed error_type=%s", type(exc).__name__)
                raise ModelUnavailable("PP-OCR 이미지 문자 추출에 실패했습니다.") from exc
            await self._maybe_recycle_engine()
        return OcrDocument(lines=tuple(lines), width=image.width, height=image.height)

    # 영수증 전체 OCR과 겹치는 세로 타일 OCR을 실행해 작은 상품행 detection 누락을 보완한다.
    async def extract_receipt(self, image: LoadedImage) -> OcrDocument:
        async with self._lock:
            engine = await self._get_engine()
            try:
                lines = await asyncio.to_thread(self._predict_receipt, engine, image)
            except Exception as exc:
                logger.exception("ocr.receipt_inference.failed error_type=%s", type(exc).__name__)
                raise ModelUnavailable("PP-OCR 영수증 문자 추출에 실패했습니다.") from exc
            await self._maybe_recycle_engine()
        return OcrDocument(lines=tuple(lines), width=image.width, height=image.height)

    # 요청마다 이미지 크기가 달라지면 onnxruntime의 내부 메모리 아레나가 계속 커지고 줄지 않는다.
    # 지금은 app/adapters/ort_runtime.py가 아레나를 끈 세션으로 바꿔 근본적으로 막고, 이 리사이클은
    # 그 교체가 실패했을 때의 안전장치다. 엔진 객체를 버리고 gc+malloc_trim을 하면 그 메모리를 거의
    # 그대로 회수할 수 있어(실측: 6.6GB→522MB), 프로세스 재시작 없이 리사이클한다. 다음 요청은
    # _get_engine()이 지연 재생성한다.
    #
    # 요청 개수만으로 판단하면 위험하다 — 실측 결과 이미지 크기 편차가 커서 5번째 요청만에
    # 6.5GB를 넘긴 적이 있다(요청 25개를 다 채우기 전에 OOM 날 수 있음). 그래서 요청 개수
    # 임계값과 별개로, 매 요청 뒤 실제 RSS를 확인해 임계값을 넘으면 개수와 무관하게 즉시
    # 리사이클한다 — 둘 중 먼저 도달하는 쪽을 따른다.
    async def _maybe_recycle_engine(self) -> None:
        threshold = self._settings.paddle_engine_recycle_after
        rss_threshold_mb = self._settings.paddle_engine_recycle_rss_mb
        self._requests_since_recycle += 1

        reason = None
        if threshold > 0 and self._requests_since_recycle >= threshold:
            reason = f"requests>={threshold}"
        elif rss_threshold_mb > 0 and self._current_rss_mb() >= rss_threshold_mb:
            reason = f"rss>={rss_threshold_mb}MB"

        if reason is None:
            return
        self._requests_since_recycle = 0
        logger.info("ocr.engine.recycling reason=%s", reason)
        self._engine = None
        await asyncio.to_thread(self._trim_memory)

    @staticmethod
    def _current_rss_mb() -> int:
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) // 1024
        except OSError:
            pass  # /proc이 없는 환경(예: macOS 개발 환경)에서는 개수 기준만 쓴다.
        return 0

    @staticmethod
    def _trim_memory() -> None:
        gc.collect()
        try:
            libc = ctypes.CDLL("libc.so.6")
            libc.malloc_trim(0)
        except OSError:
            pass  # glibc가 아닌 환경(예: macOS 개발 환경)에서는 조용히 건너뛴다.

    # 초기화된 PaddleOCR(또는 HPI paddlex 파이프라인) 엔진을 반환하거나 설정으로 한 번 생성해 반환한다.
    async def _get_engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        async with self._initialization_lock:
            if self._engine is not None:
                return self._engine
            if self._settings.paddle_hpi_backend:
                try:
                    self._engine = await asyncio.to_thread(self._build_hpi_pipeline)
                    return self._engine
                except Exception:
                    # HPI 의존성이 배포 환경에 없거나(`paddleocr install_hpi_deps cpu` 미실행)
                    # 백엔드가 이 환경에서 지원되지 않으면, OCR 전체를 죽이지 않고 표준
                    # paddleocr.PaddleOCR() 경로로 내려간다 — 느려질 뿐 서비스는 계속된다.
                    logger.exception(
                        "ocr.hpi_pipeline.failed falling back to standard PaddleOCR() backend=%s",
                        self._settings.paddle_hpi_backend,
                    )
            try:
                from paddleocr import PaddleOCR

                self._engine = await asyncio.to_thread(
                    PaddleOCR,
                    lang=self._settings.paddle_lang,
                    ocr_version=self._settings.paddle_ocr_version,
                    device=self._settings.paddle_device,
                    enable_mkldnn=self._settings.paddle_enable_mkldnn,
                    use_doc_orientation_classify=self._settings.paddle_use_doc_orientation_classify,
                    use_doc_unwarping=self._settings.paddle_use_doc_unwarping,
                    use_textline_orientation=self._settings.paddle_use_textline_orientation,
                    text_detection_model_name=self._settings.paddle_det_model_name,
                    text_det_limit_side_len=self._settings.paddle_det_limit_side_len,
                    text_det_thresh=self._settings.paddle_det_thresh,
                    text_det_box_thresh=self._settings.paddle_det_box_thresh,
                    text_det_unclip_ratio=self._settings.paddle_det_unclip_ratio,
                )
            except Exception as exc:
                logger.exception("ocr.initialization.failed error_type=%s", type(exc).__name__)
                raise ModelUnavailable(
                    "PaddleOCR를 준비하지 못했습니다. OCR 선택 의존성을 설치해 주세요."
                ) from exc
            return self._engine

    # text_detection·text_recognition 두 모델만 paddle_hpi_backend로 강제하고 나머지 단계
    # (문서방향분류/왜곡보정/텍스트줄방향)는 HPI 안전 자동설정에 맡기는 저수준 paddlex
    # 파이프라인을 만든다. paddleocr.PaddleOCR(enable_hpi=True) 전체 자동 선택은
    # PP-OCRv5_server_det에서 oneDNN/PIR 비호환 크래시를 냈지만(paddle 3.3.1로 직접 확인),
    # det·rec만 골라 강제하면 크래시 없이 실측 8~48% 추가 속도 개선을 얻는다(3개 실영수증
    # 텍스트 내용 diff로 검증, 손실 없음).
    def _build_hpi_pipeline(self) -> Any:
        from paddleocr import PaddleOCR
        from paddlex import create_pipeline

        det_model_name, rec_model_name = PaddleOCR._get_ocr_model_names(
            None, self._settings.paddle_lang, self._settings.paddle_ocr_version
        )
        det_model_name = self._settings.paddle_det_model_name or det_model_name
        rec_model_name = self._settings.paddle_rec_model_name or rec_model_name
        threads = self._settings.paddle_cpu_threads
        if self._settings.paddle_hpi_backend == "onnxruntime" and self._settings.paddle_ort_disable_arena:
            install_no_arena_ort_backend(threads)
        # backend_config의 스레드 수는 교체가 실패해 내장 런타임으로 돌아갈 때도 적용된다.
        hpi_config = {"backend": self._settings.paddle_hpi_backend, "backend_config": {"cpu_num_threads": threads}}
        config = {
            "pipeline_name": "OCR",
            "text_type": "general",
            "use_doc_preprocessor": True,
            "use_textline_orientation": self._settings.paddle_use_textline_orientation,
            "SubPipelines": {
                "DocPreprocessor": {
                    "pipeline_name": "doc_preprocessor",
                    "use_doc_orientation_classify": self._settings.paddle_use_doc_orientation_classify,
                    "use_doc_unwarping": self._settings.paddle_use_doc_unwarping,
                    "SubModules": {
                        "DocOrientationClassify": {
                            "module_name": "doc_text_orientation",
                            "model_name": "PP-LCNet_x1_0_doc_ori",
                            "model_dir": None,
                        },
                        "DocUnwarping": {
                            "module_name": "image_unwarping",
                            "model_name": "UVDoc",
                            "model_dir": None,
                        },
                    },
                },
            },
            "SubModules": {
                "TextDetection": {
                    "module_name": "text_detection",
                    "model_name": det_model_name,
                    "model_dir": None,
                    "limit_side_len": self._settings.paddle_det_limit_side_len or 64,
                    "limit_type": "min",
                    "max_side_limit": 4000,
                    "thresh": self._settings.paddle_det_thresh,
                    "box_thresh": self._settings.paddle_det_box_thresh,
                    "unclip_ratio": self._settings.paddle_det_unclip_ratio,
                    "hpi_config": hpi_config,
                },
                "TextLineOrientation": {
                    "module_name": "textline_orientation",
                    "model_name": "PP-LCNet_x1_0_textline_ori",
                    "model_dir": None,
                    "batch_size": 6,
                },
                "TextRecognition": {
                    "module_name": "text_recognition",
                    "model_name": rec_model_name,
                    "model_dir": None,
                    "batch_size": 6,
                    "score_thresh": 0.0,
                    "hpi_config": hpi_config,
                },
            },
        }
        return create_pipeline(config=config, use_hpip=True)

    # OCR 엔진과 이미지 bytes를 받아 임시 파일 추론 후 정규화된 OCR 줄을 반환한다.
    def _predict(self, engine: Any, content: bytes, *, disable_unwarping: bool = False) -> list[OcrLine]:
        suffix = ".png"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as temp:
            temp.write(content)
            temp.flush()
            if hasattr(engine, "predict"):
                kwargs = {"input": temp.name}
                if disable_unwarping:
                    kwargs["use_doc_unwarping"] = False
                result = engine.predict(**kwargs)
                return self._parse_v3(result)
            result = engine.ocr(temp.name, cls=True)
            return self._parse_legacy(result)

    # 전체 영수증 결과에 세로 타일 결과의 원본 좌표를 복원해 합치고 중복 줄을 제거한다.
    def _predict_receipt(self, engine: Any, image: LoadedImage) -> list[OcrLine]:
        primary = self._predict(engine, image.content)
        if not self._settings.worker_receipt_ocr_tiling or image.height <= image.width:
            return primary
        missing_item_numbers = self._missing_receipt_item_numbers(primary)
        if not missing_item_numbers:
            logger.info("ocr.receipt_tiling skipped=no_number_gap primary=%d", len(primary))
            return primary

        source = Image.open(io.BytesIO(image.content)).convert("RGB")
        tile_height = max(320, round(source.height * self._settings.worker_receipt_ocr_tile_height_ratio))
        overlap = max(32, round(tile_height * self._settings.worker_receipt_ocr_tile_overlap_ratio))
        step = max(1, tile_height - overlap)
        tiled: list[OcrLine] = []
        top = 0
        while top < source.height:
            bottom = min(source.height, top + tile_height)
            if bottom - top < 160:
                top = max(0, source.height - tile_height)
                bottom = source.height
            crop = source.crop((0, top, source.width, bottom))
            buffer = io.BytesIO()
            crop.save(buffer, format="PNG")
            for line in self._predict(engine, buffer.getvalue(), disable_unwarping=True):
                if line.confidence < self._settings.worker_receipt_ocr_tile_min_confidence:
                    continue
                if self._receipt_item_number(line.text) not in missing_item_numbers:
                    continue
                tiled.append(
                    OcrLine(
                        line_no=0,
                        text=line.text,
                        confidence=line.confidence,
                        box=tuple((x, y + top) for x, y in line.box),
                    )
                )
            if bottom >= source.height:
                break
            top += step

        merged = self._merge_lines(primary, tiled)
        logger.info("ocr.receipt_tiling primary=%d tiled=%d merged=%d", len(primary), len(tiled), len(merged))
        return merged

    # OCR 상품행 번호의 중간 공백과 상품수 표기 이후의 누락 번호를 찾아 재검출 대상을 제한한다.
    @classmethod
    def _missing_receipt_item_numbers(cls, lines: list[OcrLine]) -> set[int]:
        detected = {number for line in lines if (number := cls._receipt_item_number(line.text)) is not None}
        if len(detected) < 2:
            return set()
        expected_last = max(detected)
        for index, line in enumerate(lines):
            if "상품수" not in re.sub(r"\s+", "", line.text):
                continue
            inline = re.search(r"상품수\D{0,3}(\d{1,2})", line.text)
            if inline:
                expected_last = max(expected_last, int(inline.group(1)))
                break
            for following in lines[index + 1 : index + 3]:
                if re.fullmatch(r"\s*\d{1,2}\s*", following.text):
                    expected_last = max(expected_last, int(following.text.strip()))
                    break
        if expected_last > 99:
            return set()
        return set(range(min(detected), expected_last + 1)) - detected

    # 행 앞의 1~2자리 또는 001 형식 상품 번호만 반환하고 금액·수량 단독 숫자는 제외한다.
    @staticmethod
    def _receipt_item_number(text: str) -> int | None:
        match = re.match(r"^\s*0*(\d{1,2})\s*[^0-9\s,.$*]", text)
        return int(match.group(1)) if match else None

    # 동일 문구이면서 중심 좌표가 가까운 타일 중복은 높은 신뢰도 하나만 남기고 읽기 순서로 재번호화한다.
    @classmethod
    def _merge_lines(cls, primary: list[OcrLine], tiled: list[OcrLine]) -> list[OcrLine]:
        selected = list(primary)
        for candidate in tiled:
            duplicate_index = next((index for index, line in enumerate(selected) if cls._same_line(line, candidate)), None)
            if duplicate_index is None:
                selected.append(candidate)
            elif candidate.confidence > selected[duplicate_index].confidence:
                selected[duplicate_index] = candidate
        selected.sort(key=lambda line: (cls._box_center(line.box)[1], cls._box_center(line.box)[0]))
        return [OcrLine(index, line.text, line.confidence, line.box) for index, line in enumerate(selected, start=1)]

    # 좌표가 겹치고 정규화 문구가 같거나 포함·유사한 경우 타일 중복 검출로 판단한다.
    @classmethod
    def _same_line(cls, left: OcrLine, right: OcrLine) -> bool:
        left_text = re.sub(r"[^0-9A-Za-z가-힣]", "", left.text).casefold()
        right_text = re.sub(r"[^0-9A-Za-z가-힣]", "", right.text).casefold()
        if not left_text or not right_text:
            return False
        left_x, left_y = cls._box_center(left.box)
        right_x, right_y = cls._box_center(right.box)
        left_height = cls._box_height(left.box)
        right_height = cls._box_height(right.box)
        nearby = abs(left_y - right_y) <= max(8.0, left_height, right_height) and abs(left_x - right_x) <= max(24.0, left_height, right_height) * 2
        if not nearby:
            return False
        if left_text == right_text:
            return True
        shorter, longer = sorted((left_text, right_text), key=len)
        if len(shorter) >= 2 and shorter in longer:
            return True
        return difflib.SequenceMatcher(None, left_text, right_text).ratio() >= 0.82

    @staticmethod
    def _box_center(box: tuple[tuple[float, float], ...]) -> tuple[float, float]:
        if not box:
            return (0.0, 0.0)
        return (sum(point[0] for point in box) / len(box), sum(point[1] for point in box) / len(box))

    @staticmethod
    def _box_height(box: tuple[tuple[float, float], ...]) -> float:
        if not box:
            return 0.0
        ys = [point[1] for point in box]
        return max(ys) - min(ys)

    # PaddleOCR v3 결과 객체를 받아 순번·문자·신뢰도·좌표를 가진 줄 목록으로 변환한다.
    def _parse_v3(self, result: Any) -> list[OcrLine]:
        lines: list[OcrLine] = []
        for page in result or []:
            data = getattr(page, "json", None)
            data = data() if callable(data) else data
            if isinstance(data, dict) and "res" in data:
                data = data["res"]
            if not isinstance(data, dict):
                data = getattr(page, "res", {})
            texts = data.get("rec_texts", [])
            scores = data.get("rec_scores", [])
            boxes = data.get("rec_polys", data.get("dt_polys", []))
            for text, score, box in zip(texts, scores, boxes, strict=False):
                cleaned = str(text).strip()
                if cleaned:
                    lines.append(
                        OcrLine(
                            line_no=len(lines) + 1,
                            text=cleaned,
                            confidence=float(score),
                            box=self._normalize_box(box),
                        )
                    )
        return lines

    # 구형 PaddleOCR 결과를 받아 공통 OCR 줄 목록으로 변환한다.
    def _parse_legacy(self, result: Any) -> list[OcrLine]:
        lines: list[OcrLine] = []
        for page in result or []:
            for entry in page or []:
                if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                    continue
                box, recognition = entry[0], entry[1]
                if not isinstance(recognition, (list, tuple)) or len(recognition) < 2:
                    continue
                text, score = str(recognition[0]).strip(), float(recognition[1])
                if text:
                    lines.append(
                        OcrLine(
                            line_no=len(lines) + 1,
                            text=text,
                            confidence=score,
                            box=self._normalize_box(box),
                        )
                    )
        return lines

    @staticmethod
    # 임의 좌표 배열을 받아 float 좌표 tuple로 변환하며 잘못된 값이면 빈 tuple을 반환한다.
    def _normalize_box(box: Any) -> tuple[tuple[float, float], ...]:
        try:
            return tuple((float(point[0]), float(point[1])) for point in box)
        except (TypeError, ValueError, IndexError):
            return ()


class StubTextExtractor:
    """테스트 주입 전용. production 설정에서는 사용할 수 없다."""

    # 테스트 대체 객체 자체는 준비된 것으로 간주해 True를 반환한다.
    async def ready(self) -> bool:
        return True

    # 테스트 영수증 경로에서도 빈 OCR 문서를 반환하는 인터페이스를 제공한다.
    async def extract_receipt(self, image: LoadedImage) -> OcrDocument:
        return OcrDocument(lines=(), width=image.width, height=image.height)

    # 주입되지 않은 테스트 OCR 호출을 차단하기 위해 MODEL_UNAVAILABLE을 발생시킨다.
    async def extract(self, image: LoadedImage) -> OcrDocument:
        raise ModelUnavailable("stub OCR에는 테스트용 구현을 주입해야 합니다.")
