import asyncio
import hashlib
import io
from pathlib import Path, PurePosixPath

from PIL import Image, UnidentifiedImageError

from app.config import Settings
from app.domain.models import LoadedImage
from app.errors import DependencyUnavailable, ImageTooLarge, InvalidRequest, UnsupportedImageType

ALLOWED_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


class ConfiguredImageLoader:
    # 이미지 원천·크기·S3 제한이 포함된 설정을 받아 로더를 구성한다.
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # 객체 키와 기대 해시를 받아 파일을 읽고 검증된 이미지 메타데이터와 bytes를 반환한다.
    async def load(self, object_key: str, expected_sha256: str) -> LoadedImage:
        self._validate_object_key(object_key)
        if self._settings.image_source == "s3":
            content = await asyncio.to_thread(self._read_s3, object_key)
        else:
            content = await asyncio.to_thread(self._read_local, object_key)
        return await asyncio.to_thread(self.validate_image, content, expected_sha256)

    # 객체 키가 상대 경로와 허용된 S3 prefix인지 검증하며 성공 시 반환한다.
    def _validate_object_key(self, object_key: str) -> None:
        key = PurePosixPath(object_key)
        if key.is_absolute() or ".." in key.parts:
            raise InvalidRequest("허용되지 않은 image.objectKey입니다.")
        if self._settings.image_source == "s3" and not object_key.startswith(
            self._settings.s3_allowed_prefix
        ):
            raise InvalidRequest("허용되지 않은 S3 객체 경로입니다.")

    # 로컬 루트 내부 객체 키를 받아 이미지 파일 bytes를 반환한다.
    def _read_local(self, object_key: str) -> bytes:
        root = Path(self._settings.local_image_root).resolve()
        path = (root / object_key).resolve()
        if root not in path.parents:
            raise InvalidRequest("허용되지 않은 로컬 이미지 경로입니다.")
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise InvalidRequest("이미지 객체를 찾을 수 없습니다.") from exc
        except OSError as exc:
            raise DependencyUnavailable("이미지 파일을 읽지 못했습니다.") from exc

    # 고정 S3 버킷의 객체 키를 받아 제한 크기까지의 이미지 bytes를 반환한다.
    def _read_s3(self, object_key: str) -> bytes:
        try:
            import boto3

            client = boto3.client("s3", region_name=self._settings.s3_region)
            response = client.get_object(Bucket=self._settings.s3_bucket, Key=object_key)
            return response["Body"].read(self._settings.max_image_bytes + 1)
        except Exception as exc:
            raise DependencyUnavailable("S3 이미지 객체를 읽지 못했습니다.") from exc

    # 이미지 bytes와 기대 해시를 검증해 형식·크기 정보가 포함된 LoadedImage를 반환한다.
    def validate_image(self, content: bytes, expected_sha256: str) -> LoadedImage:
        if len(content) > self._settings.max_image_bytes:
            raise ImageTooLarge()
        digest = hashlib.sha256(content).hexdigest()
        if digest.lower() != expected_sha256.lower():
            raise InvalidRequest("image.sha256이 실제 이미지와 일치하지 않습니다.")
        try:
            with Image.open(io.BytesIO(content)) as image:
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                image_format = image.format or ""
                width, height = image.size
        except (UnidentifiedImageError, OSError) as exc:
            raise UnsupportedImageType() from exc
        if image_format not in ALLOWED_FORMATS:
            raise UnsupportedImageType()
        if width * height > self._settings.max_image_pixels:
            raise ImageTooLarge()
        return LoadedImage(
            content=content,
            media_type=ALLOWED_FORMATS[image_format],
            width=width,
            height=height,
            sha256=digest,
        )
