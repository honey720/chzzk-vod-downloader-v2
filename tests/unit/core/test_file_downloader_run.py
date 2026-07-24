"""FileDownloader.run 통합 검증 — 가짜 세션으로 전체 파이프라인 실행 (#73).

실제 네트워크 없이 Range 요청을 흉내 내는 세션으로 다음을 검증한다:
- 완주: 결과 파일이 바이트 단위로 원본과 동일, 완료 콜백 1회
- 일시정지 → 재개 → 완료: PAUSED 동안 진행 정지, 재개 후 완주,
  허용되지 않는 상태 전이(warning) 0건
- 중단(stop): 파일 삭제, 완료 콜백 없음
"""

import threading
import time


import core.downloaders.file_downloader as fd_module
from core.downloaders.file_downloader import FileDownloader
from core.models.download_state import DownloadState
from download.data import DownloadData

MB = 1024 * 1024
TOTAL_SIZE = 4 * MB  # 해상도 144 → 파트 1MB → 4파트


class RunLogger:
    """run() 경로 전체가 쓰는 로거 인터페이스를 기록만 하며 흉내 낸다."""

    def __init__(self):
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.completed_times: list[float] = []
        self.closed = 0

    def log_download_start(self, total_size, part_size, segments, initial_threads):
        pass

    def log_thread_start(self, thread_id, start, end):
        pass

    def log_thread_complete(self, thread_id, downloaded_size):
        pass

    def log_thread_adjust(self, active_threads, avg_speed):
        pass

    def log_thread_debug(self, active_threads, download_speed, avg_speed):
        pass

    def log_download_complete(self, total_time):
        self.completed_times.append(total_time)

    def log_error(self, message, exception=None):
        self.errors.append(message)

    def log_exception(self, message, exception=None):
        self.errors.append(message)

    def warning(self, message):
        self.warnings.append(message)

    def save_and_close(self):
        self.closed += 1


def _content_bytes(total: int) -> bytes:
    """결정적 내용의 가짜 파일 본문 — 바이트 단위 대조를 위해 주기 251 패턴을 쓴다."""
    pattern = bytes(range(251))
    repeats = total // len(pattern) + 1
    return (pattern * repeats)[:total]


CONTENT = _content_bytes(TOTAL_SIZE)


class RangeResponse:
    """Range 요청 구간을 청크로 흘리는 가짜 응답. throttle 초/청크로 속도를 조절한다."""

    def __init__(self, body: bytes, throttle: float):
        self._body = body
        self._throttle = throttle

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=8192):
        for i in range(0, len(self._body), chunk_size):
            if self._throttle:
                time.sleep(self._throttle)
            yield self._body[i : i + chunk_size]


class RangeSession:
    """head/get(Range)을 지원하는 가짜 세션 (requests.Session 최소 인터페이스)."""

    def __init__(self, content: bytes, throttle: float = 0.0):
        self._content = content
        self._throttle = throttle
        self.headers = {"content-length": str(len(content))}

    def head(self, url, **kwargs):
        return self

    # head() 응답 겸용: raise_for_status/headers만 쓰인다

    def raise_for_status(self):
        pass

    def get(self, url, headers=None, **kwargs):
        range_header = (headers or {})["Range"]  # "bytes=start-end"
        start, end = map(int, range_header.removeprefix("bytes=").split("-"))
        return RangeResponse(self._content[start : end + 1], self._throttle)


def _make_engine(tmp_path, monkeypatch, throttle: float = 0.0):
    """가짜 세션이 연결된 RUNNING 이전 상태의 엔진을 준비한다."""
    output = tmp_path / "out.mp4"
    data = DownloadData(
        base_url="https://example.invalid/video.mp4",
        vod_url="https://chzzk.naver.com/video/1",
        output_path=str(output),
        resolution=144,  # 파트 1MB
        content_type="video",
    )
    logger = RunLogger()
    session = RangeSession(CONTENT, throttle)
    monkeypatch.setattr(fd_module, "get_thread_session", lambda: session)

    finished = threading.Event()
    failures: list[BaseException] = []

    def on_finished():
        # 실제 어댑터 경로(manager.finish → task.finish)를 흉내 내 상태를 종결한다
        data.model.finish()
        finished.set()

    engine = FileDownloader(
        data,
        logger,
        on_finished=on_finished,
        on_failed=failures.append,
    )
    return engine, data, logger, output, finished, failures


def _run_in_thread(engine) -> threading.Thread:
    """엔진 run()을 별도 스레드에서 실행한다 (호출 규약: start() 후 run())."""
    thread = threading.Thread(target=engine.run, daemon=True)
    thread.start()
    return thread


def test_run_downloads_file_byte_exact(tmp_path, monkeypatch):
    """전체 파이프라인이 원본과 동일한 파일을 만들고 완료 콜백을 1회 호출한다."""
    engine, data, logger, output, finished, failures = _make_engine(tmp_path, monkeypatch)

    data.model.start()
    thread = _run_in_thread(engine)
    assert finished.wait(timeout=30), "완료 콜백이 호출되지 않았다"
    thread.join(timeout=10)

    assert output.read_bytes() == CONTENT
    assert failures == []
    assert logger.errors == []
    assert logger.warnings == []  # 느린 파트 오탐·전이 warning 없음
    assert logger.completed_times and logger.closed == 1
    assert data.completed_threads == 4  # 파트 4개 전부 정상 완료


def test_pause_resume_then_complete(tmp_path, monkeypatch):
    """일시정지 동안 진행이 멈추고, 재개 후 완주한다. 전이 warning 0건."""
    # 청크당 5ms 스로틀 → 4파트 병렬로 약 0.64초 — 중간에 일시정지할 여유를 만든다
    engine, data, logger, output, finished, failures = _make_engine(
        tmp_path, monkeypatch, throttle=0.005
    )

    data.model.start()
    thread = _run_in_thread(engine)

    time.sleep(0.25)  # 다운로드가 진행되는 중간 지점
    assert data.model.pause() is True  # RUNNING → PAUSED (유효 전이)
    assert data.model.state is DownloadState.PAUSED

    # 워커들이 pause_event 대기에 도달할 시간을 준 뒤 진행량이 고정되는지 확인
    time.sleep(0.3)
    frozen = data.total_downloaded_size
    snapshot = sum(data.threads_progress) + data.completed_progress
    time.sleep(0.5)
    assert sum(data.threads_progress) + data.completed_progress == snapshot
    assert data.total_downloaded_size == frozen

    assert data.model.resume() is True  # PAUSED → RUNNING (유효 전이)
    assert finished.wait(timeout=60), "재개 후 완료 콜백이 호출되지 않았다"
    thread.join(timeout=10)

    assert output.read_bytes() == CONTENT
    assert failures == []
    assert logger.warnings == []  # 상태 전이 warning 0건 (완료 조건)
    assert data.model.state is DownloadState.FINISHED


def test_stop_removes_partial_file(tmp_path, monkeypatch):
    """중단(stop)하면 부분 파일을 삭제하고 완료 콜백을 호출하지 않는다."""
    engine, data, logger, output, finished, failures = _make_engine(
        tmp_path, monkeypatch, throttle=0.005
    )

    data.model.start()
    thread = _run_in_thread(engine)

    time.sleep(0.25)
    data.model.stop()  # RUNNING → WAITING (취소)
    thread.join(timeout=30)
    assert not thread.is_alive()

    assert not output.exists()  # 부분 파일 삭제
    assert not finished.is_set()
    assert failures == []
