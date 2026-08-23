"""저속 재큐의 write_elapsed 진단 로그 검증 (#191).

**경위**: 처음엔 write_elapsed를 저속 판정 elapsed에서 빼는 수정을 했으나
(디스크 쓰기 시간을 네트워크 저속과 구분), 실기 로그가 이 가설을 반증했다
— ``write=0.000s/0.494s=0%``, 디스크 쓰기가 판정 시간의 정확히 0%였다.
``f.write()``는 OS 페이지 캐시에 즉시 반환되는 버퍼드 쓰기라(로컬 SSD
실측: 8192B write 2000회 평균 3.7μs) 뺄 게 애초에 거의 없었다 — 판정을
바꾸는 건 원리적으로 옳지만 이 경로에서는 사실상 아무 효과가 없었다.

그래서 **판정 자체(elapsed 계산)는 원래대로 되돌렸다** — 디스크 지연도
여전히 저속 판정에 반영된다(수정 전과 동일). 대신 **write_elapsed 계측과
진단 로그만 남긴다** — 다음에 비슷한 증상이 실기에서 재현되면, 이 값이
높게 찍히는지(디스크가 진짜 원인) 낮게 찍히는지(다른 원인 — 예: 시스템
전체 I/O 경합이 네트워크 수신 자체를 늦추는 경우, 코드만으로는 못 잡는
경로)로 원인을 빠르게 가릴 수 있다.

박제된 rules 테스트(test_file_downloader_rules.py·
test_m3u8_downloader_rules.py, 34건)는 이 파일과 별개로 무수정이다 —
elapsed 계산 자체가 원래 식으로 돌아왔으므로 애초에 영향이 없다.

시계는 실제 `time.time()`/`time.perf_counter()`를 그대로 쓴다(가짜
시계 미사용) — 디스크 지연은 `time.sleep()`으로 실제로 준다.
"""

import time as real_time

import core.downloaders.file_downloader as fd_module
import core.downloaders.m3u8_downloader as m3u8_module
from core.downloaders.file_downloader import FileDownloader
from core.downloaders.m3u8_downloader import M3U8Downloader
from core.models.download_data import DownloadData

CHUNK = 8192
# 청크당 이 지연이면 디스크 쓰기 시간만으로 8192/0.15/1024 ≈ 53 KB/s —
# 저속 임계(100 KB/s) 아래로 확실히 떨어져 재큐를 유발한다.
SLOW_WRITE_DELAY_S = 0.15
CHUNK_COUNT = 8  # slow_count > 5(6회 연속)를 확실히 넘기는 여유


class _SlowWriteFile:
    """실제 파일을 감싸 write()마다 인위적 지연을 주는 래퍼 — 느린 디스크 흉내."""

    def __init__(self, real_file, delay: float):
        self._real_file = real_file
        self._delay = delay

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._real_file.close()
        return False

    def seek(self, *args, **kwargs):
        return self._real_file.seek(*args, **kwargs)

    def write(self, data):
        real_time.sleep(self._delay)
        return self._real_file.write(data)


def _make_slow_open(delay: float):
    """monkeypatch로 다운로더 모듈의 open()을 대체할 팩토리."""
    real_open = open

    def _slow_open(path, mode, *args, **kwargs):
        return _SlowWriteFile(real_open(path, mode, *args, **kwargs), delay)

    return _slow_open


class _InstantResponse:
    """네트워크 지연 없이 즉시 청크를 흘리는 가짜 응답."""

    def __init__(self, body: bytes):
        self._body = body

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=CHUNK):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]


class _InstantSession:
    def __init__(self, body: bytes):
        self._body = body
        self.headers = {"content-length": str(len(body))}

    def head(self, *args, **kwargs):
        return self

    def get(self, *args, **kwargs):
        return _InstantResponse(self._body)


class _RecordingLogger:
    def __init__(self):
        self.warnings: list[str] = []

    def warning(self, message: str):
        self.warnings.append(message)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _make_data(output_path: str) -> DownloadData:
    data = DownloadData(
        base_url="https://example.invalid/video.mp4",
        vod_url="https://chzzk.naver.com/video/1",
        output_path=output_path,
        resolution=1080,
        content_type="video",
    )
    data.model.start()
    data.threads_progress = [0] * 4
    return data


def _diag_ratio(logger: _RecordingLogger) -> float:
    diag = next(w for w in logger.warnings if "write=" in w)
    return float(diag.split("=")[-1].rstrip("%)"))


# ================================================================ file 파트 (#78)


def test_disk_write_delay_still_triggers_requeue_but_diagnostic_shows_high_ratio(
    tmp_path, monkeypatch
):
    """판정은 되돌렸으므로 디스크 지연도 여전히 재큐를 유발한다 — 그 대신 진단이 원인을 밝힌다."""
    body = b"x" * (CHUNK * CHUNK_COUNT)
    output = tmp_path / "part.bin"
    output.write_bytes(b"\x00" * len(body))

    data = _make_data(str(output))
    logger = _RecordingLogger()
    engine = FileDownloader(data, logger)

    monkeypatch.setattr(fd_module, "get_thread_session", lambda: _InstantSession(body))
    monkeypatch.setattr(fd_module, "open", _make_slow_open(SLOW_WRITE_DELAY_S), raising=False)

    engine._download_part(0, len(body) - 1, 0, len(body))

    assert data.restart_threads == 1, "판정을 되돌렸으므로 디스크 지연도 재큐를 유발해야 한다"
    assert _diag_ratio(logger) >= 90.0, "디스크가 원인인데 write 비율이 낮게 찍힘"


def test_slow_network_still_triggers_requeue_for_file_part(tmp_path, monkeypatch):
    """대조군: 네트워크 자체가 느리면(수신 사이 지연) 여전히 저속 재큐가 발동해야 한다."""

    class _SlowNetworkResponse:
        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=CHUNK):
            for _ in range(CHUNK_COUNT):
                real_time.sleep(SLOW_WRITE_DELAY_S)  # 수신 자체가 느리다(디스크 아님)
                yield b"x" * chunk_size

    class _SlowNetworkSession:
        def get(self, *args, **kwargs):
            return _SlowNetworkResponse()

    output = tmp_path / "part.bin"
    output.write_bytes(b"\x00" * (CHUNK * CHUNK_COUNT))
    data = _make_data(str(output))
    logger = _RecordingLogger()
    engine = FileDownloader(data, logger)

    monkeypatch.setattr(fd_module, "get_thread_session", lambda: _SlowNetworkSession())

    engine._download_part(0, CHUNK * CHUNK_COUNT - 1, 0, CHUNK * CHUNK_COUNT)

    assert data.restart_threads == 1, "저속 재큐 기능 자체가 없어지면 안 된다 (#132)"
    # 진단 로그: 네트워크가 느린 경우이므로 write 비율이 낮게(0%에 가깝게) 찍혀야 한다
    assert _diag_ratio(logger) <= 5.0, "디스크가 원인이 아닌데 write 비율이 높게 찍힘"


# ================================================================ m3u8 세그먼트


def test_disk_write_delay_still_triggers_requeue_for_segment(tmp_path, monkeypatch):
    """m3u8 경로도 동일 — 판정은 원래대로이므로 디스크 지연이 재큐를 유발하고, 진단이 원인을 밝힌다."""
    body = b"x" * (CHUNK * CHUNK_COUNT)
    data = _make_data(str(tmp_path / "out.mp4"))
    data.content_type = "m3u8"
    logger = _RecordingLogger()
    engine = M3U8Downloader(data, logger)
    engine.temp_dir = str(tmp_path)
    engine.width = 4

    monkeypatch.setattr(m3u8_module, "get_thread_session", lambda: _InstantSession(body))
    monkeypatch.setattr(m3u8_module, "open", _make_slow_open(SLOW_WRITE_DELAY_S), raising=False)

    engine._download_segment(index=0, segment="seg_0.m4v", part_num=0, total_ranges=len(body))

    assert data.restart_threads == 1
    assert _diag_ratio(logger) >= 90.0, "디스크가 원인인데 write 비율이 낮게 찍힘"
