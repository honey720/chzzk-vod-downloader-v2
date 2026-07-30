"""QtDownloadBridge 배선 검증 (#75) — QApplication 불필요 (같은 스레드 emit은 direct 배달).

실제 DownloadService 대신 페이크 서비스를 주입해, 브리지가
- 구 DownloadManager.start와 같은 순서(데이터·태스크 생성 → RUNNING 전이 → 제출)로 제출하고
- core 콜백을 기존 Signal 시그니처로 변환·중계하며
- 완료·실패·중지 후처리(참조 정리, 상태 정리)를 수행하는지
검증한다. 진행 변환식 자체의 값은 각 케이스에서 함께 확인한다.
"""

import pytest
import requests

from content.data import ContentItem
from core.downloaders.base import PostprocessError
from core.downloaders.hls_aes_downloader import DecryptionError
from core.models.download_state import DownloadState
from core.models.events import ProgressEvent
from download.qt_bridge import QtDownloadBridge, _failure_message_key


class FakeHandle:
    """DownloadHandle 대역 — 브리지가 쓰는 인터페이스만 제공한다.

    wait 호출 횟수를 기록한다 — 실패 경로에서 메인 스레드가 엔진을 기다리면
    죽은 마운트 I/O에 갇혀 UI가 얼어붙는 회귀가 있었다 (PR #135 코멘트).
    """

    def __init__(self, data):
        self.data = data
        self.stopped = False
        self.wait_calls = 0

    def elapsed_seconds(self) -> float:
        return 61.0

    def wait(self, timeout=None) -> bool:
        self.wait_calls += 1
        return True


class FakeService:
    """DownloadService 대역 — submit 인자를 기록하고 페이크 핸들을 돌려준다."""

    def __init__(self):
        self.submissions: list[dict] = []
        self.abandoned: list = []

    def submit(self, content, **kwargs):
        self.submissions.append({"content": content, **kwargs})
        return FakeHandle(kwargs["data"])

    def abandon(self, handle):
        self.abandoned.append(handle)


class FakeLogger:
    """DownloadLogger 대역 — 파일 생성 없이 호출 이름만 기록한다."""

    def __init__(self, *args, **kwargs):
        self.calls: list[str] = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append(name)

        return record


def _make_item(content_type: str = "video") -> ContentItem:
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {"title": "t"},
        [(720, "base")],
        720,
        "https://cdn.example/video.mp4",
        "downloads",
        content_type,
        None,
    )
    item.output_path = "downloads/t 720p.mp4"
    return item


@pytest.fixture
def bridge(monkeypatch):
    # 실제 DownloadLogger는 config 경로에 로그 파일을 만들므로 대역으로 바꾼다
    monkeypatch.setattr("download.qt_bridge.DownloadLogger", FakeLogger)
    return QtDownloadBridge(service=FakeService())


def _submission(bridge: QtDownloadBridge) -> dict:
    return bridge._service.submissions[0]


class TestStart:
    def test_start_submits_content_with_shared_data(self, bridge):
        """제출된 content는 브리지가 만든 공유 데이터(data.content)와 같은 객체다."""
        item = _make_item()
        bridge.start(item)

        sub = _submission(bridge)
        assert sub["content"] is sub["data"].content
        assert sub["content"].url == item.vod_url
        assert sub["content"].output_path == item.output_path
        # 구 manager.start와 동일하게 제출 전에 RUNNING 전이를 마친다 (카드 상태 반영 포함)
        assert bridge.task.state is DownloadState.RUNNING
        assert item.downloadState is DownloadState.RUNNING


class TestProgressRelay:
    def test_file_progress_conversion(self, bridge):
        """파일 경로: 구 MonitorThread와 같은 (남은시간, 크기, 속도, %, item) 변환."""
        item = _make_item()
        bridge.start(item)
        received = []
        bridge.progress.connect(lambda *args: received.append(args))

        on_progress = _submission(bridge)["on_progress"]
        on_progress(ProgressEvent(downloaded_size=50, total_size=100, speed=1.0))

        assert received == [("00:00:00", "50", "1.0 MB/s", 50, item)]

    def test_m3u8_progress_conversion_and_merge_flag(self, bridge):
        """m3u8 경로: 세그먼트 수 기반 % 계산, 병합 시작 콜백은 post_process 플래그 전환."""
        item = _make_item("m3u8")
        bridge.start(item)
        received = []
        bridge.progress.connect(lambda *args: received.append(args))

        sub = _submission(bridge)
        data = sub["data"]
        data.max_threads = 10
        data.completed_threads = 5
        sub["on_progress"](ProgressEvent(downloaded_size=500, speed=0.0))
        assert received[-1] == ("N/A", "500", "0.0 MB/s", 50, item)

        # 병합 단계 진입 후에는 병합된 세그먼트 수 기반으로 계산한다
        sub["on_merge_start"]()
        assert item.post_process is True
        data.merged_segments = 11
        sub["on_progress"](ProgressEvent(downloaded_size=500, speed=0.0))
        assert received[-1] == ("N/A", "500", "0.0 MB/s", 100, item)


class TestCompletion:
    def test_finished_emits_and_clears_refs(self, bridge):
        """완료: 소요 시간 문자열과 함께 finished를 emit하고 참조를 정리한다."""
        item = _make_item()
        bridge.start(item)
        received = []
        bridge.finished.connect(lambda *args: received.append(args))

        _submission(bridge)["on_finished"]()

        assert received == [(item, "00:01:01")]
        assert bridge.handle is None
        assert bridge.task is None

    def test_failed_stops_engine_and_emits_reason_without_waiting(self, bridge):
        """실패 (#134): 엔진 종료 신호(stop→WAITING) 후 failed(item, 사유)를 emit한다.

        구 테스트(test_failed_emits_stopped_and_resets_state)는 "예외를 버리고
        WAITING으로 되돌리는" 고장난 동작을 박제하고 있었다 — 실패가 유저의
        정지와 구분되지 않는 결함 그 자체라, 실패 표시 계약으로 반전했다
        (#128 조사 ①, 근거는 PR 본문).

        종점이 모델을 FAILED로 전이하거나 handle.wait()로 기다리면 안 되는
        이유 (죽은 네트워크 드라이브 프리즈 회귀 — PR #135 코멘트): 워커 예외
        경로의 실패는 run 루프가 살아 있는 중에 통지되는데, 루프는 WAITING만
        종료 신호로 보고 FAILED→WAITING은 불허 전이라 되돌릴 수도 없다. 또
        run()의 꼬리 정리는 죽은 마운트 I/O에 갇힐 수 있어 메인 스레드가
        기다리면 UI가 얼어붙는다. FAILED 표시는 아이템 레벨
        (ContentManager.fail)의 몫이다.
        """
        item = _make_item("m3u8")
        bridge.start(item)
        item.post_process = True
        handle = bridge.handle
        model = _submission(bridge)["data"].model
        received, stopped = [], []
        bridge.failed.connect(lambda *args: received.append(args))
        bridge.stopped.connect(lambda *args: stopped.append(args))

        raw = "후처리(remux) 실패: ffmpeg stderr tail... [C:\\tools\\ffmpeg.exe]"
        _submission(bridge)["on_failed"](PostprocessError(raw))

        # 엔진 종료 신호 — 실행 루프는 WAITING만 종료 신호로 본다 (v2.9.0·main과 동일)
        assert model.state is DownloadState.WAITING
        # 프리즈 회귀 방지 — 메인 스레드는 실패 경로에서 엔진을 기다리지 않는다
        assert handle.wait_calls == 0
        assert stopped == []  # 실패는 더 이상 정지로 위장하지 않는다
        [(failed_item, message)] = received
        assert failed_item is item
        # 번역기 미설치 환경 — tr()은 키 원문을 돌려준다. 원시 문자열은 미노출
        assert message == "Postprocessing failed"
        assert "ffmpeg" not in message
        assert item.post_process is False
        # 참조 정리 — 실패 후 다음 다운로드 시작이 가능해야 한다
        assert bridge.handle is None
        assert bridge.task is None

    def test_unmapped_failure_emits_empty_reason(self, bridge):
        """매핑에 없는 예외는 사유 없이 실패만 알린다 — 원시 str(e) 노출 금지."""
        item = _make_item()
        bridge.start(item)
        model = _submission(bridge)["data"].model
        received = []
        bridge.failed.connect(lambda *args: received.append(args))

        _submission(bridge)["on_failed"](RuntimeError("raw internal detail"))

        assert received == [(item, "")]
        assert model.state is DownloadState.WAITING  # 엔진 종료 신호 (FAILED 표시는 매니저 몫)

    def test_failure_after_user_cleanup_is_ignored(self, bridge):
        """중지·정리 후 늦게 도착한 실패는 무시한다 (완료 경로의 가드와 동일)."""
        item = _make_item()
        bridge.start(item)
        bridge.stop()
        bridge.removeThreads()
        received = []
        bridge.failed.connect(lambda *args: received.append(args))

        bridge._engineFailed.emit(RuntimeError("late"))

        assert received == []
        assert item.downloadState is DownloadState.WAITING

    def test_user_stop_emits_stopped(self, bridge):
        item = _make_item()
        bridge.start(item)
        received = []
        bridge.stopped.connect(lambda *args: received.append(args))

        bridge.stop()

        assert received == [(item,)]
        assert bridge.task.state is DownloadState.WAITING

    def test_pause_resume_emit_with_item(self, bridge):
        item = _make_item()
        bridge.start(item)
        events = []
        bridge.paused.connect(lambda it: events.append(("paused", it)))
        bridge.resumed.connect(lambda it: events.append(("resumed", it)))

        bridge.pause()
        bridge.resume()

        assert events == [("paused", item), ("resumed", item)]


class StuckHandle(FakeHandle):
    """파일 I/O에 갇힌 워커 흉내 — wait가 절대 끝나지 않는다 (#136·#137)."""

    def __init__(self, data):
        super().__init__(data)
        self.wait_timeouts: list = []

    def wait(self, timeout=None) -> bool:
        self.wait_timeouts.append(timeout)
        return False


class TestRemoveThreads:
    """중지·정리 경로의 대기 상한 (#137 — #136 제안 ①).

    구 removeThreads는 타임아웃 없는 wait로, 갇힌 워커 상태에서 유저의
    유일한 행동(중지·종료)이 앱을 얼렸다. 이제 상한 초과 시 슬롯을
    방출(service.abandon)하고 참조만 정리한다.
    """

    def test_stuck_worker_gives_up_and_abandons_slot(self, bridge):
        item = _make_item()
        bridge.start(item)
        stuck = StuckHandle(_submission(bridge)["data"])
        bridge.handle = stuck

        bridge.removeThreads()  # 갇힌 워커 — 여기서 영원히 기다리면 앱 프리즈다

        assert stuck.wait_timeouts and all(
            t is not None and t > 0 for t in stuck.wait_timeouts
        )  # 무한 대기(None) 금지
        assert bridge._service.abandoned == [stuck]  # 슬롯 방출 — 다음 다운로드가 시작될 수 있다
        assert bridge.handle is None
        assert bridge.task is None

    def test_normal_exit_is_not_abandoned(self, bridge):
        """정상 종료 워커는 방출하지 않는다 — 기존 정리 경로 무변경."""
        item = _make_item()
        bridge.start(item)
        handle = bridge.handle

        bridge.removeThreads()

        assert handle.wait_calls == 1
        assert bridge._service.abandoned == []
        assert bridge.handle is None
        assert bridge.task is None


class _FakeHttpResponse:
    """HTTPError에 실을 상태 코드만 가진 응답 흉내."""

    def __init__(self, status_code: int):
        self.status_code = status_code


def _http_error(status: int) -> requests.HTTPError:
    return requests.HTTPError(f"HTTP {status}", response=_FakeHttpResponse(status))


class TestFailureMessageKey:
    """실패 예외 → 안내 키 매핑 (#134, #127의 조회 경로 방식)."""

    def test_postprocess_and_decryption_map_to_keys(self):
        assert _failure_message_key(PostprocessError("ffmpeg stderr...")) == (
            "Postprocessing failed"
        )
        assert _failure_message_key(DecryptionError("키·IV 불일치")) == "Decryption failed"

    def test_http_statuses_map_like_metadata_path(self):
        assert _failure_message_key(_http_error(403)) == "Viewing permission required"
        assert _failure_message_key(_http_error(401)) == "Viewing permission required"
        assert _failure_message_key(_http_error(404)) == "Video not found"
        assert _failure_message_key(_http_error(500)) == "Network connection error"

    def test_transport_and_os_errors(self):
        assert _failure_message_key(requests.ConnectionError("boom")) == (
            "Network connection error"
        )
        assert _failure_message_key(requests.Timeout("slow")) == "Network connection error"
        assert _failure_message_key(OSError(28, "No space left", "C:\\full\\path.mp4")) == (
            "Failed to save file"
        )

    def test_unknown_exception_has_no_key(self):
        assert _failure_message_key(RuntimeError("anything")) is None
