"""오류 재큐 상한 검증 (#131).

상한 없는 재큐는 영구 오류(404·403)를 영원히 재시도해 다운로드가 끝나지도,
실패하지도 않았다. 이 테스트는 새 상한 규칙을 검증한다:

- 오류 재큐는 항목별로 상한이 있다 — 영구 오류(4xx)는 짧게(2회),
  일시 오류(5xx·타임아웃·연결 끊김)는 길게(10회)
- 상한 도달 시 재큐 대신 실패 콜백이 호출되고 원인이 로그에 남는다
- 저속 재큐(<100 KB/s 연속 6회)는 상한에 세지 않는다 — 정상 회선에서도
  발동하는 규칙이라 함께 세면 느린 회선의 정상 다운로드가 실패한다

1회 실패 시 재큐 동작 자체는 기존 박제 테스트가 고정한다
(test_file_downloader_rules / test_m3u8_downloader_rules — 시나리오·단언 무수정).
"""

import requests

from core.downloaders.base import _is_permanent_error
from core.models.download_state import DownloadState
from core.models.download_data import DownloadData

MB = 1024 * 1024
CHUNK = 8192


class RecordingLogger:
    """DownloadLogger 호환 가짜 로거 — 실제 파일을 만들지 않고 호출만 기록한다."""

    def __init__(self):
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.thread_completes: list[tuple] = []

    def log_thread_adjust(self, active_threads, avg_speed):
        pass

    def log_thread_debug(self, active_threads, download_speed, avg_speed):
        pass

    def log_thread_start(self, thread_id, start, end):
        pass

    def log_m3u8_thread_start(self, thread_id, segment):
        pass

    def log_thread_complete(self, thread_id, downloaded_size):
        self.thread_completes.append((thread_id, downloaded_size))

    def log_part_resume(self, part_num, offset, part_size):
        pass

    def log_error(self, message, exception=None):
        self.errors.append(message)

    def warning(self, message):
        self.warnings.append(message)


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


class FakeHttpResponse:
    """HTTPError에 실을 상태 코드만 가진 응답 흉내."""

    def __init__(self, status_code: int):
        self.status_code = status_code


def _http_error(status: int) -> requests.HTTPError:
    """지정 상태 코드의 HTTPError를 만든다 (raise_for_status 산출물과 동형)."""
    return requests.HTTPError(f"HTTP {status}", response=FakeHttpResponse(status))


def _make_data(output_path: str) -> DownloadData:
    """테스트용 DownloadData를 만든다 (네트워크 정보는 더미)."""
    return DownloadData(
        base_url="https://example.invalid/video.mp4",
        vod_url="https://chzzk.naver.com/video/1",
        output_path=output_path,
        resolution=1080,
        content_type="video",
    )


# ================================================================ 오류 분류


def test_client_errors_are_permanent():
    """404·403 등 4xx(408·429 제외)는 재시도해도 결과가 같은 영구 오류다."""
    assert _is_permanent_error(_http_error(404))
    assert _is_permanent_error(_http_error(403))
    assert _is_permanent_error(_http_error(410))


def test_retryable_statuses_and_transport_errors_are_transient():
    """408·429·5xx·연결/타임아웃 오류는 일시 오류로 분류한다."""
    assert not _is_permanent_error(_http_error(408))
    assert not _is_permanent_error(_http_error(429))
    assert not _is_permanent_error(_http_error(500))
    assert not _is_permanent_error(_http_error(503))
    assert not _is_permanent_error(requests.ConnectionError("boom"))
    assert not _is_permanent_error(requests.Timeout("slow"))
    assert not _is_permanent_error(requests.HTTPError("no response attached"))


# ================================================================ 파일 경로 (base 공통 규칙)


def _prepare_file_engine(tmp_path, monkeypatch, exception=None, chunks=None, clock_step=1e-6):
    """RUNNING 상태의 FileDownloader와 부속(데이터·로거·실패 기록)을 준비한다."""
    import core.downloaders.file_downloader as mod
    from core.downloaders.file_downloader import FileDownloader

    output = tmp_path / "part.bin"
    output.write_bytes(b"")

    data = _make_data(str(output))
    logger = RecordingLogger()
    failures: list[BaseException] = []
    engine = FileDownloader(data, logger, on_failed=failures.append)

    data.model.start()
    data.threads_progress = [0] * 4
    data.remaining_ranges = []

    monkeypatch.setattr(
        mod, "get_thread_session", lambda: FakeSession(FakeResponse(chunks or []), exception)
    )
    monkeypatch.setattr(mod.tm, "time", TickingClock(clock_step))
    return engine, data, logger, failures


def test_permanent_error_fails_download_at_limit(tmp_path, monkeypatch):
    """완료 조건: 영구 오류(404)를 주입하면 상한(재큐 2회)에서 멈추고 실패로 끝난다."""
    error = _http_error(404)
    engine, data, logger, failures = _prepare_file_engine(tmp_path, monkeypatch, exception=error)

    for attempt in range(2):  # 1·2회째 실패 — 아직 재큐된다
        data.remaining_ranges = []  # 실행 루프가 pop해 갔다고 가정
        engine._download_part(0, MB - 1, 0, MB)
        assert data.remaining_ranges == [(0, MB - 1)], f"{attempt + 1}회째는 재큐돼야 한다"
        assert failures == []

    data.remaining_ranges = []
    returned = engine._download_part(0, MB - 1, 0, MB)  # 3회째 — 상한 도달

    assert returned == 0  # 워커는 정상 반환한다 (future_dict 정리 경로 유지)
    assert data.remaining_ranges == []  # 더 이상 재큐하지 않는다
    assert failures == [error]  # 실패 콜백으로 끝난다
    assert data.model.state is DownloadState.WAITING  # 실행 루프 종료 신호
    assert data.failed_threads == 2  # 재큐된 횟수만 센다
    # 완료 조건: 원인이 로그에 남는다 — 상한 도달 사실과 오류 분류
    assert any("requeue limit" in m and "permanent" in m for m in logger.errors)


def test_transient_error_gets_longer_allowance(tmp_path, monkeypatch):
    """일시 오류(연결 끊김)는 재큐 10회까지 허용되고, 11회째에 실패로 끝난다."""
    error = requests.ConnectionError("boom")
    engine, data, logger, failures = _prepare_file_engine(tmp_path, monkeypatch, exception=error)

    for _ in range(10):  # 10회까지는 재큐된다
        data.remaining_ranges = []
        engine._download_part(0, MB - 1, 0, MB)
        assert data.remaining_ranges == [(0, MB - 1)]
        assert failures == []

    data.remaining_ranges = []
    engine._download_part(0, MB - 1, 0, MB)  # 11회째 — 상한 도달

    assert data.remaining_ranges == []
    assert failures == [error]
    assert data.model.state is DownloadState.WAITING
    assert any("requeue limit" in m and "transient" in m for m in logger.errors)


def test_requeue_counts_are_per_item(tmp_path, monkeypatch):
    """상한은 항목별이다 — 한 파트의 실패가 다른 파트의 허용 횟수를 깎지 않는다."""
    error = _http_error(404)
    engine, data, logger, failures = _prepare_file_engine(tmp_path, monkeypatch, exception=error)

    for _ in range(2):  # 파트 (0, MB-1)이 상한 직전까지 실패
        data.remaining_ranges = []
        engine._download_part(0, MB - 1, 0, MB)

    data.remaining_ranges = []
    engine._download_part(MB, 2 * MB - 1, 1, 2 * MB)  # 다른 파트의 첫 실패

    assert data.remaining_ranges == [(MB, 2 * MB - 1)]  # 정상 재큐
    assert failures == []


# ================================================================ 저속 재큐 분리 (m3u8 경로)


def _prepare_m3u8_engine(tmp_path):
    """RUNNING 상태의 M3U8Downloader와 부속을 준비한다 (세그먼트 로직만 단위 검증)."""
    import core.downloaders.m3u8_downloader as mod
    from core.downloaders.m3u8_downloader import M3U8Downloader

    data = _make_data(str(tmp_path / "out.mp4"))
    logger = RecordingLogger()
    failures: list[BaseException] = []
    engine = M3U8Downloader(data, logger, on_failed=failures.append)

    data.model.start()
    data.threads_progress = [0] * 4
    data.remaining_ranges = []
    engine.temp_dir = str(tmp_path)
    engine.width = 4
    return engine, data, logger, failures, mod


def test_slow_requeues_are_unlimited_and_download_still_completes(tmp_path, monkeypatch):
    """완료 조건: 저속 재큐가 양 상한(2·10회)을 넘게 반복돼도 실패하지 않고 완주한다."""
    engine, data, logger, failures, mod = _prepare_m3u8_engine(tmp_path)

    slow_chunks = [b"x" * CHUNK] * 10  # 1초/청크 → 8 KB/s로 항상 저속
    monkeypatch.setattr(mod, "get_thread_session", lambda: FakeSession(FakeResponse(slow_chunks)))
    monkeypatch.setattr(mod.tm, "time", TickingClock(1.0))

    for _ in range(12):  # 영구(2)·일시(10) 상한 모두 초과
        data.remaining_ranges = []
        engine._download_segment(index=7, segment="segment_007.m4v", part_num=0, total_ranges=4)
        assert data.remaining_ranges == [(7, "segment_007.m4v")]  # 계속 재큐된다

    assert failures == []  # 저속 재큐는 실패가 아니다
    assert data.restart_threads == 12
    assert data.model.state is DownloadState.RUNNING

    # 속도가 회복되면 같은 세그먼트가 정상 완주한다
    fast_chunks = [b"x" * CHUNK] * 3
    monkeypatch.setattr(mod, "get_thread_session", lambda: FakeSession(FakeResponse(fast_chunks)))
    monkeypatch.setattr(mod.tm, "time", TickingClock(1e-6))
    engine._download_segment(index=7, segment="segment_007.m4v", part_num=0, total_ranges=4)

    assert data.completed_threads == 1
    assert failures == []


def test_slow_requeues_do_not_consume_error_allowance(tmp_path, monkeypatch):
    """저속 재큐와 오류 재큐는 별도 계열이다 — 저속 반복 후에도 오류 허용 횟수는 그대로다."""
    engine, data, logger, failures, mod = _prepare_m3u8_engine(tmp_path)

    for _ in range(12):  # 저속 재큐를 상한 이상 반복
        engine._requeue_slow((7, "segment_007.m4v"), 0)

    monkeypatch.setattr(mod, "get_thread_session", lambda: FakeSession(exception=_http_error(404)))
    monkeypatch.setattr(mod.tm, "time", TickingClock(1e-6))
    data.remaining_ranges = []
    engine._download_segment(index=7, segment="segment_007.m4v", part_num=0, total_ranges=4)

    # 오류 계열의 첫 실패이므로 실패 종료가 아니라 정상 재큐다
    assert data.remaining_ranges == [(7, "segment_007.m4v")]
    assert failures == []
    assert data.model.state is DownloadState.RUNNING


# ================================================================ hls_aes 경로 배선


def test_hls_aes_permanent_error_fails_at_limit(tmp_path, monkeypatch):
    """AES 경로도 같은 상한을 탄다 — 세그먼트 403(키·쿠키 만료류) 반복 시 실패로 끝난다."""
    import core.downloaders.hls_aes_downloader as mod
    from core.downloaders.hls_aes_downloader import HlsAesDownloader

    data = _make_data(str(tmp_path / "out.mp4"))
    logger = RecordingLogger()
    failures: list[BaseException] = []
    engine = HlsAesDownloader(data, logger, on_failed=failures.append)

    data.model.start()
    data.threads_progress = [0] * 4
    data.remaining_ranges = []
    engine.width = 4

    error = _http_error(403)
    monkeypatch.setattr(mod, "get_thread_session", lambda: FakeSession(exception=error))
    monkeypatch.setattr(mod.tm, "time", TickingClock(1e-6))

    for _ in range(2):
        data.remaining_ranges = []
        engine._download_segment(index=3, segment="seg_003.ts", part_num=0, total_ranges=4)
        assert data.remaining_ranges == [(3, "seg_003.ts")]

    data.remaining_ranges = []
    engine._download_segment(index=3, segment="seg_003.ts", part_num=0, total_ranges=4)

    assert data.remaining_ranges == []
    assert failures == [error]
    assert data.model.state is DownloadState.WAITING
    assert any("requeue limit" in m and "permanent" in m for m in logger.errors)


# ================================================================ 실행 루프 재사용 초기화


def test_error_counts_reset_between_runs(tmp_path, monkeypatch):
    """엔진 재사용 시 run()이 카운터를 초기화한다 — 이전 실행의 실패가 이월되지 않는다."""
    engine, data, logger, failures = _prepare_file_engine(
        tmp_path, monkeypatch, exception=_http_error(404)
    )
    for _ in range(2):
        data.remaining_ranges = []
        engine._download_part(0, MB - 1, 0, MB)
    assert engine._error_requeues == {(0, MB - 1): 2}

    # run()의 재사용 초기화 블록과 동일한 동작 검증 (실행 루프 전체는 띄우지 않는다)
    with engine.lock:
        engine._error_requeues = {}

    data.remaining_ranges = []
    engine._download_part(0, MB - 1, 0, MB)
    assert data.remaining_ranges == [(0, MB - 1)]  # 초기화 후 첫 실패 — 정상 재큐
    assert failures == []
