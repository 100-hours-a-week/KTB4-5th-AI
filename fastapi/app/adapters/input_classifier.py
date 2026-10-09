import asyncio
import logging
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from app.config import Settings
from app.domain.models import InputTypePrediction, LoadedImage

logger = logging.getLogger(__name__)


class OnnxMobileNetInputClassifier:
    # 설정을 받아 Colab에서 내보낸 MobileNetV3-Small ONNX 입력 유형 분류기를 구성한다.
    def __init__(self, settings: Settings) -> None:
        self._model_path = Path(settings.input_classifier_model_path)
        self._labels = tuple(
            label.strip() for label in settings.input_classifier_labels.split(",") if label.strip()
        )
        self._min_confidence = settings.input_classifier_min_confidence
        self._min_margin = settings.input_classifier_min_margin
        self._cpu_threads = settings.input_classifier_cpu_threads
        self._session: Any | None = None
        self._input_name: str | None = None
        self._load_lock = asyncio.Lock()

    # 모델 파일과 ONNX Runtime 의존성을 확인해 실제 추론 준비 여부를 반환한다.
    async def ready(self) -> bool:
        try:
            await self._ensure_session()
        except Exception:
            logger.exception("input_classifier.ready_failed model_path=%s", self._model_path)
            return False
        return True

    # 이미지 바이트를 MobileNetV3 전처리 규약으로 변환해 유형·신뢰도·상위 점수 차이를 반환한다.
    async def classify(self, image: LoadedImage) -> InputTypePrediction:
        await self._ensure_session()
        tensor = await asyncio.to_thread(self._preprocess, image.content)
        scores = await asyncio.to_thread(self._infer, tensor)
        ranked = sorted(enumerate(scores), key=lambda pair: pair[1], reverse=True)
        top_index, confidence = ranked[0]
        margin = confidence - ranked[1][1]
        return InputTypePrediction(
            document_type=self._labels[top_index],
            confidence=confidence,
            margin=margin,
            accepted=confidence >= self._min_confidence and margin >= self._min_margin,
        )

    # 첫 요청에서만 ONNX Runtime 세션을 생성하고 이후 요청에는 같은 세션을 재사용한다.
    async def _ensure_session(self) -> None:
        if self._session is not None:
            return
        async with self._load_lock:
            if self._session is not None:
                return
            if not self._model_path.is_file():
                raise FileNotFoundError(f"입력 유형 모델 파일이 없습니다: {self._model_path}")
            self._session, self._input_name = await asyncio.to_thread(self._load_session)

    # 설정한 CPU 스레드 수로 ONNX Runtime 세션과 입력 이름을 생성한다.
    def _load_session(self) -> tuple[Any, str]:
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = self._cpu_threads
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(
            str(self._model_path),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        return session, session.get_inputs()[0].name

    # 원본 이미지 바이트를 256 리사이즈·224 중앙 자르기·ImageNet 정규화 NCHW 배열로 변환한다.
    @staticmethod
    def _preprocess(content: bytes):
        import numpy as np

        with Image.open(BytesIO(content)) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
            width, height = image.size
            scale = 256 / min(width, height)
            resized = image.resize(
                (round(width * scale), round(height * scale)),
                Image.Resampling.BILINEAR,
            )
            left = (resized.width - 224) // 2
            top = (resized.height - 224) // 2
            cropped = resized.crop((left, top, left + 224, top + 224))
            array = np.asarray(cropped, dtype=np.float32) / 255.0
        mean = np.asarray([0.485, 0.456, 0.406], dtype=np.float32)
        std = np.asarray([0.229, 0.224, 0.225], dtype=np.float32)
        normalized = (array - mean) / std
        return np.transpose(normalized, (2, 0, 1))[None, ...]

    # 전처리 배열을 ONNX 모델에 넣고 softmax 확률 목록을 반환한다.
    def _infer(self, tensor) -> list[float]:
        import numpy as np

        logits = self._session.run(None, {self._input_name: tensor})[0][0]
        shifted = logits - np.max(logits)
        probabilities = np.exp(shifted) / np.exp(shifted).sum()
        return [float(value) for value in probabilities]
