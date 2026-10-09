import asyncio
import hashlib
import io
import math
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from app.config import Settings
from app.domain.models import LoadedImage


@dataclass(frozen=True, slots=True)
class ProductDetectionResult:
    image: LoadedImage
    crops: tuple[LoadedImage, ...]
    boxes: tuple[tuple[int, int, int, int], ...]
    labels: tuple[str, ...]
    detection_count: int
    used_contact_sheet: bool


class YoloWorldProductDetector:
    # YOLO-World를 지연 로드하고 설정된 상품군 프롬프트를 한 번만 인코딩한다.
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model = None
        self._load_lock = asyncio.Lock()

    # 다중 상품 영역을 검출해 번호가 붙은 contact sheet를 반환하며, 2개 미만이면 원본을 반환한다.
    async def prepare(self, image: LoadedImage) -> ProductDetectionResult:
        model = await self._get_model()
        return await asyncio.to_thread(self._prepare_sync, model, image)

    # 가중치 존재 여부를 기준으로 모델 준비 가능 상태를 반환한다.
    async def ready(self) -> bool:
        return Path(self._settings.worker_product_detector_model_path).is_file()

    # 첫 요청에서만 모델과 텍스트 클래스를 로드하여 GPU 메모리와 초기화 시간을 재사용한다.
    async def _get_model(self):
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is None:
                self._model = await asyncio.to_thread(self._load_model)
        return self._model

    # Ultralytics를 선택 기능으로 import하고 YOLO-World 모델에 상품군 프롬프트를 설정한다.
    def _load_model(self):
        try:
            from ultralytics import YOLOWorld
        except ImportError as exc:
            raise RuntimeError("YOLO-World 사용에는 runpod[yolo] 의존성이 필요합니다.") from exc
        model = YOLOWorld(self._settings.worker_product_detector_model_path)
        model.set_classes(self._classes())
        return model

    # 설정 문자열을 공백 제거된 프롬프트 목록으로 변환한다.
    def _classes(self) -> list[str]:
        return [value.strip() for value in self._settings.worker_product_detector_classes.split(",") if value.strip()]

    # 실제 검출 결과를 위에서 아래, 왼쪽에서 오른쪽 순으로 정렬해 contact sheet로 만든다.
    def _prepare_sync(self, model, image: LoadedImage) -> ProductDetectionResult:
        with Image.open(io.BytesIO(image.content)) as source:
            original = ImageOps.exif_transpose(source).convert("RGB")
        result = model.predict(
            original,
            conf=self._settings.worker_product_detector_confidence,
            iou=self._settings.worker_product_detector_iou,
            imgsz=self._settings.worker_product_detector_image_size,
            max_det=self._settings.worker_product_detector_max_detections,
            agnostic_nms=True,
            verbose=False,
        )[0]
        raw_boxes = [tuple(round(float(value)) for value in box) for box in result.boxes.xyxy.tolist()]
        label_by_box = {box: self._classes()[int(class_id)] for box, class_id in zip(raw_boxes, result.boxes.cls.tolist(), strict=True)}
        boxes = raw_boxes
        boxes = self._valid_boxes(boxes, original.width, original.height)
        boxes = self._remove_nested_boxes(boxes)
        boxes = self._merge_overlapping_fragments(boxes)
        if len(boxes) < 2:
            return ProductDetectionResult(image=image, crops=(image,), boxes=(), labels=(), detection_count=len(boxes), used_contact_sheet=False)
        boxes.sort(key=lambda box: (box[1] // max(1, original.height // 4), box[0]))
        crops = [original.crop(box) for box in boxes]
        loaded_crops = tuple(self._encode_crop(crop) for crop in crops)
        prepared = self._contact_sheet(crops)
        output = io.BytesIO()
        prepared.save(output, format="JPEG", quality=92, optimize=True)
        content = output.getvalue()
        return ProductDetectionResult(
            image=LoadedImage(
                content=content,
                media_type="image/jpeg",
                width=prepared.width,
                height=prepared.height,
                sha256=hashlib.sha256(content).hexdigest(),
            ),
            crops=loaded_crops,
            boxes=tuple(boxes),
            labels=tuple(label_by_box.get(box, "food package") for box in boxes),
            detection_count=len(boxes),
            used_contact_sheet=True,
        )

    # 개별 상품 crop을 Gemma가 받을 수 있는 JPEG LoadedImage로 변환한다.
    @staticmethod
    def _encode_crop(crop: Image.Image) -> LoadedImage:
        output = io.BytesIO()
        crop.save(output, format="JPEG", quality=92, optimize=True)
        content = output.getvalue()
        return LoadedImage(content=content, media_type="image/jpeg", width=crop.width, height=crop.height, sha256=hashlib.sha256(content).hexdigest())

    # 지나치게 작거나 이미지 밖으로 벗어난 검출 박스를 제거한다.
    @staticmethod
    def _valid_boxes(boxes: list[tuple[int, int, int, int]], width: int, height: int) -> list[tuple[int, int, int, int]]:
        minimum_area = width * height * 0.008
        valid = []
        for left, top, right, bottom in boxes:
            clipped = (max(0, left), max(0, top), min(width, right), min(height, bottom))
            box_width = clipped[2] - clipped[0]
            box_height = clipped[3] - clipped[1]
            if box_width >= width * 0.04 and box_height >= height * 0.12 and box_width * box_height >= minimum_area:
                valid.append(clipped)
        return valid

    # 여러 작은 상품을 한 번에 감싼 큰 박스와 상품 내부 라벨을 제거해 상품 단위 crop만 남긴다.
    @staticmethod
    def _remove_nested_boxes(boxes: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
        def area(box: tuple[int, int, int, int]) -> int:
            return (box[2] - box[0]) * (box[3] - box[1])

        def contains(outer: tuple[int, int, int, int], inner: tuple[int, int, int, int]) -> bool:
            center_x = (inner[0] + inner[2]) / 2
            center_y = (inner[1] + inner[3]) / 2
            return outer[0] <= center_x <= outer[2] and outer[1] <= center_y <= outer[3]

        aggregate_boxes = {
            candidate
            for candidate in boxes
            if len([other for other in boxes if other != candidate and area(other) < area(candidate) * 0.8 and contains(candidate, other)]) >= 2
        }
        candidates = [box for box in boxes if box not in aggregate_boxes]
        result = []
        for candidate in candidates:
            containers = [other for other in candidates if other != candidate and area(candidate) < area(other) * 0.5 and contains(other, candidate)]
            if containers:
                continue
            result.append(candidate)
        return result

    # 같은 포장지 내부를 여러 상품으로 잘못 검출한 겹침 박스들을 하나의 상품 영역으로 합친다.
    @staticmethod
    def _merge_overlapping_fragments(boxes: list[tuple[int, int, int, int]], overlap_threshold: float = 0.2) -> list[tuple[int, int, int, int]]:
        if len(boxes) < 2:
            return boxes

        def area(box: tuple[int, int, int, int]) -> int:
            return max(0, box[2] - box[0]) * max(0, box[3] - box[1])

        def overlap_ratio(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> float:
            intersection_width = max(0, min(first[2], second[2]) - max(first[0], second[0]))
            intersection_height = max(0, min(first[3], second[3]) - max(first[1], second[1]))
            intersection = intersection_width * intersection_height
            minimum_area = min(area(first), area(second))
            return intersection / minimum_area if minimum_area else 0.0

        parent = list(range(len(boxes)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(first: int, second: int) -> None:
            first_root = find(first)
            second_root = find(second)
            if first_root != second_root:
                parent[second_root] = first_root

        for first_index, first in enumerate(boxes):
            for second_index in range(first_index + 1, len(boxes)):
                if overlap_ratio(first, boxes[second_index]) >= overlap_threshold:
                    union(first_index, second_index)

        groups: dict[int, list[tuple[int, int, int, int]]] = {}
        for index, box in enumerate(boxes):
            groups.setdefault(find(index), []).append(box)

        return [
            (
                min(box[0] for box in group),
                min(box[1] for box in group),
                max(box[2] for box in group),
                max(box[3] for box in group),
            )
            for group in groups.values()
        ]

    # 각 검출 영역을 같은 크기의 셀에 letterbox 배치하고 번호를 표시한다.
    @staticmethod
    def _contact_sheet(crops: list[Image.Image], cell_size: int = 384) -> Image.Image:
        columns = min(4, max(2, math.ceil(math.sqrt(len(crops)))))
        rows = math.ceil(len(crops) / columns)
        sheet = Image.new("RGB", (columns * cell_size, rows * cell_size), "white")
        draw = ImageDraw.Draw(sheet)
        for index, crop in enumerate(crops):
            scale = min((cell_size - 24) / crop.width, (cell_size - 24) / crop.height)
            target_size = (max(1, round(crop.width * scale)), max(1, round(crop.height * scale)))
            item = crop.resize(target_size, Image.Resampling.LANCZOS)
            x = (index % columns) * cell_size + (cell_size - item.width) // 2
            y = (index // columns) * cell_size + (cell_size - item.height) // 2
            sheet.paste(item, (x, y))
            draw.rectangle((index % columns * cell_size + 6, index // columns * cell_size + 6, index % columns * cell_size + 48, index // columns * cell_size + 38), fill="white", outline="black", width=2)
            draw.text((index % columns * cell_size + 16, index // columns * cell_size + 12), str(index + 1), fill="black")
        return sheet


class YoloESegProductDetector(YoloWorldProductDetector):
    # segmentation 체크포인트가 준비됐는지 반환한다.
    async def ready(self) -> bool:
        return Path(self._settings.worker_product_segmenter_model_path).is_file()

    # YOLOE open-vocabulary segmentation 모델을 로드하고 기존 상품 프롬프트를 설정한다.
    def _load_model(self):
        try:
            from ultralytics import YOLOE
        except ImportError as exc:
            raise RuntimeError("YOLOE-seg 사용에는 runpod[yolo] 의존성이 필요합니다.") from exc
        model = YOLOE(self._settings.worker_product_segmenter_model_path)
        model.set_classes(self._classes())
        return model

    # 상품별 인스턴스 마스크로 배경을 제거한 crop과 contact sheet를 만든다.
    def _prepare_sync(self, model, image: LoadedImage) -> ProductDetectionResult:
        with Image.open(io.BytesIO(image.content)) as source:
            original = ImageOps.exif_transpose(source).convert("RGB")
        prediction = model.predict(
            original,
            conf=self._settings.worker_product_detector_confidence,
            iou=self._settings.worker_product_detector_iou,
            imgsz=self._settings.worker_product_detector_image_size,
            max_det=self._settings.worker_product_detector_max_detections,
            agnostic_nms=True,
            verbose=False,
        )[0]
        if prediction.boxes is None:
            return ProductDetectionResult(image=image, crops=(image,), boxes=(), labels=(), detection_count=0, used_contact_sheet=False)

        raw_boxes = [tuple(round(float(value)) for value in box) for box in prediction.boxes.xyxy.tolist()]
        valid_boxes = self._remove_nested_boxes(self._valid_boxes(raw_boxes, original.width, original.height))
        if len(valid_boxes) < 2:
            return ProductDetectionResult(image=image, crops=(image,), boxes=(), labels=(), detection_count=len(valid_boxes), used_contact_sheet=False)

        polygons = prediction.masks.xy if prediction.masks is not None else []
        classes = self._classes()
        records = []
        for index, box in enumerate(raw_boxes):
            clipped = (max(0, box[0]), max(0, box[1]), min(original.width, box[2]), min(original.height, box[3]))
            if clipped not in valid_boxes:
                continue
            class_id = int(prediction.boxes.cls[index])
            polygon = polygons[index].tolist() if index < len(polygons) else []
            records.append((clipped, classes[class_id], polygon))
        records.sort(key=lambda record: (record[0][1] // max(1, original.height // 4), record[0][0]))

        crops = [self._masked_crop(original, box, polygon) for box, _, polygon in records]
        loaded_crops = tuple(self._encode_crop(crop) for crop in crops)
        prepared = self._contact_sheet(crops)
        output = io.BytesIO()
        prepared.save(output, format="JPEG", quality=92, optimize=True)
        content = output.getvalue()
        return ProductDetectionResult(
            image=LoadedImage(content=content, media_type="image/jpeg", width=prepared.width, height=prepared.height, sha256=hashlib.sha256(content).hexdigest()),
            crops=loaded_crops,
            boxes=tuple(record[0] for record in records),
            labels=tuple(record[1] for record in records),
            detection_count=len(records),
            used_contact_sheet=True,
        )

    @staticmethod
    # 원본 좌표의 마스크 다각형으로 상품만 남긴 흰 배경 crop을 반환한다.
    def _masked_crop(original: Image.Image, box: tuple[int, int, int, int], polygon: list[list[float]]) -> Image.Image:
        crop = original.crop(box)
        if len(polygon) < 3:
            return crop
        mask = Image.new("L", crop.size, 0)
        points = [(round(point[0] - box[0]), round(point[1] - box[1])) for point in polygon]
        ImageDraw.Draw(mask).polygon(points, fill=255)
        background = Image.new("RGB", crop.size, "white")
        background.paste(crop, mask=mask)
        return background
