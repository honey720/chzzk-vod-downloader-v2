"""일시정지한 시간이 속도 판정 · 로그에 섞이지 않고, 준비 뒤에는 일시정지 상태로 서고, 중단이 로그에 남는지 (#309).

- 받는 도중 일시정지한 시간은 그 파트의 속도 판정에서 빠진다 — 재개 직후 "slow speed"로 끊기지 않는다
- 준비하는 동안 일시정지됐으면 준비가 끝나도 받기를 띄우지 않고, 재개하면 그때 받는다
- 일시정지 중에는 스레드 수 · 속도 로그를 남기지 않는다
- 컷의 단계 시간에서 일시정지한 시간을 빼고, 그 합을 따로 넘긴다
- 중단하면 어느 단계였는지 로거에 알린다

네트워크는 가짜 세션(메모리의 바이트)이고, 컷은 실제 ffmpeg로 합성 영상을 자른다.
"""

import threading
import time

import core.downloaders.file_downloader as fd_module
from core.downloaders.file_downloader import FileDownloader
from core.models.download_state import DownloadState
from core.models.plan import TimeRange
from tests.unit.core.long_ffmpeg import end_all, record_processes
from tests.unit.core.test_file_downloader_run import (
    CONTENT,
    RunLogger,
    _make_engine,
    _run_in_thread,
)
from tests.unit.core.test_file_sections import (  # noqa: F401 — 모듈 범위 픽스처
    _Run,
    _requests_go_to_host,
    _seconds,
    server,
    sources,
)
from tests.unit.core.test_slow_requeue_disk_vs_network import CHUNK, _make_data, _RecordingLogger

PAUSE_SECONDS = 1.0  # 받는 도중 일시정지해 두는 시간


class _PausingResponse:
    """청크를 곧바로 흘리다가 정해 둔 청크 앞에서 일시정지를 걸고 PAUSE_SECONDS 뒤 재개한다."""

    def __init__(self, body: bytes, model, pause_before_chunk: int):
        self._body = body
        self._model = model
        self._pause_before_chunk = pause_before_chunk

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=CHUNK):
        for number, start in enumerate(range(0, len(self._body), chunk_size)):
            if number == self._pause_before_chunk:
                assert self._model.pause() is True, "전제: 받는 도중에 일시정지됐다"
                threading.Timer(PAUSE_SECONDS, self._model.resume).start()
            yield self._body[start : start + chunk_size]


class _PausingSession:
    def __init__(self, body: bytes, model, pause_before_chunk: int):
        self._args = (body, model, pause_before_chunk)

    def get(self, *args, **kwargs):
        return _PausingResponse(*self._args)


def test_time_spent_paused_is_left_out_of_a_parts_speed(tmp_path, monkeypatch):
    """파트를 받는 도중 일시정지한 시간은 그 파트의 속도 판정에 들지 않아야 한다 — 재개 뒤 느린 속도로 끊기지 않는다.

    12청크(8192바이트씩)를 곧바로 흘리는 응답. 둘째 청크 뒤에 일시정지하고 1초 뒤 재개
    (일시정지한 1초를 넣어 재면 재개 뒤의 청크 여섯이 모두 100KB/s 아래다 — 느린 속도 재큐의 조건)
    -> 파트가 끝까지 받아진다(completed_threads == 1), 느린 속도 재큐 0회, "slow speed" 경고 없음
    """
    body = b"x" * (CHUNK * 12)
    output = tmp_path / "part.bin"
    output.write_bytes(b"\x00" * len(body))
    data = _make_data(str(output))
    logger = _RecordingLogger()
    engine = FileDownloader(data, logger)
    monkeypatch.setattr(
        fd_module, "get_thread_session", lambda: _PausingSession(body, data.model, 2)
    )

    started = time.perf_counter()
    engine._download_part(0, len(body) - 1, 0, len(body))
    took = time.perf_counter() - started

    assert took >= PAUSE_SECONDS, "전제: 받는 도중 1초 동안 서 있었다"
    assert (data.completed_threads, data.restart_threads) == (1, 0)
    assert [w for w in logger.warnings if "slow speed" in w] == []
    assert output.read_bytes() == body


class _Recording(RunLogger):
    """엔진이 부른 것 가운데 이 파일이 재는 것을 시각과 함께 적는다."""

    def __init__(self):
        super().__init__()
        self.speed_lines: list[float] = []  # 스레드 수 · 속도 로그를 남긴 시각
        self.stopped: list[str] = []
        self.started_at: list[float] = []  # 받기 시작 로그(log_download_start)의 시각

    def log_download_start(self, *args):
        self.started_at.append(time.perf_counter())

    def log_thread_adjust(self, active_threads, avg_speed):
        self.speed_lines.append(time.perf_counter())

    def log_thread_debug(self, active_threads, download_speed, avg_speed):
        self.speed_lines.append(time.perf_counter())

    def log_stopped(self, phase):
        self.stopped.append(phase)


def _recording_engine(tmp_path, monkeypatch, throttle: float):
    engine, data, _logger, output, finished, failures = _make_engine(
        tmp_path, monkeypatch, throttle=throttle
    )
    logger = _Recording()
    engine.logger = logger
    engine._slow_speed_threshold_kb_s = (
        0  # 느리게 흘리는 가짜 세션 — 저속 규칙은 여기서 재지 않는다
    )
    return engine, data, logger, output, finished, failures


def test_engine_paused_while_preparing_starts_receiving_only_after_it_is_resumed(
    tmp_path, monkeypatch
):
    """준비하는 동안 일시정지됐으면 준비가 끝나도 받기를 띄우지 않고, 재개하면 그때 받아 끝내야 한다.

    준비(prepare)가 도는 동안 일시정지. 준비가 끝난 0.6초 뒤에 적고 재개
    -> 재개 전: 받은 바이트 0, 가짜 세션에 온 범위 요청 0건, 상태 PAUSED, 완료 통지 없음
    -> 재개 뒤: 완료, 산출물의 바이트 == 원본
    """
    engine, data, _logger, output, finished, failures = _recording_engine(
        tmp_path, monkeypatch, throttle=0.0
    )
    requests: list[str] = []
    real_session = fd_module.get_thread_session()

    class _Counting:
        headers = real_session.headers

        def head(self, url, **kwargs):
            return real_session.head(url, **kwargs)

        def get(self, url, headers=None, **kwargs):
            requests.append((headers or {}).get("Range", ""))
            return real_session.get(url, headers=headers, **kwargs)

    monkeypatch.setattr(fd_module, "get_thread_session", lambda: _Counting())
    real_prepare = engine.prepare
    prepared = threading.Event()

    def pausing_prepare(content):
        plan = real_prepare(content)
        assert data.model.pause() is True, "전제: 준비하는 동안 일시정지됐다"
        prepared.set()
        return plan

    monkeypatch.setattr(engine, "prepare", pausing_prepare)

    data.model.start()
    thread = _run_in_thread(engine)
    assert prepared.wait(10)
    time.sleep(0.6)
    before = (data.total_downloaded_size, list(requests), data.model.state, finished.is_set())
    data.model.resume()
    assert finished.wait(timeout=30), "재개 뒤 완료 통지가 오지 않았다"
    thread.join(timeout=10)

    assert before == (0, [], DownloadState.PAUSED, False)
    assert requests != [] and failures == []
    assert output.read_bytes() == CONTENT


def test_no_thread_or_speed_line_is_logged_while_paused(tmp_path, monkeypatch):
    """일시정지 중에는 스레드 수 · 속도 로그를 남기지 않아야 한다.

    청크마다 5ms 쉬는 가짜 세션으로 받는 도중 0.3초에 일시정지, 2.5초 뒤 재개
    (관측은 1초마다 돈다 — 일시정지 구간에 두 번 넘게 돌 수 있다)
    -> 일시정지한 지 0.2초 뒤부터 재개까지 남긴 스레드 수 · 속도 로그 0건, 재개 뒤 완료
    """
    engine, data, logger, output, finished, failures = _recording_engine(
        tmp_path, monkeypatch, throttle=0.005
    )

    data.model.start()
    thread = _run_in_thread(engine)
    time.sleep(0.3)
    assert data.model.pause() is True, "전제: 받는 도중에 일시정지됐다"
    paused_at = time.perf_counter()
    time.sleep(2.5)
    resumed_at = time.perf_counter()
    data.model.resume()
    assert finished.wait(timeout=60)
    thread.join(timeout=10)

    # 일시정지 직후의 한 틱은 이미 돌고 있던 관측일 수 있다 — 0.2초의 틈을 둔다
    during = [at for at in logger.speed_lines if paused_at + 0.2 < at < resumed_at]
    assert during == []
    assert failures == [] and output.read_bytes() == CONTENT


def test_stopping_while_receiving_tells_the_logger_it_stopped_during_transfer(
    tmp_path, monkeypatch
):
    """받는 도중 중단하면 로거에 전송 단계에서 중단됐다고 한 번 알려야 한다.

    청크마다 5ms 쉬는 가짜 세션으로 받는 도중 0.25초에 중단
    -> log_stopped("transfer") 1회, 완료 통지 없음, 실패 통지 없음
    """
    engine, data, logger, _output, finished, failures = _recording_engine(
        tmp_path, monkeypatch, throttle=0.005
    )

    data.model.start()
    thread = _run_in_thread(engine)
    time.sleep(0.25)
    data.model.stop()
    thread.join(timeout=10)

    assert logger.stopped == ["transfer"]
    assert not finished.is_set() and failures == []


def test_stopping_while_preparing_tells_the_logger_it_stopped_during_prepare(tmp_path, monkeypatch):
    """준비하는 동안 중단하면 로거에 준비 단계에서 중단됐다고 알려야 하고, 받기를 띄우지 않아야 한다.

    준비(prepare)가 도는 동안 중단
    -> log_stopped("prepare") 1회, 받은 바이트 0, 완료 통지 없음
    """
    engine, data, logger, _output, finished, failures = _recording_engine(
        tmp_path, monkeypatch, throttle=0.0
    )
    real_prepare = engine.prepare

    def stopping_prepare(content):
        plan = real_prepare(content)
        data.model.stop()
        return plan

    monkeypatch.setattr(engine, "prepare", stopping_prepare)

    data.model.start()
    thread = _run_in_thread(engine)
    thread.join(timeout=10)

    assert logger.stopped == ["prepare"]
    assert data.total_downloaded_size == 0 and not finished.is_set() and failures == []


def test_cut_stage_times_leave_out_the_pause_and_the_pause_is_reported_apart(
    server,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    """컷의 단계 시간에는 일시정지한 시간이 들지 않아야 하고, 그 합은 따로 넘겨야 한다.

    구간 하나(프레임 35~80). 오디오 단계의 ffmpeg가 뜬 직후 일시정지하고 1.5초 뒤 재개
    -> log_cut_stages의 넷째 인자(일시정지한 시간) 1.2초 이상 1.5초 + 0.5초 이하
    -> 단계 시간의 합 + 일시정지한 시간 <= 컷에 걸린 실제 시간 + 0.3초,
       오디오 단계의 시간 < 1.2초(일시정지가 빠졌다)
    """
    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))])
    marks: list[float] = []

    def on_launch(command, process) -> None:
        if command[-1] == "audio.m4a" and not marks:
            marks.append(time.perf_counter())
            assert run.data.model.pause() is True, "전제: 컷 도중에 일시정지됐다"
            threading.Timer(1.5, run.data.model.resume).start()

    processes = record_processes(monkeypatch, on_launch)
    try:
        started = time.perf_counter()
        run.start()
        took = time.perf_counter() - started
    finally:
        end_all(processes)

    logged = [args for name, args in run.logger.calls if name == "log_cut_stages"]
    assert len(logged) == 1 and len(logged[0]) == 4, "전제: 일시정지한 시간이 함께 넘어왔다"
    _number, _total, stages, paused = logged[0]
    by_name = dict(stages)
    assert 1.2 <= paused <= 2.0
    assert by_name["audio"] < 1.2
    assert sum(by_name.values()) + paused <= took + 0.3
    assert (run.finished, run.failures) == (1, [])


def test_cut_without_a_pause_reports_no_paused_time(server, tmp_path):  # noqa: F811
    """일시정지 없이 자른 구간은 일시정지한 시간을 넘기지 않아야 한다(그 인자를 모르는 로거가 깨지지 않는다).

    구간 하나(프레임 35~80)를 일시정지 없이 받음 -> log_cut_stages의 인자 3개
    """
    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))]).start()

    logged = [args for name, args in run.logger.calls if name == "log_cut_stages"]
    assert len(logged) == 1 and len(logged[0]) == 3
    assert (run.finished, run.failures) == (1, [])


def test_stopping_during_the_cut_tells_the_logger_it_stopped_during_postprocess(
    server,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    """구간을 자르는 도중 중단하면 로거에 후처리 단계에서 중단됐다고 알려야 한다.

    구간 하나(프레임 35~80). 오디오 단계의 ffmpeg가 뜬 직후 중단
    -> log_stopped("postprocess") 1회, 완료 통지 없음
    """
    run = _Run(server, "plain", tmp_path, [TimeRange(_seconds(35), _seconds(80))])

    def on_launch(command, process) -> None:
        if command[-1] == "audio.m4a":
            run.data.model.stop()

    processes = record_processes(monkeypatch, on_launch)
    try:
        run.start()
    finally:
        end_all(processes)

    assert [args for name, args in run.logger.calls if name == "log_stopped"] == [("postprocess",)]
    assert run.finished == 0
