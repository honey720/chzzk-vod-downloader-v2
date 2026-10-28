"""다시 요청할 때의 받은 양 · 속도 (#395).

연결을 다시 요청하면(저속 재시작 · 오류 재요청) 받던 바이트가 진행 집계에서 빠진다. 속도는 받은 양이
아니라 회선으로 받은 바이트(받은 양 + 버린 바이트)로 재므로 0 미만이 되지 않아야 하고, 파일 파트는
산출물에 남은 바이트를 이어받으므로 받은 양이 줄지 않아야 한다. 끝난 뒤 전송 줄의 Bytes는 산출물에
남은 바이트와 같아야 한다.

워커 함수(``_download_part`` · ``_download_segment``)를 직접 부르고, 진행을 기록할 때마다 속도를 한 번
잰다 — 관측 틱이 받기의 한가운데에 올 때를 확정한다.
"""

import threading

import pytest
import requests

import core.engine.base as base_module
import core.sources.file_downloader as fd_module
import core.sources.m3u8_downloader as m3u8_module
from core.sources.file_downloader import FileDownloader
from core.sources.m3u8_downloader import M3U8Downloader
from core.models.download_data import DownloadData
from tests.core.engine.fmp4_samples import init_segment, media_segment, split

CHUNK = 8192


class StepLogger:
    """워커 함수가 쓰는 로거 인터페이스를 기록만 하며 흉내 낸다."""

    def __init__(self):
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def log_thread_debug(self, active_threads, download_speed, avg_speed):
        pass

    def log_thread_complete(self, thread_id, downloaded_size):
        pass

    def log_m3u8_thread_start(self, thread_id, segment_url):
        pass

    def log_part_resume(self, part_num, offset, part_size):
        pass

    def log_error(self, message, exception=None):
        self.errors.append(message)

    def warning(self, message):
        self.warnings.append(message)


class Body:
    """청크를 흘리는 가짜 응답 — fail_after개를 흘린 뒤 연결이 끊긴다(None이면 끝까지)."""

    def __init__(self, chunks, status_code: int = 206, fail_after: int | None = None):
        self._chunks = chunks
        self.status_code = status_code
        self._fail_after = fail_after

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=CHUNK):
        for number, chunk in enumerate(self._chunks):
            if number == self._fail_after:
                raise requests.ConnectionError("연결 끊김")
            yield chunk

    def close(self):
        pass


class QueuedSession:
    """준비된 응답을 요청 순서대로 돌려주는 가짜 세션."""

    def __init__(self, responses):
        self._responses = list(responses)

    def get(self, url, **kwargs):
        return self._responses.pop(0)


class Clock:
    """엔진의 단조 시계(``_now``) 대체 — 읽을 때마다 1ms 흐른다(저속 판정 창에 닿지 않는다)."""

    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self) -> float:
        self.now += 0.001
        return self.now


class SlowAfter:
    """``_watch_slow``가 돌려주는 판정기 대체 — 첫 판정기만 calls번째 판정에서 저속이라 답한다."""

    def __init__(self, calls: int):
        self._calls = calls
        self._made = 0

    def __call__(self, *args, **kwargs):
        self._made += 1
        first = self._made == 1
        seen = [0]

        class Watch:
            def is_slow(self_, elapsed, received):
                seen[0] += 1
                return first and seen[0] == self._calls

        return Watch()


class Watched:
    """진행을 기록할 때마다, 그리고 tick()을 부를 때 속도를 재고 그때의 받은 양을 적는다."""

    def __init__(self, engine, data, monkeypatch):
        self.engine, self.data = engine, data
        self.speeds: list[float] = []
        self.sizes: list[int] = []
        report = engine._check_speed_and_update_progress

        def report_and_tick(*args):
            report(*args)
            self.tick()

        monkeypatch.setattr(engine, "_check_speed_and_update_progress", report_and_tick)

    def tick(self) -> None:
        self.engine.measure_speed()
        self.speeds.append(self.data.speed_mb)
        self.sizes.append(self.data.total_downloaded_size)


def _data(tmp_path, content_type: str) -> DownloadData:
    return DownloadData(
        base_url="https://example.invalid/hls/video.m3u8",
        vod_url="https://chzzk.naver.com/video/1",
        output_path=str(tmp_path / "out.mp4"),
        resolution=1080,
        content_type=content_type,
    )


def _segment_engine(tmp_path, monkeypatch, responses):
    """RUNNING 상태의 세그먼트 엔진과 관측기 — 세그먼트 로직만 단위로 돌린다."""
    data = _data(tmp_path, "m3u8")
    engine = M3U8Downloader(data, StepLogger())
    data.model.start()
    data.threads_progress = [0] * 4
    data.remaining_ranges = []
    engine.temp_dir = str(tmp_path)
    engine.width = 4
    session = QueuedSession(responses)
    monkeypatch.setattr(m3u8_module, "get_thread_session", lambda: session)
    monkeypatch.setattr(engine, "_now", Clock())
    return engine, data, Watched(engine, data, monkeypatch)


PART_END = 4 * CHUNK - 1  # 4청크짜리 파트
PART = bytes(range(256)) * (4 * CHUNK // 256)  # 파트 본문
PART_CHUNKS = split(PART, CHUNK)


def _part_engine(tmp_path, monkeypatch, responses):
    """RUNNING 상태의 파일 엔진과 관측기 — 빈 산출물에 파트 하나를 받는다."""
    data = _data(tmp_path, "video")
    (tmp_path / "out.mp4").write_bytes(b"")
    engine = FileDownloader(data, StepLogger())
    data.model.start()
    data.threads_progress = [0] * 4
    data.remaining_ranges = []
    session = QueuedSession(responses)
    monkeypatch.setattr(fd_module, "get_thread_session", lambda: session)
    monkeypatch.setattr(engine, "_now", Clock())
    return engine, data, Watched(engine, data, monkeypatch)


SEGMENT = media_segment(3 * CHUNK)  # 3청크짜리 온전한 세그먼트 (#321)
SEGMENT_CHUNKS = split(SEGMENT, CHUNK)


def _non_decreasing(values) -> bool:
    return all(after >= before for before, after in zip(values, values[1:]))


# ================================================================ 세그먼트 — 받다 만 것을 버린다


def test_slow_restart_of_a_segment_keeps_speed_non_negative(tmp_path, monkeypatch):
    """세그먼트가 저속으로 재시작되면 버린 바이트를 따로 세고 속도가 0 미만이 되지 않아야 한다 (#395).

    세그먼트 3청크, 둘째 청크에서 저속 판정 → 재시작, 다시 받아 끝냄
    -> 매 측정의 속도 >= 0, 버린 바이트 == 2청크, 끝난 뒤 받은 양 == 세그먼트 크기
    """
    engine, data, watched = _segment_engine(
        tmp_path, monkeypatch, [Body(SEGMENT_CHUNKS), Body(SEGMENT_CHUNKS)]
    )
    monkeypatch.setattr(engine, "_watch_slow", SlowAfter(2))

    engine._download_segment(index=0, segment="seg_0.m4v", part_num=0, total_ranges=4)
    watched.tick()
    assert data.restart_threads == 1  # 전제: 저속 재시작이 났다
    engine._download_segment(index=0, segment="seg_0.m4v", part_num=0, total_ranges=4)
    engine.update_progress()
    watched.tick()

    assert min(watched.speeds) >= 0
    assert data.discarded_bytes == 2 * CHUNK
    assert data.total_downloaded_size == len(SEGMENT)


def test_error_retry_of_a_segment_keeps_speed_non_negative(tmp_path, monkeypatch):
    """세그먼트가 받는 중 끊겨 다시 요청되면 버린 바이트를 따로 세고 속도가 0 미만이 되지 않아야 한다 (#395).

    세그먼트 3청크, 둘째 청크를 받은 뒤 연결 끊김 → 오류 재요청, 다시 받아 끝냄
    -> 매 측정의 속도 >= 0, 버린 바이트 == 2청크, 끝난 뒤 받은 양 == 세그먼트 크기
    """
    engine, data, watched = _segment_engine(
        tmp_path, monkeypatch, [Body(SEGMENT_CHUNKS, fail_after=2), Body(SEGMENT_CHUNKS)]
    )

    engine._download_segment(index=0, segment="seg_0.m4v", part_num=0, total_ranges=4)
    watched.tick()
    assert data.failed_threads == 1  # 전제: 오류 재요청이 났다
    engine._download_segment(index=0, segment="seg_0.m4v", part_num=0, total_ranges=4)
    engine.update_progress()
    watched.tick()

    assert min(watched.speeds) >= 0
    assert data.discarded_bytes == 2 * CHUNK
    assert data.total_downloaded_size == len(SEGMENT)


# ================================================================ 파일 파트 — 산출물에 남은 것을 이어받는다


def test_slow_restart_of_a_part_resumes_without_lowering_the_received_size(tmp_path, monkeypatch):
    """파트가 저속으로 재시작돼 이어받으면 받은 양이 줄지 않고 속도가 0 미만이 되지 않아야 한다 (#395).

    파트 4청크, 둘째 청크에서 저속 판정 → 재시작, 셋째 청크부터 이어받아(206) 끝냄
    -> 받은 양이 매 측정에서 줄지 않음, 속도 >= 0, 버린 바이트 0, 끝난 뒤 받은 양 == 산출물 크기
    """
    engine, data, watched = _part_engine(
        tmp_path, monkeypatch, [Body(PART_CHUNKS), Body(PART_CHUNKS[2:])]
    )
    monkeypatch.setattr(engine, "_watch_slow", SlowAfter(2))

    engine._download_part(0, PART_END, 0, len(PART))
    watched.tick()
    assert data.restart_threads == 1  # 전제: 저속 재시작이 났다
    assert data.total_downloaded_size == 2 * CHUNK  # 기다리는 동안에도 받아 쓴 2청크가 남는다
    engine._download_part(0, PART_END, 0, len(PART))
    engine.update_progress()
    watched.tick()

    assert _non_decreasing(watched.sizes)
    assert min(watched.speeds) >= 0
    assert data.discarded_bytes == 0
    assert data.total_downloaded_size == len(PART) == (tmp_path / "out.mp4").stat().st_size
    assert (tmp_path / "out.mp4").read_bytes() == PART


def test_error_retry_of_a_part_resumes_without_lowering_the_received_size(tmp_path, monkeypatch):
    """파트가 받는 중 끊겨 이어받으면 받은 양이 줄지 않고 속도가 0 미만이 되지 않아야 한다 (#395).

    파트 4청크, 둘째 청크를 받은 뒤 연결 끊김 → 오류 재요청, 셋째 청크부터 이어받아(206) 끝냄
    -> 받은 양이 매 측정에서 줄지 않음, 속도 >= 0, 버린 바이트 0, 끝난 뒤 받은 양 == 산출물 크기
    """
    engine, data, watched = _part_engine(
        tmp_path, monkeypatch, [Body(PART_CHUNKS, fail_after=2), Body(PART_CHUNKS[2:])]
    )

    engine._download_part(0, PART_END, 0, len(PART))
    watched.tick()
    assert data.failed_threads == 1  # 전제: 오류 재요청이 났다
    assert data.total_downloaded_size == 2 * CHUNK  # 기다리는 동안에도 받아 쓴 2청크가 남는다
    engine._download_part(0, PART_END, 0, len(PART))
    engine.update_progress()
    watched.tick()

    assert _non_decreasing(watched.sizes)
    assert min(watched.speeds) >= 0
    assert data.discarded_bytes == 0
    assert data.total_downloaded_size == len(PART) == (tmp_path / "out.mp4").stat().st_size
    assert (tmp_path / "out.mp4").read_bytes() == PART


def test_rejected_resume_counts_the_kept_bytes_as_discarded(tmp_path, monkeypatch):
    """이어받기가 거부되면(200) 남겨 둔 바이트를 버린 바이트로 세고 속도가 0 미만이 되지 않아야 한다 (#395).

    파트 4청크, 둘째 청크를 받은 뒤 연결 끊김 → 이어받기 요청에 200 → 처음부터 다시 받아 끝냄
    -> 속도 >= 0, 버린 바이트 == 2청크, 끝난 뒤 받은 양 == 산출물 크기
    """
    engine, data, watched = _part_engine(
        tmp_path,
        monkeypatch,
        [Body(PART_CHUNKS, fail_after=2), Body([], status_code=200), Body(PART_CHUNKS, 200)],
    )

    engine._download_part(0, PART_END, 0, len(PART))
    watched.tick()
    engine._download_part(0, PART_END, 0, len(PART))
    engine.update_progress()
    watched.tick()

    assert min(watched.speeds) >= 0
    assert data.discarded_bytes == 2 * CHUNK
    assert data.total_downloaded_size == len(PART) == (tmp_path / "out.mp4").stat().st_size
    assert (tmp_path / "out.mp4").read_bytes() == PART


def test_part_without_retry_is_counted_as_before(tmp_path, monkeypatch):
    """다시 요청하지 않은 파트는 받은 바이트가 받은 양에 그대로 오르고 버린 바이트가 없어야 한다 (#395).

    파트 4청크를 한 번에 받음
    -> 받은 양이 1~4청크로 오름, 버린 바이트 0, 속도 > 0
    """
    engine, data, watched = _part_engine(tmp_path, monkeypatch, [Body(PART_CHUNKS)])

    engine._download_part(0, PART_END, 0, len(PART))
    engine.update_progress()
    watched.tick()

    assert watched.sizes == [CHUNK, 2 * CHUNK, 3 * CHUNK, 4 * CHUNK, 4 * CHUNK]
    assert data.discarded_bytes == 0
    assert min(watched.speeds[:4]) > 0


# ================================================================ 실행 전체 — 전송 줄의 Bytes


class RunLogger:
    """run() 경로 전체가 쓰는 로거 인터페이스 — 전송 줄과 버린 바이트 줄만 기록한다."""

    def __init__(self):
        self.transfer_bytes: list[int] = []
        self.discarded: list[int] = []
        self.errors: list[str] = []

    def log_transfer_complete(self, elapsed, downloaded_bytes, retries, peak_threads, paused=0.0):
        self.transfer_bytes.append(downloaded_bytes)

    def log_transfer_discarded(self, size):
        self.discarded.append(size)

    def log_error(self, message, exception=None):
        self.errors.append(message)

    def log_exception(self, message, exception=None):
        self.errors.append(message)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


TOTAL = 4 * 1024 * 1024  # 해상도 144 → 파트 1MB → 4파트
FILE_BODY = bytes(range(251)) * (TOTAL // 251) + bytes(range(TOTAL % 251))


class FileServer:
    """head · Range get을 받는 가짜 세션 — 둘째 파트의 첫 요청만 2청크 뒤에 끊는다."""

    def __init__(self, fail: bool):
        self._fail = fail
        self.headers = {"content-length": str(TOTAL)}

    def head(self, url, **kwargs):
        return self

    def raise_for_status(self):
        pass

    def get(self, url, headers=None, **kwargs):
        start, end = map(int, headers["Range"].removeprefix("bytes=").split("-"))
        chunks = split(FILE_BODY[start : end + 1], CHUNK)
        if self._fail and start == 1024 * 1024:
            self._fail = False
            return Body(chunks, fail_after=2)
        return Body(chunks)


SEGMENTS = [media_segment(3 * CHUNK, bytes([index + 1])) for index in range(6)]
INIT = init_segment(CHUNK, b"\xf0")
PLAYLIST = "\n".join(
    ["#EXTM3U", "#EXT-X-VERSION:7", '#EXT-X-MAP:URI="init.m4s"']
    + [line for index in range(6) for line in ("#EXTINF:2.000,", f"seg_{index}.m4v")]
    + ["#EXT-X-ENDLIST"]
)


class Text:
    """플레이리스트(.text) · 초기화 세그먼트(.content) 겸용 가짜 응답."""

    def __init__(self, text: str = "", content: bytes = b""):
        self.text, self.content = text, content

    def raise_for_status(self):
        pass


class SegmentServer:
    """m3u8 경로의 가짜 세션 — 셋째 세그먼트의 첫 요청만 2청크 뒤에 끊는다."""

    def __init__(self, fail: bool):
        self._fail = fail

    def get(self, url, **kwargs):
        if url.endswith(".m3u8"):
            return Text(text=PLAYLIST)
        if url.endswith("init.m4s"):
            return Text(content=INIT)
        index = int(url.rsplit("seg_", 1)[1].removesuffix(".m4v"))
        chunks = split(SEGMENTS[index], CHUNK)
        if self._fail and index == 2:
            self._fail = False
            return Body(chunks, status_code=200, fail_after=2)
        return Body(chunks, status_code=200)


def _pass_through(chunks, dst_path, **_wait):
    with open(dst_path, "wb") as f:
        for chunk in chunks:
            f.write(chunk)


def _run(make, data) -> None:
    """make(on_finished)로 엔진을 만들어 끝까지 돌린다."""
    finished = threading.Event()
    engine = make(lambda: (data.model.finish(), finished.set()))
    data.model.start()
    thread = threading.Thread(target=engine.run, daemon=True)
    thread.start()
    assert finished.wait(timeout=60), "완료 콜백이 오지 않았다"
    thread.join(timeout=10)


@pytest.mark.parametrize("fail", [True, False], ids=["retry", "no-retry"])
def test_file_run_reports_the_output_bytes_after_a_retry(tmp_path, monkeypatch, fail):
    """파트가 끊겨 이어받은 파일 다운로드도 전송 줄의 Bytes가 산출물 크기와 같아야 한다 (#395).

    4파트, 둘째 파트의 첫 요청이 2청크 뒤 끊김(retry) · 끊김 없음(no-retry)
    -> Bytes == 산출물 크기 == 4MB, 버린 바이트 줄 없음(이어받았다)
    """
    data = DownloadData(
        base_url="https://example.invalid/video.mp4",
        vod_url="https://chzzk.naver.com/video/1",
        output_path=str(tmp_path / "out.mp4"),
        resolution=144,
        content_type="video",
    )
    logger = RunLogger()
    server = FileServer(fail)
    monkeypatch.setattr(fd_module, "get_thread_session", lambda: server)

    _run(lambda done: FileDownloader(data, logger, on_finished=done), data)

    output = tmp_path / "out.mp4"
    assert output.read_bytes() == FILE_BODY
    assert logger.transfer_bytes == [output.stat().st_size]
    assert logger.discarded == []
    assert data.failed_threads == (1 if fail else 0)


@pytest.mark.parametrize("fail", [True, False], ids=["retry", "no-retry"])
def test_segment_run_reports_the_kept_bytes_and_the_discarded_bytes(tmp_path, monkeypatch, fail):
    """세그먼트를 다시 받은 m3u8 다운로드는 Bytes가 세그먼트 크기의 합이고 버린 바이트를 따로 적어야 한다 (#395).

    6세그먼트, 셋째 세그먼트의 첫 요청이 2청크 뒤 끊김(retry) · 끊김 없음(no-retry)
    -> Bytes == 세그먼트 크기의 합, 버린 바이트 줄 == [2청크](retry) · 없음(no-retry)
    """
    data = _data(tmp_path, "m3u8")
    logger = RunLogger()
    server = SegmentServer(fail)
    monkeypatch.setattr(m3u8_module, "get_thread_session", lambda: server)
    monkeypatch.setattr(base_module, "remux_stream", _pass_through)

    _run(lambda done: M3U8Downloader(data, logger, on_finished=done), data)

    assert (tmp_path / "out.mp4").read_bytes() == INIT + b"".join(SEGMENTS)
    assert logger.transfer_bytes == [sum(len(segment) for segment in SEGMENTS)]
    assert logger.discarded == ([2 * CHUNK] if fail else [])
    assert data.failed_threads == (1 if fail else 0)
