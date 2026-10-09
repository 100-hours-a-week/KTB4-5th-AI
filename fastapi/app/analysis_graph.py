import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from app.errors import AppError
from app.pipeline import ReceiptAnalysisPipeline
from app.schemas import AnalysisResult, InternalStage
from app.store import AnalysisRecord

logger = logging.getLogger(__name__)

StageSetter = Callable[[InternalStage], Awaitable[None]]


class AnalysisGraphState(TypedDict, total=False):
    record: AnalysisRecord
    set_stage: StageSetter
    attempt: int
    result: AnalysisResult | None
    error: AppError | None


class AnalysisGraph:
    # 분석 파이프라인을 LangGraph 노드로 감싸고 재시도 가능한 실패만 지정 횟수만큼 다시 실행한다.
    def __init__(self, pipeline: ReceiptAnalysisPipeline, retry_count: int = 1, retry_delay_seconds: float = 1.0) -> None:
        self.pipeline = pipeline
        self.retry_count = retry_count
        self.retry_delay_seconds = retry_delay_seconds

        builder = StateGraph(AnalysisGraphState)
        builder.add_node("analyze", self._analyze)
        builder.add_node("retry_wait", self._retry_wait)
        builder.add_edge(START, "analyze")
        builder.add_conditional_edges("analyze", self._route_after_analysis, {"retry": "retry_wait", "finish": END})
        builder.add_edge("retry_wait", "analyze")
        self.graph = builder.compile()

    # 분석 레코드와 단계 콜백을 받아 그래프를 실행하고 성공 결과를 반환하며 최종 실패는 다시 발생시킨다.
    async def run(self, record: AnalysisRecord, set_stage: StageSetter) -> AnalysisResult:
        state = await self.graph.ainvoke(
            {
                "record": record,
                "set_stage": set_stage,
                "attempt": 0,
                "result": None,
                "error": None,
            }
        )
        error = state.get("error")
        result = state.get("result")

        if error is not None:
            raise error
        if result is None:
            raise RuntimeError("분석 그래프가 결과 없이 종료되었습니다.")
        return result

    # 파이프라인을 한 번 실행하고 성공 결과 또는 AppError를 그래프 상태에 기록한다.
    async def _analyze(self, state: AnalysisGraphState) -> AnalysisGraphState:
        attempt = state.get("attempt", 0) + 1
        record = state["record"]
        logger.info(
            "analysis.graph.attempt_started analysis_id=%s attempt=%s max_attempts=%s",
            record.analysis_id,
            attempt,
            self.retry_count + 1,
        )

        try:
            result = await self.pipeline.run(record, state["set_stage"])
        except AppError as error:
            logger.warning(
                "analysis.graph.attempt_failed analysis_id=%s attempt=%s error_code=%s retryable=%s",
                record.analysis_id,
                attempt,
                error.code,
                error.retryable,
            )
            return {**state, "attempt": attempt, "result": None, "error": error}

        logger.info(
            "analysis.graph.attempt_succeeded analysis_id=%s attempt=%s",
            record.analysis_id,
            attempt,
        )
        return {**state, "attempt": attempt, "result": result, "error": None}

    # 현재 오류의 재시도 가능 여부와 남은 횟수를 보고 재실행 또는 종료 경로를 선택한다.
    def _route_after_analysis(self, state: AnalysisGraphState) -> Literal["retry", "finish"]:
        error = state.get("error")
        if error is not None and error.retryable and state.get("attempt", 0) <= self.retry_count:
            return "retry"
        return "finish"

    # 설정된 짧은 대기 후 동일 이미지를 전체 파이프라인에 한 번 더 전달한다.
    async def _retry_wait(self, state: AnalysisGraphState) -> AnalysisGraphState:
        record = state["record"]
        logger.info(
            "analysis.graph.retry_scheduled analysis_id=%s next_attempt=%s delay_seconds=%s",
            record.analysis_id,
            state.get("attempt", 0) + 1,
            self.retry_delay_seconds,
        )
        if self.retry_delay_seconds > 0:
            await asyncio.sleep(self.retry_delay_seconds)
        return state
