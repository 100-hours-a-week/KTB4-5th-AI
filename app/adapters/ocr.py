import asyncio
import ctypes
import gc
import logging
import tempfile
from typing import Any

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
                raise ModelUnavailable("PP-OCR 영수증 분석에 실패했습니다.") from exc
            await self._maybe_recycle_engine()
        return OcrDocument(lines=tuple(lines), width=image.width, height=image.height)

    # 요청마다 이미지 크기가 달라지면 onnxruntime/paddle inference의 내부 메모리 아레나가
    # 계속 커지고 줄지 않는다. 엔진 객체를 버리고 gc+malloc_trim을 하면 그 메모리를 거의 그대로
    # 회수할 수 있어(실측: 6.6GB→522MB), 프로세스 재시작 없이 리사이클한다. 다음 요청은
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
        hpi_config = {"backend": self._settings.paddle_hpi_backend}
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
                    "thresh": 0.3,
                    "box_thresh": 0.6,
                    "unclip_ratio": 1.5,
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
    def _predict(self, engine: Any, content: bytes) -> list[OcrLine]:
        suffix = ".png"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as temp:
            temp.write(content)
            temp.flush()
            if hasattr(engine, "predict"):
                result = engine.predict(input=temp.name)
                return self._parse_v3(result)
            result = engine.ocr(temp.name, cls=True)
            return self._parse_legacy(result)

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

    # 주입되지 않은 테스트 OCR 호출을 차단하기 위해 MODEL_UNAVAILABLE을 발생시킨다.
    async def extract(self, image: LoadedImage) -> OcrDocument:
        raise ModelUnavailable("stub OCR에는 테스트용 구현을 주입해야 합니다.")
