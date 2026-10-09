import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.errors import AnalysisNotFound, IdempotencyConflict
from app.schemas import (
    AnalysisAccepted,
    AnalysisCreateRequest,
    AnalysisResult,
    AnalysisView,
    ErrorBody,
    InternalStage,
    PublicStatus,
)


@dataclass(slots=True)
class AnalysisRecord:
    analysis_id: str
    request: AnalysisCreateRequest
    request_fingerprint: str
    status: PublicStatus
    stage: InternalStage
    submitted_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: AnalysisResult | None = None
    error: ErrorBody | None = None

    # 내부 분석 레코드를 API에 공개 가능한 AnalysisView로 변환해 반환한다.
    def to_view(self) -> AnalysisView:
        return AnalysisView(
            analysisId=self.analysis_id,
            status=self.status,
            stage=self.stage,
            submittedAt=self.submitted_at,
            startedAt=self.started_at,
            completedAt=self.completed_at,
            result=self.result,
            error=self.error,
        )


class InMemoryAnalysisStore:
    # 완료 결과 TTL과 최대 항목 수를 받아 프로세스 내부 저장소를 구성한다.
    def __init__(self, ttl_seconds: int, max_entries: int) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_entries = max_entries
        self._records: OrderedDict[str, AnalysisRecord] = OrderedDict()
        self._request_index: dict[str, str] = {}

    # 새 ID와 요청을 받아 멱등 레코드와 신규 생성 여부를 반환한다.
    def create_or_get(self, analysis_id: str, request: AnalysisCreateRequest) -> tuple[AnalysisRecord, bool]:
        fingerprint = self._fingerprint(request)
        existing_id = self._request_index.get(request.request_id)
        if existing_id:
            existing = self._records.get(existing_id)
            if existing is None:
                self._request_index.pop(request.request_id, None)
            elif existing.request_fingerprint != fingerprint:
                raise IdempotencyConflict()
            else:
                self._records.move_to_end(existing_id)
                return existing, False

        now = datetime.now(UTC)
        record = AnalysisRecord(
            analysis_id=analysis_id,
            request=request,
            request_fingerprint=fingerprint,
            status=PublicStatus.QUEUED,
            stage=InternalStage.QUEUED,
            submitted_at=now,
        )
        self._records[analysis_id] = record
        self._request_index[request.request_id] = analysis_id
        self.prune()
        return record, True

    # 내부 분석 레코드를 POST 접수 응답 형식으로 변환해 반환한다.
    def accepted(self, record: AnalysisRecord) -> AnalysisAccepted:
        return AnalysisAccepted(
            analysisId=record.analysis_id,
            status=record.status,
            submittedAt=record.submitted_at,
            pollAfterMs=0
            if record.status in {PublicStatus.COMPLETED, PublicStatus.FAILED}
            else 1000,
        )

    # 분석 ID를 받아 내부 레코드를 반환하고 없으면 ANALYSIS_NOT_FOUND를 발생시킨다.
    def get_record(self, analysis_id: str) -> AnalysisRecord:
        self.prune()
        record = self._records.get(analysis_id)
        if record is None:
            raise AnalysisNotFound()
        self._records.move_to_end(analysis_id)
        return record

    # 분석 ID를 받아 공개 상태·결과 view를 반환한다.
    def get_view(self, analysis_id: str) -> AnalysisView:
        return self.get_record(analysis_id).to_view()
    
    # 분석 ID와 연결된 레코드 및 requestId 색인을 제거하고 반환한다.
    def remove(self, analysis_id: str) -> None:
        record = self._records.pop(analysis_id, None)
        if record:
            self._request_index.pop(record.request.request_id, None)

    # 분석 ID의 상태를 PROCESSING으로 바꾸고 시작 시각을 기록한다.
    def mark_processing(self, analysis_id: str) -> None:
        record = self.get_record(analysis_id)
        record.status = PublicStatus.PROCESSING
        record.stage = InternalStage.CLASSIFYING
        record.started_at = datetime.now(UTC)

    # 분석 ID와 내부 단계를 받아 현재 처리 단계를 갱신한다.
    def set_stage(self, analysis_id: str, stage: InternalStage) -> None:
        self.get_record(analysis_id).stage = stage

    # 분석 ID와 결과를 받아 완료 상태로 저장하고 공개 view를 반환한다.
    def complete(self, analysis_id: str, result: AnalysisResult) -> AnalysisView:
        record = self.get_record(analysis_id)
        record.status = PublicStatus.COMPLETED
        record.stage = InternalStage.COMPLETED
        record.completed_at = datetime.now(UTC)
        record.result = result
        record.error = None
        return record.to_view()

    # 분석 ID와 오류를 받아 실패 상태로 저장하고 공개 view를 반환한다.
    def fail(self, analysis_id: str, error: ErrorBody) -> AnalysisView:
        record = self.get_record(analysis_id)
        record.status = PublicStatus.FAILED
        record.stage = InternalStage.FAILED
        record.completed_at = datetime.now(UTC)
        record.error = error
        record.result = None
        return record.to_view()

    # TTL이 지난 완료 결과와 최대 항목 수를 넘긴 오래된 완료 결과를 제거한다.
    def prune(self) -> None:
        now = datetime.now(UTC)
        expired = [
            analysis_id
            for analysis_id, record in self._records.items()
            if record.completed_at is not None and now - record.completed_at > self._ttl
        ]
        for analysis_id in expired:
            self.remove(analysis_id)

        while len(self._records) > self._max_entries:
            removable = next(
                (
                    analysis_id
                    for analysis_id, record in self._records.items()
                    if record.status in {PublicStatus.COMPLETED, PublicStatus.FAILED}
                ),
                None,
            )
            if removable is None:
                break
            self.remove(removable)

    @staticmethod
    # 정규화된 요청을 받아 멱등 비교에 사용할 SHA-256 fingerprint를 반환한다.
    def _fingerprint(request: AnalysisCreateRequest) -> str:
        canonical = request.model_dump(by_alias=True, mode="json")
        serialized = json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(serialized.encode()).hexdigest()
