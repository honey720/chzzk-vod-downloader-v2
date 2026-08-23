"""저속 재큐 판정이 디스크 쓰기 지연과 네트워크 수신 지연을 구분하는지 검증 (#191).

오너 실측: 같은 VOD·같은 바이트 수인데 exFAT SD 카드에서 재큐 70회,
맥 내장 SSD에서 0회였다 — 저속 판정의 elapsed가 디스크 쓰기 시간을
포함해 느린 저장매체를 느린 회선으로 오판했다. 이 파일은 그 파일
파트(#78)·m3u8 세그먼트 두 경로를 실제 파일 I/O(느린 쓰기를 흉내 내는
래퍼)로 재현한다 — 저속 재큐 자체의 규칙(임계·연속 6회)은 건드리지
않는다. 박제된 rules 테스트(test_file_downloader_rules.py·
test_m3u8_downloader_rules.py, 34건)는 이 파일과 별개로 무수정이다.

시계는 실제 `time.time()`/`time.perf_counter()`를 그대로 쓴다(가짜
시계 미사용) — 네트워크 자체는 인위적 지연 없이 즉시 응답하고, 디스크
쓰기만 `time.sleep()`으로 실제로 늦춘다. 이렇게 하면 "실제로 걸린
시간"을 코드가 올바르게 구분하는지를 가짜 시계 없이 실행으로 증명한다.
"""

import time as real_time

import core.downloaders.file_downloader as fd_module
import core.downloaders.m3u8_downloader as m3u8_module
from core.downloaders.file_downloader import FileDownloader
from core.downloaders.m3u8_downloader import M3U8Downloader
from core.models.download_data import DownloadData

CHUNK = 8192
# 청크당 이 지연이면 디스크 쓰기 시간만으로 8192/0.15/1024 ≈ 53 KB/s —
# 저속 임계(100 KB/s) 아래로 확실히 떨어진다. 네트워크는 지연이 없으므로
# 순수 수신 시간만 잰다면 이 지연과 무관하게 항상 고속으로 판정돼야 한다.
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


# ================================================================ file 파트 (#78)


def test_slow_disk_write_alone_does_not_trigger_requeue(tmp_path, monkeypatch):
    """네트워크는 즉시 응답하는데 디스크 쓰기만 느리면(수정 후) 저속 재큐가 발동하지 않는다."""
    body = b"x" * (CHUNK * CHUNK_COUNT)
    output = tmp_path / "part.bin"
    output.write_bytes(b"\x00" * len(body))

    data = _make_data(str(output))
    logger = _RecordingLogger()
    engine = FileDownloader(data, logger)

    monkeypatch.setattr(fd_module, "get_thread_session", lambda: _InstantSession(body))
    monkeypatch.setattr(fd_module, "open", _make_slow_open(SLOW_WRITE_DELAY_S), raising=False)

    engine._download_part(0, len(body) - 1, 0, len(body))

    assert data.restart_threads == 0, "디스크 지연이 저속 재큐를 유발했다 — 회귀"
    assert data.completed_threads == 1


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


# ================================================================ m3u8 세그먼트


def test_slow_disk_write_alone_does_not_trigger_requeue_for_segment(tmp_path, monkeypatch):
    """m3u8 경로도 동일 — 디스크 쓰기만 느리면(수정 후) 저속 재큐가 발동하지 않는다."""
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

    assert data.restart_threads == 0, "디스크 지연이 저속 재큐를 유발했다 — 회귀"
    assert data.completed_threads == 1
