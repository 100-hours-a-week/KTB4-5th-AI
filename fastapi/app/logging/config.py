import logging
from datetime import UTC, date, datetime, timedelta
from logging.handlers import QueueHandler, QueueListener
from pathlib import Path
from queue import SimpleQueue
from typing import TextIO

from app.config import Settings

_log_queue: SimpleQueue[logging.LogRecord] | None = None
_queue_handler: QueueHandler | None = None
_file_handler: "DailyUtcFileHandler | None" = None
_queue_listener: QueueListener | None = None


class UtcIsoFormatter(logging.Formatter):
    # 로그 레코드 시각을 받아 millisecond 단위 UTC ISO-8601 문자열로 반환한다.
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")


class DailyUtcFileHandler(logging.Handler):
    # 로그 디렉터리와 보관 일수를 받아 UTC 날짜별 파일 handler를 구성한다.
    def __init__(self, log_dir: Path, retention_days: int) -> None:
        super().__init__()
        self.log_dir = log_dir
        self.retention_days = retention_days
        self.current_date: date | None = None
        self.stream: TextIO | None = None

    # 로그 레코드를 받아 해당 UTC 날짜 파일에 포맷한 한 줄을 기록한다.
    def emit(self, record: logging.LogRecord) -> None:
        try:
            record_date = datetime.fromtimestamp(record.created, UTC).date()
            if self.current_date != record_date or self.stream is None:
                self._open_for_date(record_date)
            assert self.stream is not None
            self.stream.write(self.format(record) + "\n")
            self.stream.flush()
        except Exception:
            self.handleError(record)

    # UTC 날짜를 받아 기존 stream을 닫고 yyyy-mm-dd.log stream을 연다.
    def _open_for_date(self, record_date: date) -> None:
        if self.stream is not None:
            self.stream.close()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.stream = (self.log_dir / f"{record_date.isoformat()}.log").open("a", encoding="utf-8")
        self.current_date = record_date
        self._prune(record_date)

    # 현재 UTC 날짜를 받아 보관기간보다 오래된 날짜 로그 파일을 제거한다.
    def _prune(self, current_date: date) -> None:
        oldest_date = current_date - timedelta(days=self.retention_days - 1)
        for path in self.log_dir.glob("????-??-??.log"):
            try:
                file_date = date.fromisoformat(path.stem)
                if file_date < oldest_date:
                    path.unlink()
            except (OSError, ValueError):
                continue

    # 열린 날짜 로그 stream을 닫고 logging handler 자원을 정리한다.
    def close(self) -> None:
        try:
            if self.stream is not None:
                self.stream.close()
                self.stream = None
        finally:
            super().close()


# 설정을 받아 비동기 큐 logger와 날짜별 writer thread를 시작하고 현재 파일 경로를 반환한다.
def configure_logging(settings: Settings) -> Path:
    global _file_handler, _log_queue, _queue_handler, _queue_listener

    log_dir = Path(settings.log_dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    utc_date = datetime.now(UTC).date().isoformat()
    log_path = log_dir / f"{utc_date}.log"

    root = logging.getLogger()
    root.setLevel(settings.log_level)
    if _queue_listener is not None:
        return log_path

    _log_queue = SimpleQueue()
    _queue_handler = QueueHandler(_log_queue)
    _queue_handler.name = "ktb-fastapi-queue"
    _queue_handler.setLevel(settings.log_level)

    _file_handler = DailyUtcFileHandler(log_dir, settings.log_retention_days)
    _file_handler.name = "ktb-fastapi-file-writer"
    _file_handler.setLevel(settings.log_level)
    _file_handler.setFormatter(UtcIsoFormatter("[%(asctime)s] %(levelname)s %(name)s %(message)s"))

    _queue_listener = QueueListener(
        _log_queue,
        _file_handler,
        respect_handler_level=True,
    )
    root.addHandler(_queue_handler)
    _queue_listener.start()
    return log_path


# 로그 listener가 큐를 모두 처리할 때까지 기다린 뒤 handler와 날짜 파일을 닫는다.
def shutdown_logging() -> None:
    global _file_handler, _log_queue, _queue_handler, _queue_listener

    root = logging.getLogger()
    if _queue_handler is not None:
        root.removeHandler(_queue_handler)
    if _queue_listener is not None:
        _queue_listener.stop()
    if _file_handler is not None:
        _file_handler.close()
    _log_queue = None
    _queue_handler = None
    _file_handler = None
    _queue_listener = None
