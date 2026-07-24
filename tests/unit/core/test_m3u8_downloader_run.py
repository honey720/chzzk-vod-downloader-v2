"""M3U8Downloader.run 통합 검증 — 가짜 세션으로 전체 파이프라인 실행 (#74).

실제 네트워크 없이 m3u8 플레이리스트·초기화 세그먼트·세그먼트 스트림을
흉내 내는 세션으로 다음을 검증한다:
- 완주: 결과 파일이 초기화 세그먼트 + 세그먼트들을 **인덱스 순서대로** 이어붙인
  것과 바이트 단위로 동일(병합 순서 보장), 병합 시작 콜백·완료 콜백 각 1회,
  임시 폴더 삭제
- 일시정지 → 재개 → 완료: PAUSED 동안 진행 정지, 재개 후 완주,
  허용되지 않는 상태 전이(warning) 0건
- 중단(stop): 결과 파일·임시 폴더 삭제, 완료 콜백 없음
"""

import threading
import time

import core.downloaders.m3u8_downloader as m3u8_module
from core.downloaders.m3u8_downloader import M3U8Downloader
from core.models.download_state import DownloadState
from download.data import DownloadData

CHUNK = 8192
SEGMENT_COUNT = 6
BASE_URL = "https://example.invalid/hls/video.m3u8"

INIT_CONTENT = b"\xf0" * CHUNK


def _segment_content(index: int, chunks_per_segment: int) -> bytes:
    """세그먼트별로 구별되는 결정적 본문 — 병합 순서를 바이트로 검증하기 위함."""
    return bytes([index + 1]) * (CHUNK * chunks_per_segment)


def _playlist_text() -> str:
    """EXT-X-MAP 초기화 세그먼트와 세그먼트 목록을 가진 최소 플레이리스트."""
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", '#EXT-X-MAP:URI="init.m4s"']
    for i in range(SEGMENT_COUNT):
        lines.append("#EXTINF:2.000,")
        lines.append(f"seg_{i}.m4v")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines)


class StreamResponse:
    """세그먼트 본문을 청크로 흘리는 가짜 응답. throttle 초/청크로 속도를 조절한다."""

    def __init__(self, body: bytes, throttle: float):
        self._body = body
        self._throttle = throttle

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=CHUNK):
        for i in range(0, len(self._body), chunk_size):
            if self._throttle:
                time.sleep(self._throttle)
            yield self._body[i : i + chunk_size]


class TextResponse:
    """플레이리스트(.text)·초기화 세그먼트(.content) 겸용 가짜 응답."""

    def __init__(self, text: str = "", content: bytes = b""):
        self.text = text
        self.content = content

    def raise_for_status(self):
        pass


class M3U8Session:
    """m3u8 경로의 세 요청(플레이리스트/초기화/세그먼트)을 URL로 분기하는 가짜 세션."""

    def __init__(self, chunks_per_segment: int, throttle: float = 0.0):
        self._chunks_per_segment = chunks_per_segment
        self._throttle = throttle

    def get(self, url, **kwargs):
        if url.endswith(".m3u8"):
            return TextResponse(text=_playlist_text())
        if url.endswith("init.m4s"):
            return TextResponse(content=INIT_CONTENT)
        index = int(url.rsplit("seg_", 1)[1].removesuffix(".m4v"))
        return StreamResponse(_segment_content(index, self._chunks_per_segment), self._throttle)


def _expected_output(chunks_per_segment: int) -> bytes:
    """초기화 세그먼트 + 세그먼트들을 인덱스 순서로 이어붙인 기대 결과."""
    return INIT_CONTENT + b"".join(
        _segment_content(i, chunks_per_segment) for i in range(SEGMENT_COUNT)
    )


class RunLogger:
    """run() 경로 전체가 쓰는 로거 인터페이스를 기록만 하며 흉내 낸다."""

    def __init__(self):
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.completed_times: list[float] = []
        self.closed = 0

    def log_download_start(self, total_size, part_size, segments, initial_threads):
        pass

    def log_m3u8_thread_start(self, thread_id, segment_url):
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


def _make_engine(tmp_path, monkeypatch, chunks_per_segment: int = 3, throttle: float = 0.0):
    """가짜 세션이 연결된 RUNNING 이전 상태의 엔진을 준비한다."""
    output = tmp_path / "out.mp4"
    data = DownloadData(
        base_url=BASE_URL,
        vod_url="https://chzzk.naver.com/video/1",
        output_path=str(output),
        resolution=1080,
        content_type="m3u8",
    )
    logger = RunLogger()
    session = M3U8Session(chunks_per_segment, throttle)
    monkeypatch.setattr(m3u8_module, "get_thread_session", lambda: session)

    finished = threading.Event()
    failures: list[BaseException] = []
    merge_starts: list[bool] = []

    def on_finished():
        # 실제 어댑터 경로(manager.finish → task.finish)를 흉내 내 상태를 종결한다
        data.model.finish()
        finished.set()

    engine = M3U8Downloader(
        data,
        logger,
        on_finished=on_finished,
        on_failed=failures.append,
        on_merge_start=lambda: merge_starts.append(True),
    )
    return engine, data, logger, output, finished, failures, merge_starts


def _run_in_thread(engine) -> threading.Thread:
    """엔진 run()을 별도 스레드에서 실행한다 (호출 규약: start() 후 run())."""
    thread = threading.Thread(target=engine.run, daemon=True)
    thread.start()
    return thread


def test_run_merges_segments_in_index_order_byte_exact(tmp_path, monkeypatch):
    """전체 파이프라인이 초기화+세그먼트 순서 그대로의 파일을 만들고 콜백을 1회씩 호출한다."""
    engine, data, logger, output, finished, failures, merge_starts = _make_engine(
        tmp_path, monkeypatch
    )

    data.model.start()
    thread = _run_in_thread(engine)
    assert finished.wait(timeout=30), "완료 콜백이 호출되지 않았다"
    thread.join(timeout=10)

    assert output.read_bytes() == _expected_output(chunks_per_segment=3)
    assert merge_starts == [True]  # 병합 시작 통지 1회
    assert failures == []
    assert logger.errors == []
    assert logger.warnings == []  # 느린 세그먼트 오탐·전이 warning 없음
    assert logger.completed_times and logger.closed == 1
    assert data.completed_threads == SEGMENT_COUNT  # 세그먼트 전부 정상 완료
    assert data.merged_segments == SEGMENT_COUNT + 1  # 초기화 세그먼트 포함 병합
    assert not (tmp_path / "CVDv2_temp").exists()  # 임시 폴더 삭제


def test_pause_resume_then_complete(tmp_path, monkeypatch):
    """일시정지 동안 진행이 멈추고, 재개 후 완주한다. 전이 warning 0건."""
    # 청크당 5ms 스로틀 × 세그먼트당 120청크 → 일시정지할 여유를 만들고,
    # 일시정지 시점까지 누적 바이트를 충분히 키워 재개 직후 평균 속도가
    # 저속 판정(<100 KB/s)에 걸리는 오탐을 피한다 (판정식은 누적/경과 기반)
    engine, data, logger, output, finished, failures, merge_starts = _make_engine(
        tmp_path, monkeypatch, chunks_per_segment=120, throttle=0.005
    )

    data.model.start()
    thread = _run_in_thread(engine)

    time.sleep(0.25)  # 다운로드가 진행되는 중간 지점
    assert data.model.pause() is True  # RUNNING → PAUSED (유효 전이)
    assert data.model.state is DownloadState.PAUSED

    # 워커들이 pause_event 대기에 도달할 시간을 준 뒤 진행량이 고정되는지 확인
    time.sleep(0.3)
    snapshot = sum(data.threads_progress) + data.completed_progress
    time.sleep(0.5)
    assert sum(data.threads_progress) + data.completed_progress == snapshot

    assert data.model.resume() is True  # PAUSED → RUNNING (유효 전이)
    assert finished.wait(timeout=60), "재개 후 완료 콜백이 호출되지 않았다"
    thread.join(timeout=10)

    assert output.read_bytes() == _expected_output(chunks_per_segment=120)
    assert failures == []
    assert logger.warnings == []  # 상태 전이 warning 0건 (완료 조건)
    assert data.model.state is DownloadState.FINISHED


def test_stop_removes_partial_file_and_temp_dir(tmp_path, monkeypatch):
    """중단(stop)하면 임시 폴더·부분 파일을 삭제하고 완료 콜백을 호출하지 않는다."""
    engine, data, logger, output, finished, failures, merge_starts = _make_engine(
        tmp_path, monkeypatch, chunks_per_segment=40, throttle=0.005
    )

    data.model.start()
    thread = _run_in_thread(engine)

    time.sleep(0.25)
    data.model.stop()  # RUNNING → WAITING (취소)
    thread.join(timeout=30)
    assert not thread.is_alive()

    assert not output.exists()  # 부분 파일 없음
    assert not (tmp_path / "CVDv2_temp").exists()  # 임시 폴더 삭제
    assert not finished.is_set()
    assert failures == []
