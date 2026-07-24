"""적응형 스레드 스케일링·느린 파트 판정·속도 계산 규칙 박제 (#73 착수 조건 가).

이 테스트는 다운로드 엔진 이주(QThread → threading) 전후 모두 통과해야 한다.
규칙 자체를 고정하는 것이 목적이므로, 시나리오와 단언은 이주 후에도 바꾸지 않는다.
이주 시에는 아래 팩토리(_make_scaler/_make_engine)의 대상 클래스만 교체한다.

박제하는 규칙 (현행 동작 그대로):
- 스레드 증가: 활성 스레드당 평균 속도 > 4 MB/s 틱마다 adjust_count += 1,
  adjust_count > 1이면 adjust_threads를 +4 (max_threads 상한), 카운터 리셋
- 스레드 감소: 평균 속도 < 2 MB/s 틱마다 adjust_count -= 1,
  adjust_count < -4이면 adjust_threads를 절반으로 (하한 1), 카운터 리셋
- 중간 대역(2~4 MB/s)에서는 adjust_count가 0을 향해 1씩 감쇠
- 속도 계산: 직전 틱 대비 바이트 증가량을 MB/s로 환산, prev_size 갱신
- 느린 파트: 청크 속도 < 100 KB/s가 연속 6회(slow_count > 5) 누적되면
  해당 파트를 중단하고 구간을 재큐잉(restart_threads += 1), 빠른 청크가 오면 리셋
- 파트 실패: 요청 예외 시 구간 재큐잉(failed_threads += 1)
"""

import threading

import pytest
import requests

from core.models.download_state import DownloadState
from download.data import DownloadData

MB = 1024 * 1024


class RecordingLogger:
    """DownloadLogger 호환 가짜 로거 — 실제 파일을 만들지 않고 호출만 기록한다."""

    def __init__(self):
        self.adjust_calls: list[tuple] = []
        self.debug_calls: list[tuple] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.thread_completes: list[tuple] = []

    def log_thread_adjust(self, active_threads, avg_speed):
        self.adjust_calls.append((active_threads, avg_speed))

    def log_thread_debug(self, active_threads, download_speed, avg_speed):
        self.debug_calls.append((active_threads, download_speed, avg_speed))

    def log_thread_start(self, thread_id, start, end):
        pass

    def log_thread_complete(self, thread_id, downloaded_size):
        self.thread_completes.append((thread_id, downloaded_size))

    def log_error(self, message, exception=None):
        self.errors.append(message)

    def warning(self, message):
        self.warnings.append(message)


class FakeTask:
    """DownloadTask 호환 가짜 태스크 — 엔진이 쓰는 인터페이스(state/lock/data/logger)만 제공."""

    def __init__(self, data: DownloadData, logger: RecordingLogger):
        self.data = data
        self.logger = logger
        self.lock = threading.Lock()

    @property
    def state(self) -> DownloadState:
        return self.data.model.state


def _make_data(output_path: str = "unused.part") -> DownloadData:
    """테스트용 DownloadData를 만든다 (네트워크 정보는 더미)."""
    return DownloadData(
        base_url="https://example.invalid/video.mp4",
        vod_url="https://chzzk.naver.com/video/1",
        output_path=output_path,
        resolution=1080,
        content_type="video",
    )


def _make_scaler(data: DownloadData, logger: RecordingLogger):
    """스케일링 규칙(_adjust_threads/measure_speed) 보유 객체를 만든다.

    이주 전: download.monitor.MonitorThread / 이주 후: core FileDownloader.
    반환 객체는 adjust_count 속성과 두 메서드를 노출해야 한다.
    """
    from download.monitor import MonitorThread

    return MonitorThread(FakeTask(data, logger))


def _make_engine(data: DownloadData, logger: RecordingLogger):
    """파트 다운로드 로직(_download_part) 보유 객체와 (엔진, 태스크)를 만든다.

    이주 전: download.download.DownloadThread / 이주 후: core FileDownloader.
    """
    from download.download import DownloadThread

    task = FakeTask(data, logger)
    return DownloadThread(task), task


def _engine_module():
    """_download_part가 사는 모듈 — 시간·세션 몽키패치 대상."""
    import download.download as mod

    return mod


# ================================================================ 스레드 증가/감소


def test_fast_two_ticks_increase_threads_by_4():
    """평균 > 4 MB/s 틱 2회 → adjust_count 2(>1) → 스레드 +4, 카운터 리셋."""
    data, logger = _make_data(), RecordingLogger()
    data.max_threads = 32
    scaler = _make_scaler(data, logger)

    data.future_count = 4
    data.speed_mb = 20.0  # 평균 5 MB/s
    scaler._adjust_threads()
    assert (scaler.adjust_count, data.adjust_threads) == (1, 4)  # 1틱으로는 불변

    scaler._adjust_threads()
    assert data.adjust_threads == 8
    assert scaler.adjust_count == 0
    assert logger.adjust_calls == [(8, 20.0)]


def test_increase_is_capped_at_max_threads():
    """증가 시 상한은 max_threads다."""
    data, logger = _make_data(), RecordingLogger()
    data.max_threads = 10
    data.adjust_threads = 8
    scaler = _make_scaler(data, logger)

    data.future_count = 8
    data.speed_mb = 40.0  # 평균 5 MB/s
    scaler._adjust_threads()
    scaler._adjust_threads()

    assert data.adjust_threads == 10  # 8+4=12가 아니라 상한 10


def test_slow_five_ticks_halve_threads():
    """평균 < 2 MB/s 틱 5회 → adjust_count -5(<-4) → 스레드 절반, 카운터 리셋."""
    data, logger = _make_data(), RecordingLogger()
    data.max_threads = 32
    data.adjust_threads = 8
    scaler = _make_scaler(data, logger)

    data.future_count = 8
    data.speed_mb = 8.0  # 평균 1 MB/s
    for _ in range(4):
        scaler._adjust_threads()
    assert data.adjust_threads == 8  # 4틱까지는 불변 (-4는 경계 미달)

    scaler._adjust_threads()
    assert data.adjust_threads == 4
    assert scaler.adjust_count == 0
    assert logger.adjust_calls == [(4, 8.0)]


def test_halving_floor_is_one_thread():
    """감소 시 하한은 1스레드다."""
    data, logger = _make_data(), RecordingLogger()
    data.adjust_threads = 1
    scaler = _make_scaler(data, logger)

    data.future_count = 1
    data.speed_mb = 0.5
    for _ in range(5):
        scaler._adjust_threads()

    assert data.adjust_threads == 1


def test_middle_band_decays_adjust_count_toward_zero():
    """중간 대역(2~4 MB/s)은 누적 카운터를 0으로 1씩 되돌린다 (히스테리시스)."""
    data, logger = _make_data(), RecordingLogger()
    data.max_threads = 32
    scaler = _make_scaler(data, logger)

    data.future_count = 4
    data.speed_mb = 20.0  # 평균 5 → +1
    scaler._adjust_threads()
    assert scaler.adjust_count == 1

    data.speed_mb = 12.0  # 평균 3 → 감쇠 -1
    scaler._adjust_threads()
    assert scaler.adjust_count == 0
    assert data.adjust_threads == 4  # 임계 미달로 불변

    data.speed_mb = 4.0  # 평균 1 → -1
    scaler._adjust_threads()
    assert scaler.adjust_count == -1
    data.speed_mb = 12.0  # 평균 3 → 감쇠 +1
    scaler._adjust_threads()
    assert scaler.adjust_count == 0


def test_band_boundaries_are_exclusive():
    """경계값 4 MB/s·2 MB/s는 중간 대역이다 (초과/미만 판정)."""
    data, logger = _make_data(), RecordingLogger()
    scaler = _make_scaler(data, logger)

    data.future_count = 2
    data.speed_mb = 8.0  # 평균 정확히 4 → 증가 아님
    scaler._adjust_threads()
    assert scaler.adjust_count == 0

    data.speed_mb = 4.0  # 평균 정확히 2 → 감소 아님
    scaler._adjust_threads()
    assert scaler.adjust_count == 0


def test_zero_active_threads_counts_as_slow_tick():
    """활성 스레드 0이면 평균 0으로 간주되어 감소 방향 틱이다."""
    data, logger = _make_data(), RecordingLogger()
    scaler = _make_scaler(data, logger)

    data.future_count = 0
    data.speed_mb = 10.0
    scaler._adjust_threads()

    assert scaler.adjust_count == -1


# ================================================================ 속도 계산


def test_measure_speed_uses_delta_from_previous_tick():
    """속도 = (현재 누적 - 직전 누적) 바이트를 MB/s로 환산, prev_size 갱신."""
    data, logger = _make_data(), RecordingLogger()
    scaler = _make_scaler(data, logger)

    data.total_downloaded_size = 5 * MB
    data.prev_size = 2 * MB
    data.future_count = 3

    scaler.measure_speed()

    assert data.speed_mb == pytest.approx(3.0)
    assert data.prev_size == 5 * MB
    assert logger.debug_calls == [(3, pytest.approx(3.0), pytest.approx(1.0))]


def test_measure_speed_with_no_active_threads_reports_zero_average():
    """활성 스레드 0이면 평균 속도는 0으로 기록한다 (0 나눗셈 금지)."""
    data, logger = _make_data(), RecordingLogger()
    scaler = _make_scaler(data, logger)

    data.total_downloaded_size = 1 * MB
    data.prev_size = 0
    data.future_count = 0

    scaler.measure_speed()

    assert logger.debug_calls == [(0, pytest.approx(1.0), 0)]


# ================================================================ 느린 파트·실패 처리


class FakeResponse:
    """스트리밍 응답 흉내 — 지정한 청크 목록을 그대로 흘린다."""

    def __init__(self, chunks):
        self._chunks = chunks

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=8192):
        yield from self._chunks


class FakeSession:
    """get_thread_session() 대체 — 준비된 응답을 반환하거나 예외를 던진다."""

    def __init__(self, response=None, exception=None):
        self._response = response
        self._exception = exception

    def get(self, url, **kwargs):
        if self._exception is not None:
            raise self._exception
        return self._response


class TickingClock:
    """time.time 대체 — 호출마다 지정 간격으로 흐르는 가짜 시계."""

    def __init__(self, step: float):
        self.now = 1_000_000.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def _prepare_running_engine(tmp_path, monkeypatch, chunks=None, exception=None, clock_step=1.0):
    """RUNNING 상태의 엔진과 부속(데이터·로거)을 준비한다.

    clock_step=1.0이면 청크당 1초가 흘러 항상 저속(<100 KB/s) 판정,
    아주 작은 값이면 항상 고속 판정이 난다.
    """
    output = tmp_path / "part.bin"
    output.write_bytes(b"")

    data = _make_data(str(output))
    logger = RecordingLogger()
    engine, task = _make_engine(data, logger)

    data.model.start()  # RUNNING 상태로 진입
    data.threads_progress = [0] * 4
    data.remaining_ranges = []

    mod = _engine_module()
    monkeypatch.setattr(
        mod, "get_thread_session", lambda: FakeSession(FakeResponse(chunks or []), exception)
    )
    monkeypatch.setattr(mod.tm, "time", TickingClock(clock_step))
    return engine, data, logger


def test_slow_part_restarts_after_six_slow_chunks(tmp_path, monkeypatch):
    """청크 속도 < 100 KB/s 연속 6회(slow_count > 5)면 파트를 중단·재큐잉한다."""
    chunks = [b"x" * 8192] * 10  # 1초/청크 → 8 KB/s로 항상 저속
    engine, data, logger = _prepare_running_engine(tmp_path, monkeypatch, chunks=chunks, clock_step=1.0)

    returned = engine._download_part(0, 40 * MB - 1, 0, 40 * MB)

    assert returned == 0
    assert data.restart_threads == 1
    assert data.remaining_ranges == [(0, 40 * MB - 1)]  # 같은 구간 재큐잉
    assert data.threads_progress[0] == 0  # 진행 바이트 롤백
    assert data.completed_threads == 0
    assert any("slow" in w for w in logger.warnings)


def test_fast_part_completes_and_accumulates_progress(tmp_path, monkeypatch):
    """정상 속도면 파트를 완주하고 완료 카운터·누적 진행에 반영한다."""
    part_size = 3 * 8192
    chunks = [b"x" * 8192] * 3
    engine, data, logger = _prepare_running_engine(
        tmp_path, monkeypatch, chunks=chunks, clock_step=1e-6
    )

    returned = engine._download_part(0, part_size - 1, 0, part_size)

    assert returned == 0
    assert data.completed_threads == 1
    assert data.completed_progress == part_size
    assert data.restart_threads == 0
    assert data.remaining_ranges == []
    assert logger.thread_completes == [(0, part_size)]


def test_request_exception_requeues_range_as_failed(tmp_path, monkeypatch):
    """요청 예외 시 실패 카운터를 올리고 같은 구간을 재큐잉한다."""
    engine, data, logger = _prepare_running_engine(
        tmp_path, monkeypatch, exception=requests.ConnectionError("boom")
    )

    returned = engine._download_part(0, MB - 1, 0, MB)

    assert returned == 0
    assert data.failed_threads == 1
    assert data.remaining_ranges == [(0, MB - 1)]
    assert data.threads_progress[0] == 0
    assert len(logger.errors) == 1
