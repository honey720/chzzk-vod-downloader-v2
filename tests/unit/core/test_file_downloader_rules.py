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

import pytest
import requests

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
    from core.downloaders.file_downloader import FileDownloader

    return FileDownloader(data, logger)


def _make_engine(data: DownloadData, logger: RecordingLogger):
    """파트 다운로드 로직(_download_part) 보유 객체와 (엔진, 태스크)를 만든다.

    이주 전: download.download.DownloadThread / 이주 후: core FileDownloader
    (태스크 어댑터가 더 이상 필요 없어 None을 돌려준다).
    """
    from core.downloaders.file_downloader import FileDownloader

    return FileDownloader(data, logger), None


def _engine_module():
    """_download_part가 사는 모듈 — 시간·세션 몽키패치 대상."""
    import core.downloaders.file_downloader as mod

    return mod


# ================================================================ 스레드 조정 (#112 — 총 처리량 등반)
#
# 구 규칙(스레드당 평균 속도 vs 고정 임계 4/2 MB/s)의 박제는 #112로 폐기했다.
# 판단 신호를 총 처리량의 실제 증감으로 바꾼 근거는 base._adjust_threads
# docstring과 m3u8 규칙 테스트의 섹션 주석 참조 (파일 경로도 같은 base 규칙을 쓴다).


def _tick(scaler, data, speed: float) -> None:
    """1초 관측 틱을 흉내 낸다 — 측정된 총 처리량 반영 후 조정 1회."""
    data.speed_mb = speed
    scaler._adjust_threads()


def test_warmup_without_measurement_does_nothing():
    """첫 유효 측정 전(속도 0)에는 조정하지 않는다 — 시작 직후 오판 방지."""
    data, logger = _make_data(), RecordingLogger()
    scaler = _make_scaler(data, logger)

    _tick(scaler, data, 0.0)

    assert data.adjust_threads == 4
    assert logger.adjust_calls == []


def test_first_valid_tick_probes_up_by_4():
    """첫 유효 측정에서 +4 탐침을 시작한다 (구 규칙의 +4 보폭 유지)."""
    data, logger = _make_data(), RecordingLogger()
    data.max_threads = 32
    scaler = _make_scaler(data, logger)

    _tick(scaler, data, 3.9)

    assert data.adjust_threads == 8
    assert logger.adjust_calls == [(8, 3.9)]


def test_climbs_while_throughput_grows_and_caps_at_max_threads():
    """총 처리량이 느는 동안 +4 계단으로 오르되 상한은 min(max_threads, 48)이다."""
    data, logger = _make_data(), RecordingLogger()
    data.max_threads = 10
    scaler = _make_scaler(data, logger)

    for _ in range(10):
        _tick(scaler, data, data.adjust_threads * 0.95)

    assert data.adjust_threads == 10  # 8+4=12가 아니라 상한 10


def test_reverts_and_holds_when_raise_does_not_increase_throughput():
    """인상해도 총 처리량이 늘지 않으면 되물리고 그 지점을 상한으로 정체한다."""
    data, logger = _make_data(), RecordingLogger()
    data.max_threads = 32
    scaler = _make_scaler(data, logger)

    _tick(scaler, data, 5.0)  # 4→8 (기준 5.0)
    _tick(scaler, data, 5.0)  # settle 1
    _tick(scaler, data, 5.0)  # settle 2
    _tick(scaler, data, 5.2)  # 요구치(5.0×1.125=5.625) 미달 → 8→4 되물림 + 정체

    assert data.adjust_threads == 4
    assert logger.adjust_calls == [(8, 5.0), (4, 5.2)]


def test_sustained_collapse_halves_after_five_ticks():
    """정체 기준의 절반 미만이 5틱 지속되면 절반으로 줄인다 (관성은 구 규칙과 동일)."""
    data, logger = _make_data(), RecordingLogger()
    data.max_threads = 32
    scaler = _make_scaler(data, logger)

    _tick(scaler, data, 5.0)  # 4→8
    _tick(scaler, data, 9.0)  # settle 1
    _tick(scaler, data, 9.0)  # settle 2
    _tick(scaler, data, 9.0)  # 요구치(5.0×1.125) 초과 → 8→12 (기준 9.0)
    _tick(scaler, data, 9.2)  # settle 1
    _tick(scaler, data, 9.2)  # settle 2
    _tick(scaler, data, 9.2)  # 요구치(9.0×1.0417=9.375) 미달 → 12→8 되물림 + 정체 (기준 9.2)
    assert data.adjust_threads == 8

    for _ in range(4):  # 기준의 절반(4.6) 미만 4틱 — 아직 불변
        _tick(scaler, data, 4.0)
    assert data.adjust_threads == 8

    _tick(scaler, data, 4.0)  # 5틱째 — 절반으로
    assert data.adjust_threads == 4


def test_halving_floor_is_one_thread():
    """처리량 하락이 이어지면 절반씩 1까지 내려가고, 그 밑으로는 내려가지 않는다."""
    data, logger = _make_data(), RecordingLogger()
    data.max_threads = 32
    scaler = _make_scaler(data, logger)

    _tick(scaler, data, 5.0)  # 4→8
    _tick(scaler, data, 5.0)  # settle 1
    _tick(scaler, data, 5.0)  # settle 2
    _tick(scaler, data, 5.0)  # 요구치 미달 → 되물림 → 4 정체 (기준 5.0)
    assert data.adjust_threads == 4

    for _ in range(7):  # 기준(5.0)의 절반 미만 지속 → 4→2 (새 기준 2.0)
        _tick(scaler, data, 2.0)
    assert data.adjust_threads == 2

    for _ in range(7):  # 또 절반 미만으로 하락 → 2→1
        _tick(scaler, data, 0.9)
    assert data.adjust_threads == 1

    for _ in range(10):  # 더 하락해도 1 밑으로는 내려가지 않는다
        _tick(scaler, data, 0.1)
    assert data.adjust_threads == 1


def test_zero_active_sample_is_not_a_slow_signal():
    """관측 순간 활성 0이어도 총 처리량이 신호다 — 구 규칙 붕괴 원인의 반전 박제 (#112)."""
    data, logger = _make_data(), RecordingLogger()
    data.max_threads = 32
    scaler = _make_scaler(data, logger)

    data.future_count = 0  # 빈 슬롯 순간에 관측됐다
    _tick(scaler, data, 3.9)

    assert data.adjust_threads == 8  # 감소가 아니라 정상 탐침


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
    engine, data, logger = _prepare_running_engine(
        tmp_path, monkeypatch, chunks=chunks, clock_step=1.0
    )

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


# ================================================================ 파트 이어받기 (#78)


class ResumeResponse(FakeResponse):
    """status_code를 가진 스트리밍 응답 — 이어받기(206) 검증용."""

    def __init__(self, chunks, status_code=206):
        super().__init__(chunks)
        self.status_code = status_code

    def close(self):
        pass


class RecordingSession:
    """요청별 Range 헤더를 기록하고 준비된 응답을 순서대로 돌려준다 (#78)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.range_headers: list[str] = []

    def get(self, url, headers=None, **kwargs):
        self.range_headers.append(headers["Range"])
        return self._responses.pop(0)


CHUNK = 8192
PART_END = 3 * CHUNK - 1  # 3청크짜리 파트 (0 ~ 24575)


def _prepare_resume_engine(tmp_path, monkeypatch, responses, prewritten: bytes = b""):
    """이어받기 시나리오용 엔진 — 산출물 선기록과 응답 시퀀스를 주입한다."""
    output = tmp_path / "part.bin"
    output.write_bytes(prewritten)

    data = _make_data(str(output))
    logger = RecordingLogger()
    engine, _task = _make_engine(data, logger)

    data.model.start()
    data.threads_progress = [0] * 4
    data.remaining_ranges = []

    mod = _engine_module()
    session = RecordingSession(responses)
    monkeypatch.setattr(mod, "get_thread_session", lambda: session)
    monkeypatch.setattr(mod.tm, "time", TickingClock(1e-6))  # 항상 고속 판정
    return engine, data, logger, session, output


def test_requeued_part_resumes_with_range(tmp_path, monkeypatch):
    """재큐잉된 파트는 이미 받은 바이트 뒤에서 Range로 이어받는다 (#78).

    완료 시 누적 진행은 이어받은 바이트를 포함한 파트 전체여야 한다.
    """
    engine, data, logger, session, output = _prepare_resume_engine(
        tmp_path,
        monkeypatch,
        responses=[ResumeResponse([b"y" * CHUNK], status_code=206)],
        prewritten=b"x" * (2 * CHUNK),  # 앞선 시도가 2청크를 받아 둔 상태
    )
    engine._record_partial(0, PART_END, 2 * CHUNK)

    returned = engine._download_part(0, PART_END, 0, 3 * CHUNK)

    assert returned == 0
    assert session.range_headers == [f"bytes={2 * CHUNK}-{PART_END}"]  # 이어받기 요청
    assert data.completed_threads == 1
    assert data.completed_progress == 3 * CHUNK  # 이어받은 바이트 포함 전체
    assert logger.thread_completes == [(0, 3 * CHUNK)]
    assert output.read_bytes() == b"x" * (2 * CHUNK) + b"y" * CHUNK  # 기존 바이트 보존
    assert (0, PART_END) not in engine._part_progress  # 완료 후 기록 정리


def test_resume_rejected_falls_back_to_full_retry(tmp_path, monkeypatch):
    """완료 조건: 서버가 이어받기 Range를 존중하지 않으면(200) 전체 재시도로 폴백한다."""
    engine, data, logger, session, output = _prepare_resume_engine(
        tmp_path,
        monkeypatch,
        responses=[
            ResumeResponse([], status_code=200),  # 이어받기 거부 — 전체 응답
            ResumeResponse([b"z" * CHUNK] * 3, status_code=200),  # 처음부터 전체 파트
        ],
        prewritten=b"x" * (2 * CHUNK),
    )
    engine._record_partial(0, PART_END, 2 * CHUNK)

    returned = engine._download_part(0, PART_END, 0, 3 * CHUNK)

    assert returned == 0
    assert session.range_headers == [
        f"bytes={2 * CHUNK}-{PART_END}",  # 이어받기 시도
        f"bytes=0-{PART_END}",  # 거부 후 파트 처음부터
    ]
    assert any("resume rejected" in w for w in logger.warnings)
    assert data.completed_progress == 3 * CHUNK
    assert output.read_bytes() == b"z" * (3 * CHUNK)  # 전체를 새로 받았다


def test_resume_integrity_mismatch_restarts_from_start(tmp_path, monkeypatch):
    """완료 조건: 기록된 오프셋만큼 파일이 자라 있지 않으면 처음부터 받는다."""
    engine, data, logger, session, output = _prepare_resume_engine(
        tmp_path,
        monkeypatch,
        responses=[ResumeResponse([b"z" * CHUNK] * 3, status_code=206)],
        prewritten=b"",  # 기록(2청크)과 달리 파일이 비어 있다 — 쓰기 유실 상황
    )
    engine._record_partial(0, PART_END, 2 * CHUNK)

    returned = engine._download_part(0, PART_END, 0, 3 * CHUNK)

    assert returned == 0
    assert session.range_headers == [f"bytes=0-{PART_END}"]  # 이어받기 시도 없음
    assert (0, PART_END) not in engine._part_progress  # 무결성 실패 기록은 폐기된다
    assert data.completed_progress == 3 * CHUNK
    assert output.read_bytes() == b"z" * (3 * CHUNK)


def test_slow_requeue_records_partial_progress(tmp_path, monkeypatch):
    """저속 재큐잉 시 받아 쓴 바이트가 기록된다 — 다음 시도의 이어받기 근거 (#78).

    저속 판정 규칙(연속 6회 < 100 KB/s) 자체는 무변경이다 — 박제는
    test_slow_part_restarts_after_six_slow_chunks가 그대로 유지한다.
    """
    chunks = [b"x" * CHUNK] * 10  # 1초/청크 → 항상 저속
    engine, data, logger = _prepare_running_engine(
        tmp_path, monkeypatch, chunks=chunks, clock_step=1.0
    )

    engine._download_part(0, 40 * MB - 1, 0, 40 * MB)

    # 저속 판정은 6청크째에 나므로 그때까지 받은 바이트가 기록된다
    assert engine._part_progress[(0, 40 * MB - 1)] == 6 * CHUNK
