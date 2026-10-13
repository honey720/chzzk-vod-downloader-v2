"""FileDownloader.run 통합 검증 — 가짜 세션으로 전체 파이프라인 실행 (#73).

실제 네트워크 없이 Range 요청을 흉내 내는 세션으로 다음을 검증한다:
- 완주: 결과 파일이 바이트 단위로 원본과 동일, 완료 콜백 1회
- 일시정지 → 재개 → 완료: PAUSED 동안 진행 정지, 재개 후 완주,
  허용되지 않는 상태 전이(warning) 0건
- 중단(stop): 파일 삭제, 완료 콜백 없음
"""

import threading
import time


import errno
import os
from types import SimpleNamespace

import pytest

import core.downloaders.base as base_module
import core.downloaders.file_downloader as fd_module
from core.downloaders.base import (
    DISK_SPACE_SHORT,
    FILE_TOO_LARGE_FOR_FAT,
    FileSizeLimitError,
    PostprocessError,
)
from core.downloaders.file_downloader import FileDownloader
from core.models.download_state import DownloadState
from core.models.download_data import DownloadData

MB = 1024 * 1024
TOTAL_SIZE = 4 * MB  # 해상도 144 → 파트 1MB → 4파트


class RunLogger:
    """run() 경로 전체가 쓰는 로거 인터페이스를 기록만 하며 흉내 낸다."""

    def __init__(self):
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.completed_times: list[float] = []
        self.closed = 0
        # 단계 경계 로그 (#110)
        self.transfer_completes: list[tuple] = []
        self.transfer_paused: list[float] = []  # 전송 줄에 함께 온 일시정지한 시간(초)
        self.breakdown_paused: list[float] = []  # 시간 구분 줄에 함께 온 일시정지한 시간(초)
        self.postprocess_starts: list[str] = []
        self.postprocess_completes: list[tuple] = []
        self.breakdowns: list[tuple] = []

    def log_download_start(self, total_size, part_size, segments, initial_threads):
        pass

    def log_part_resume(self, part_num, offset, part_size):
        pass

    def log_transfer_complete(self, elapsed, downloaded_bytes, retries, peak_threads, paused=0.0):
        self.transfer_completes.append((elapsed, downloaded_bytes, retries, peak_threads))
        self.transfer_paused.append(paused)

    def log_postprocess_start(self, kind):
        self.postprocess_starts.append(kind)

    def log_postprocess_complete(self, elapsed, output_size):
        self.postprocess_completes.append((elapsed, output_size))

    def log_total_breakdown(self, transfer_elapsed, postprocess_elapsed, paused=0.0):
        self.breakdowns.append((transfer_elapsed, postprocess_elapsed))
        self.breakdown_paused.append(paused)

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


def test_phase_boundary_logs_for_file_path(tmp_path, monkeypatch):
    """파일 경로의 단계 경계 로그 (#110) — 전송 요약 1줄, 후처리 줄 없음.

    후처리가 없는 경로는 구분 줄이 "(no postprocess)"(postprocess_elapsed
    None)로 남아 세 다운로더의 로그가 같은 형태로 끝난다.
    """
    engine, data, logger, output, finished, failures = _make_engine(tmp_path, monkeypatch)

    data.model.start()
    thread = _run_in_thread(engine)
    assert finished.wait(timeout=30), "완료 콜백이 호출되지 않았다"
    thread.join(timeout=10)

    assert len(logger.transfer_completes) == 1
    _elapsed, downloaded, retries, peak = logger.transfer_completes[0]
    assert downloaded == len(CONTENT)  # 받은 총 바이트
    assert retries == 0  # 가짜 세션은 실패·저속이 없다
    assert 1 <= peak <= 4  # 파트 4개 경로의 정점 동시 스레드
    assert logger.postprocess_starts == [] and logger.postprocess_completes == []
    assert len(logger.breakdowns) == 1
    _transfer, postprocess = logger.breakdowns[0]
    assert postprocess is None  # 후처리 없음 표기
    assert logger.completed_times  # 기존 완료 줄(형식 불변)도 그대로


def test_pause_resume_then_complete(tmp_path, monkeypatch):
    """일시정지 동안 진행이 멈추고, 재개 후 완주한다. 전이 warning 0건."""
    # 청크당 5ms 스로틀 → 4파트 병렬로 약 0.64초 — 중간에 일시정지할 여유를 만든다
    engine, data, logger, output, finished, failures = _make_engine(
        tmp_path, monkeypatch, throttle=0.005
    )
    # 저속 재큐 판정 비활성 (#160) — 느린 CI 러너에서는 스로틀된 가짜
    # 다운로드의 측정 속도가 임계(100KB/s) 아래로 떨어져 정상적인 저속
    # 재큐가 발동, 재큐 순환으로 완료 상한(60초)을 넘겼다(macOS 실측).
    # 이 테스트의 검증 대상은 일시정지·재개이지 저속 규칙이 아니다 —
    # 저속 규칙은 test_file_downloader_rules.py가 가짜 시계로 박제한다
    engine._slow_speed_threshold_kb_s = 0

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


# ================================================================ FAT 계열의 파일 크기 한계 (#309)

FAT_LIMIT = 4 * 1024**3 - 1  # 4GiB − 1


def _on_filesystem(monkeypatch, name, free: int = 10**13) -> list[str]:
    """저장 폴더의 파일 시스템 이름과 여유 공간을 정해 둔다 — 이름을 물은 폴더를 적어 돌려준다."""
    asked: list[str] = []

    def filesystem_name(folder):
        asked.append(folder)
        return name

    monkeypatch.setattr(base_module, "filesystem_name", filesystem_name)
    monkeypatch.setattr(base_module.shutil, "disk_usage", lambda folder: SimpleNamespace(free=free))
    return asked


@pytest.mark.parametrize(
    ("name", "largest", "refused"),
    [
        ("FAT32", FAT_LIMIT, False),  # 한계와 같다 — 담긴다
        ("FAT32", FAT_LIMIT + 1, True),  # 한계를 1바이트 넘는다
        ("vfat", FAT_LIMIT + 1, True),
        ("exFAT", FAT_LIMIT + 1, False),
        ("NTFS", FAT_LIMIT + 1, False),
        (None, FAT_LIMIT + 1, False),  # 이름을 읽지 못했다 — 검사하지 않는다
    ],
)
def test_file_size_is_refused_only_over_the_limit_on_a_fat_drive(
    tmp_path, monkeypatch, name, largest, refused
):
    """만들 파일은 저장 폴더가 FAT 계열이고 크기가 4GiB − 1바이트를 넘을 때만 거부돼야 한다.

    파일 시스템 이름과 크기는 표의 것
    -> 거부면 FileSizeLimitError(키 "File too large for a FAT32 drive", largest · filesystem 그대로),
       아니면 예외 없음. 이름을 물은 폴더 == 산출물의 폴더
    """
    engine, _data, _logger, _output, _finished, _failures = _make_engine(tmp_path, monkeypatch)
    asked = _on_filesystem(monkeypatch, name)

    if refused:
        with pytest.raises(FileSizeLimitError) as info:
            engine._require_file_size_fits(largest)
        error = info.value
        assert (error.message_key, error.largest, error.filesystem) == (
            FILE_TOO_LARGE_FOR_FAT,
            largest,
            name,
        )
    else:
        engine._require_file_size_fits(largest)
    assert asked == [str(tmp_path)]


def test_file_size_check_logs_the_filesystem_and_the_largest_file(tmp_path, monkeypatch):
    """파일 크기 검사는 읽은 파일 시스템 이름과 가장 큰 파일의 크기를 로그로 남겨야 한다.

    이름 "exFAT", 크기 123 -> 줄 ("exFAT", 123). 이름을 읽지 못하면 (None, 123)
    """
    engine, _data, logger, _output, _finished, _failures = _make_engine(tmp_path, monkeypatch)
    lines: list[tuple] = []
    logger.log_file_size_check = lambda *args: lines.append(args)

    _on_filesystem(monkeypatch, "exFAT")
    engine._require_file_size_fits(123)
    _on_filesystem(monkeypatch, None)
    engine._require_file_size_fits(123)

    assert lines == [("exFAT", 123), (None, 123)]


@pytest.mark.parametrize(("over", "starts"), [(1, False), (0, True)], ids=["over", "at-limit"])
def test_download_without_sections_does_not_start_when_the_file_is_over_the_fat_limit(
    tmp_path, monkeypatch, over, starts
):
    """구간 없는 mp4는 저장 폴더가 FAT 계열이고 파일이 한계를 넘으면 받지 않고 실패해야 한다.

    파일 4MiB, 파일 시스템 "FAT32", 한계를 4MiB − 1(파일이 1바이트 넘는다) 또는 4MiB(꼭 맞는다)로 둠
    -> 넘으면: 실패 1건(FileSizeLimitError, largest 4MiB), 완료 안 함, 산출물 없음
       꼭 맞으면: 완료, 실패 0건
    """
    engine, data, _logger, output, finished, failures = _make_engine(tmp_path, monkeypatch)
    _on_filesystem(monkeypatch, "FAT32")
    monkeypatch.setattr(base_module, "FAT_MAX_FILE_BYTES", TOTAL_SIZE - over)

    data.model.start()
    thread = _run_in_thread(engine)
    thread.join(timeout=30)

    assert not thread.is_alive(), "엔진이 끝나지 않았다"
    if starts:
        assert (finished.is_set(), failures) == (True, [])
    else:
        assert [(type(e), e.largest) for e in failures] == [(FileSizeLimitError, TOTAL_SIZE)]
        assert (finished.is_set(), output.exists()) == (False, False)


# ================================================================ 쓰다가 난 공간 부족 (#309)


def _no_space() -> OSError:
    return OSError(errno.ENOSPC, "No space left on device")


def _cut_failure_from_ffmpeg() -> PostprocessError:
    """ffmpeg가 쓰다 실패한 컷 — 원인의 글에 ffmpeg의 stderr가 들어 있다."""
    error = PostprocessError("후처리(cut) 실패: 구간 1개")
    error.__cause__ = RuntimeError("exit 1: av_interleaved_write_frame(): No space left on device")
    return error


@pytest.mark.parametrize(
    ("make", "expected"),
    [
        (_no_space, True),  # 파이썬이 쓰다 실패
        (_cut_failure_from_ffmpeg, True),  # ffmpeg가 쓰다 실패 — 원인의 글
        (lambda: OSError(errno.EACCES, "Permission denied"), False),
        (lambda: PostprocessError("후처리(cut) 실패: 조각을 잇는 곳에서 DTS가 뒤로 간다"), False),
    ],
    ids=["oserror-enospc", "ffmpeg-text-in-cause", "other-oserror", "other-cut-failure"],
)
def test_running_out_of_space_is_found_in_the_error_or_its_cause(make, expected):
    """쓰다가 공간이 없어 난 실패는 예외 자신이나 원인에서 알아봐야 한다.

    ENOSPC인 OSError · 원인의 글에 "No space left on device"가 든 실패 -> 참
    권한 오류 · 그 글이 없는 컷 실패 -> 거짓
    """
    assert base_module._ran_out_of_space(make()) is expected


@pytest.mark.parametrize(
    ("name", "free", "sections", "expected"),
    [
        ("FAT32", FAT_LIMIT, True, FILE_TOO_LARGE_FOR_FAT),  # 공간은 있는데 실패 — 한계에 닿았다
        ("FAT32", FAT_LIMIT, False, FILE_TOO_LARGE_FOR_FAT),
        ("FAT32", FAT_LIMIT - 1, True, DISK_SPACE_SHORT),  # 정말 공간이 모자라다
        ("NTFS", 10**13, True, DISK_SPACE_SHORT),
        ("NTFS", 10**13, False, None),  # 구간 없는 다운로드 — 저장 실패 안내가 그대로 나간다
        (None, 10**13, True, DISK_SPACE_SHORT),  # 파일 시스템을 읽지 못했다
    ],
)
def test_failure_from_running_out_of_space_carries_the_key_to_show(
    tmp_path, monkeypatch, name, free, sections, expected
):
    """쓰다가 공간이 없어 난 실패를 알릴 때, 예외에 보일 안내의 키(space_key)가 적혀 있어야 한다.

    파일 시스템 · 저장 폴더의 여유 · 구간이 있는지는 표의 것. 엔진의 실패 통지로 ENOSPC인 OSError를 보냄
    -> 받은 예외는 보낸 그 객체이고 space_key == 표의 기대값(None이면 속성이 없다)
    """
    engine, data, _logger, _output, _finished, failures = _make_engine(tmp_path, monkeypatch)
    _on_filesystem(monkeypatch, name, free)
    if sections:
        data.content.selection_paths = (os.path.join(str(tmp_path), "구간_1.mp4"),)
        data.content.selections = (object(),)
    sent = _no_space()

    engine._notify_failed(sent)

    assert failures == [sent]
    assert getattr(sent, "space_key", None) == expected


def test_failure_that_is_not_about_space_carries_no_key(tmp_path, monkeypatch):
    """공간과 무관한 실패에는 space_key를 적지 않아야 한다.

    파일 시스템 "FAT32"(여유 넉넉), 권한 오류인 OSError를 실패로 통지 -> space_key 속성 없음
    """
    engine, _data, _logger, _output, _finished, failures = _make_engine(tmp_path, monkeypatch)
    _on_filesystem(monkeypatch, "FAT32")
    sent = OSError(errno.EACCES, "Permission denied")

    engine._notify_failed(sent)

    assert failures == [sent] and not hasattr(sent, "space_key")
