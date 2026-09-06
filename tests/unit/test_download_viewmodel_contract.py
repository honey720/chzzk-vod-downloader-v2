"""DownloadViewModel 계약 게이트 (#259 B0) — 흡수 전후로 바뀌지 않아야 하는 경계만 잰다.

B1(qt_bridge 흡수)은 클래스가 사라지는 단계라 그 클래스를 재던 테스트도 같이
바뀐다 — 그러면 "테스트가 통과한다"는 검사자와 피검사자가 함께 움직인 결과라
증거가 약하다. 이 파일은 흡수 **전에** 세워 흡수 **뒤에도 한 줄도 바뀌지 않은
채** 통과하는 것을 증거로 삼는다. 그래서 브리지의 클래스·Signal·속성 이름을
일절 알지 않고, 흡수 뒤에도 남는 두 경계로만 잰다:

- 위쪽 경계: DownloadService 대역(FakeService)이 받는 submit 인자와 콜백 4개
  (on_progress / on_finished / on_failed / on_merge_start).
- 아래쪽 경계: content(ContentManager 자리)가 받는 6개 호출
  (update_progress / pause / resume / stop / finish / fail)의 인자·순서·횟수,
  호출 시점의 아이템 상태, 그리고 **호출 스레드**.

스레드 축이 이 게이트의 핵심이다. 콜백은 워커 스레드에서 오고 content는 위젯을
갱신하므로 반드시 메인 스레드에서 불려야 한다(Signal 큐 연결이 그 장치다).
흡수 중 "Signal 대신 직접 부르면 되지 않나"로 단순화하면 위젯이 워커 스레드에서
갱신되어 조용히·산발적으로 깨진다. 판정은 타이밍이 아니라 스레드 식별자
기록·비교로 한다 — join()으로 워커가 끝난 뒤에도 processEvents() 전에는 아무것도
도착하지 않아야 하고, 도착한 호출의 식별자는 메인 스레드여야 한다.

DownloadLogger는 클래스 메서드를 대역으로 바꿔 파일 생성을 막는다 — 모듈 경로
문자열 monkeypatch(`download.qt_bridge.DownloadLogger`)는 흡수와 함께 죽는
지점이라 쓰지 않는다.
"""

import threading

import pytest
import requests
from PySide6.QtCore import QObject

from app.viewmodels.data import ContentItem
from app.viewmodels.download_viewmodel import DownloadViewModel
from core.downloaders.base import PostprocessError
from core.downloaders.hls_aes_downloader import DecryptionError
from core.models.download_state import DownloadState
from core.models.events import ProgressEvent
from core.utils.ffmpeg import FFmpegNotFoundError, RemuxError
from download.logger import DownloadLogger

# ============ 대역 ============


class FakeHandle:
    """DownloadHandle 대역 — wait 호출 횟수·timeout 인자를 기록한다."""

    def __init__(self, data):
        self.data = data
        self.wait_calls = 0
        self.wait_timeouts: list = []

    def elapsed_seconds(self) -> float:
        return 61.0

    def wait(self, timeout=None) -> bool:
        self.wait_calls += 1
        self.wait_timeouts.append(timeout)
        return True


class StuckHandle(FakeHandle):
    """파일 I/O에 갇힌 워커 흉내 — wait가 절대 끝나지 않는다 (#136·#137)."""

    def wait(self, timeout=None) -> bool:
        super().wait(timeout)
        return False


class FakeService:
    """DownloadService 대역 — submit 인자와 **제출 시점의 상태**를 기록한다.

    "제출 전에 RUNNING 전이와 다운로드 정보 로깅이 끝나 있다"는 순서 계약은
    제출 시점에 스냅샷을 떠야 잴 수 있다 — 사후 검사로는 순서가 안 보인다.
    """

    def __init__(self, log_calls: list, handle_factory=FakeHandle):
        self._log_calls = log_calls
        self._handle_factory = handle_factory
        self.submissions: list[dict] = []
        self.handles: list[FakeHandle] = []
        self.abandoned: list = []
        self.state_at_submit: DownloadState | None = None
        self.log_calls_at_submit: int | None = None

    def submit(self, content, **kwargs):
        self.state_at_submit = kwargs["data"].model.state
        self.log_calls_at_submit = len(self._log_calls)
        self.submissions.append({"content": content, **kwargs})
        handle = self._handle_factory(kwargs["data"])
        self.handles.append(handle)
        return handle

    def abandon(self, handle):
        self.abandoned.append(handle)


class RecordingContent(QObject):
    """content 자리의 기록 대역 — (호출 이름, 인자, 호출 스레드 id, 호출 시점 아이템 상태).

    QObject인 이유: 제품의 ContentManager가 QObject라 Signal 연결의 스레드
    친화성이 같아야 한다(큐 연결의 도착 스레드가 수신자의 스레드로 정해진다).
    호출 시점의 `item.downloadState`를 함께 적는 이유: 카드는 이 호출을 받는
    순간의 상태로 그린다 — 모델 전이보다 호출이 먼저 오면 카드가 이전 상태로
    그려지므로, "전이 → 통지" 순서는 content 쪽에서 관찰 가능한 계약이다.
    """

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, tuple, int, DownloadState]] = []

    def _record(self, name: str, item: ContentItem, *args) -> None:
        self.calls.append((name, (item, *args), threading.get_ident(), item.downloadState))

    def update_progress(self, rem, size, spd, prog, item: ContentItem):
        self._record("update_progress", item, rem, size, spd, prog)

    def pause(self, item: ContentItem):
        self._record("pause", item)

    def resume(self, item: ContentItem):
        self._record("resume", item)

    def stop(self, item: ContentItem):
        self._record("stop", item)

    def finish(self, item: ContentItem, download_time: str):
        self._record("finish", item, download_time)

    def fail(self, item: ContentItem, message: str = ""):
        self._record("fail", item, message)

    @property
    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


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


class Wired:
    """한 건의 배선: viewmodel · content 대역 · 서비스 대역 · 제출 기록."""

    def __init__(self, qapp, log_calls: list, handle_factory=FakeHandle):
        self.qapp = qapp
        self.content = RecordingContent()
        self.service = FakeService(log_calls, handle_factory)
        self.vm = DownloadViewModel(self.content, service=self.service)
        self.log_calls = log_calls

    @property
    def submission(self) -> dict:
        return self.service.submissions[0]

    @property
    def handle(self) -> FakeHandle:
        """서비스가 돌려준 핸들 — 정리 규칙(wait·abandon) 관찰용. 서비스 쪽 기록이다."""
        return self.service.handles[0]

    def start(self, item: ContentItem) -> None:
        self.vm.start(item)

    def pump(self) -> None:
        self.qapp.processEvents()


@pytest.fixture
def log_calls(monkeypatch) -> list:
    """DownloadLogger를 파일 없는 대역으로 — 클래스 메서드 교체(모듈 경로 무의존).

    log_download_info 호출을 기록해 "제출 전에 다운로드 정보가 로깅됐다"를 잰다.
    """
    calls: list = []
    monkeypatch.setattr(DownloadLogger, "_setup_logging", lambda self: None)
    monkeypatch.setattr(
        DownloadLogger,
        "log_download_info",
        lambda self, item: calls.append(("log_download_info", item)),
    )
    return calls


@pytest.fixture
def wired(qapp, log_calls) -> Wired:
    return Wired(qapp, log_calls)


MAIN_THREAD = threading.main_thread().ident


# ============ 시작 ============


class TestStart:
    def test_submit_carries_shared_data_and_running_precedes_submit(self, wired):
        """submit의 content는 공유 데이터의 것이고, 제출 시점에 이미 RUNNING·정보 로깅 완료다."""
        item = _make_item()
        wired.start(item)

        sub = wired.submission
        assert sub["content"] is sub["data"].content
        assert sub["content"].url == item.vod_url
        assert sub["content"].output_path == item.output_path
        assert isinstance(sub["task_logger"], DownloadLogger)
        for key in ("on_progress", "on_finished", "on_failed", "on_merge_start"):
            assert callable(sub[key]), key
        # 순서 계약: 전이·로깅 → 제출 (제출 시점 스냅샷)
        assert wired.service.state_at_submit is DownloadState.RUNNING
        assert wired.service.log_calls_at_submit == 1
        assert wired.log_calls == [("log_download_info", item)]

    def test_start_reflects_running_without_calling_content(self, wired):
        """시작은 content를 부르지 않는다 — 카드의 시작 반영은 mainWindow가 따로 한다."""
        item = _make_item()
        wired.start(item)
        wired.pump()

        assert wired.content.calls == []
        assert item.downloadState is DownloadState.RUNNING
        assert wired.vm.isDownloading() is True
        assert wired.vm.task is not None
        assert wired.vm.task.state is DownloadState.RUNNING


# ============ 일시정지 · 재개 · 중지 ============


class TestControls:
    def test_pause_then_resume_notify_after_transition(self, wired):
        """pause → resume 각 1회, 통지 시점에 모델은 이미 전이돼 있다 (카드가 새 상태로 그린다)."""
        item = _make_item()
        wired.start(item)

        wired.vm.pause()
        wired.pump()
        wired.vm.resume()
        wired.pump()

        assert wired.content.calls == [
            ("pause", (item,), MAIN_THREAD, DownloadState.PAUSED),
            ("resume", (item,), MAIN_THREAD, DownloadState.RUNNING),
        ]

    def test_stop_notifies_once_in_waiting_state(self, wired):
        """중지는 stop 1회, 통지 시점에 WAITING, 병합 표시 해제. 참조 정리는 removeThreads 몫이라 아직 살아 있다."""
        item = _make_item("m3u8")
        wired.start(item)
        item.post_process = True

        wired.vm.stop()
        wired.pump()

        assert wired.content.calls == [("stop", (item,), MAIN_THREAD, DownloadState.WAITING)]
        assert item.post_process is False
        assert (
            wired.vm.isDownloading() is True
        )  # mainWindow.stopDownload가 이어서 removeThreads를 부른다

    def test_cleanup_after_stop_waits_once_and_keeps_slot(self, wired):
        """정상 종료 워커: wait 1회(유한 timeout), abandon 없음, 참조 정리."""
        item = _make_item()
        wired.start(item)
        wired.vm.stop()

        wired.vm.removeThreads()

        assert wired.handle.wait_calls == 1
        assert all(t is not None and t > 0 for t in wired.handle.wait_timeouts)
        assert wired.service.abandoned == []
        assert wired.vm.isDownloading() is False
        assert wired.vm.task is None
        assert wired.content.names == ["stop"]


# ============ 완료 ============


class TestCompletion:
    def test_finish_carries_elapsed_time_after_refs_are_cleared(self, wired):
        """완료: finish(item, "HH:MM:SS") 1회. 서비스 계약대로 모델 FINISHED 뒤에 콜백이 온다."""
        item = _make_item()
        wired.start(item)

        wired.submission["data"].model.finish()  # DownloadService._handle_finished의 순서
        wired.submission["on_finished"]()
        wired.pump()

        assert wired.content.calls == [
            ("finish", (item, "00:01:01"), MAIN_THREAD, DownloadState.FINISHED)
        ]
        assert wired.handle.wait_calls == 1
        assert wired.service.abandoned == []
        assert wired.vm.isDownloading() is False
        assert wired.vm.task is None

    def test_late_finished_after_user_cleanup_is_ignored(self, wired):
        """중지·정리 뒤 늦게 도착한 완료는 content에 닿지 않는다."""
        item = _make_item()
        wired.start(item)
        wired.vm.stop()
        wired.vm.removeThreads()
        wired.pump()

        wired.submission["on_finished"]()
        wired.pump()

        assert wired.content.names == ["stop"]
        assert item.downloadState is DownloadState.WAITING

    def test_late_failed_after_user_cleanup_is_ignored(self, wired):
        """중지·정리 뒤 늦게 도착한 실패도 마찬가지다."""
        item = _make_item()
        wired.start(item)
        wired.vm.stop()
        wired.vm.removeThreads()
        wired.pump()

        wired.submission["on_failed"](RuntimeError("late"))
        wired.pump()

        assert wired.content.names == ["stop"]
        assert item.downloadState is DownloadState.WAITING


# ============ 실패 ============


class _FakeHttpResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


def _http_error(status: int) -> requests.HTTPError:
    return requests.HTTPError(f"HTTP {status}", response=_FakeHttpResponse(status))


def _postprocess_error(cause: BaseException | None) -> PostprocessError:
    exc = PostprocessError("후처리(remux) 실패: ffmpeg stderr tail... [C:\\tools\\ffmpeg.exe]")
    exc.__cause__ = cause
    return exc


class TestFailure:
    def test_mapped_failure_calls_fail_only_without_stop_or_wait(self, wired):
        """실패: fail 1회(stop 없음), 사유는 매핑 문구, 엔진은 WAITING 신호, 메인 스레드는 기다리지 않는다."""
        item = _make_item("m3u8")
        wired.start(item)
        item.post_process = True
        model = wired.submission["data"].model

        wired.submission["on_failed"](
            _postprocess_error(RemuxError("exit 183: Invalid data found"))
        )
        wired.pump()

        [(name, (failed_item, message), ident, state)] = wired.content.calls
        assert name == "fail"
        assert failed_item is item
        assert ident == MAIN_THREAD
        assert state is DownloadState.WAITING  # FAILED 표시는 아이템 레벨(content.fail)의 몫
        assert message.startswith("Segments are corrupted · download the video again\n")
        assert "ffmpeg" not in message and "remux" not in message  # 원시 문자열 미노출
        assert model.state is DownloadState.WAITING  # 실행 루프의 종료 신호
        assert wired.handle.wait_calls == 0  # 죽은 마운트 프리즈 회귀 방지 (PR #135)
        assert item.post_process is False
        assert wired.vm.isDownloading() is False
        assert wired.vm.task is None

    def test_unmapped_failure_carries_empty_reason(self, wired):
        item = _make_item()
        wired.start(item)

        wired.submission["on_failed"](RuntimeError("raw internal detail"))
        wired.pump()

        assert wired.content.calls == [("fail", (item, ""), MAIN_THREAD, DownloadState.WAITING)]

    @pytest.mark.parametrize(
        ("exc", "headline"),
        [
            (
                _postprocess_error(FFmpegNotFoundError("미설치")),
                "ffmpeg not found · check the installation",
            ),
            (
                _postprocess_error(RemuxError("exit 183")),
                "Segments are corrupted · download the video again",
            ),
            (_postprocess_error(None), "Segments are corrupted · download the video again"),
            (DecryptionError("키·IV 불일치"), "Decryption failed · check your cookies"),
            (_http_error(403), "Viewing permission required · add cookies in Settings"),
            (_http_error(401), "Viewing permission required · add cookies in Settings"),
            (_http_error(404), "Video not found · check the URL"),
            (_http_error(500), "Network connection error · check your connection"),
            (requests.ConnectionError("boom"), "Network connection error · check your connection"),
            (requests.Timeout("slow"), "Network connection error · check your connection"),
            (
                OSError(28, "No space left", "C:\\full\\path.mp4"),
                "Failed to save file · check the path and disk space",
            ),
        ],
        ids=lambda v: v if isinstance(v, str) else type(v).__name__,
    )
    def test_failure_reason_headline_by_exception(self, wired, exc, headline):
        """예외 → 사유 첫 줄(핵심 한 줄) 매핑 — 번역기 미설치 환경이라 원문이 온다."""
        item = _make_item()
        wired.start(item)

        wired.submission["on_failed"](exc)
        wired.pump()

        [(_, (_, message), _, _)] = wired.content.calls
        first_line, _, detail = message.partition("\n")
        assert first_line == headline
        assert detail.strip()  # 둘째 줄=상세 (#245 규약)


# ============ 정리 규칙 — 갇힌 워커 ============


class TestStuckWorker:
    def test_stuck_worker_is_abandoned_after_finite_wait(self, qapp, log_calls):
        """갇힌 워커: 유한 timeout으로 기다린 뒤 슬롯을 방출하고 참조만 정리한다 (#137)."""
        wired = Wired(qapp, log_calls, handle_factory=StuckHandle)
        item = _make_item()
        wired.start(item)

        wired.vm.removeThreads()  # 여기서 영원히 기다리면 앱 프리즈다

        assert wired.handle.wait_calls >= 1
        assert all(t is not None and t > 0 for t in wired.handle.wait_timeouts)
        assert wired.service.abandoned == [wired.handle]
        assert wired.vm.isDownloading() is False
        assert wired.vm.task is None


# ============ 진행 변환 ============


class TestProgress:
    def test_file_progress_is_converted_to_the_four_tuple(self, wired):
        item = _make_item()
        wired.start(item)

        wired.submission["on_progress"](
            ProgressEvent(downloaded_size=50, total_size=100, speed=1.0)
        )
        wired.pump()

        assert wired.content.calls == [
            (
                "update_progress",
                (item, "00:00:00", "50", "1.0 MB/s", 50),
                MAIN_THREAD,
                DownloadState.RUNNING,
            )
        ]

    def test_segment_progress_switches_to_merge_ratio_after_merge_start(self, wired):
        item = _make_item("m3u8")
        wired.start(item)
        sub = wired.submission
        data = sub["data"]
        data.max_threads = 10
        data.completed_threads = 5

        sub["on_progress"](ProgressEvent(downloaded_size=500, speed=0.0))
        wired.pump()
        assert wired.content.calls[-1][1] == (item, "N/A", "500", "0.0 MB/s", 50)

        sub["on_merge_start"]()
        assert item.post_process is True
        data.merged_segments = 11  # m3u8은 초기화 세그먼트 1개를 더 병합한다 (#57)
        sub["on_progress"](ProgressEvent(downloaded_size=500, speed=0.0))
        wired.pump()
        assert wired.content.calls[-1][1] == (item, "N/A", "500", "0.0 MB/s", 100)
        assert len(wired.content.calls) == 2


# ============ 스레드 축 — 워커 콜백은 메인 스레드에서 content에 닿는다 ============


def _call_on_worker(fn, *args) -> int:
    """fn을 별도 스레드에서 실행하고 끝날 때까지 기다린 뒤 그 스레드의 id를 돌려준다."""
    ident: list[int] = []

    def run():
        ident.append(threading.get_ident())
        fn(*args)

    worker = threading.Thread(target=run, name="FakeEngineWorker")
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive(), "콜백이 워커 스레드에서 끝나지 않았다"
    return ident[0]


class TestThreadBoundary:
    """판정은 스레드 식별자 비교다. join() 뒤 processEvents() 전에는 도착이 없어야 하고
    (큐에 있을 뿐), 도착한 호출의 스레드는 메인이어야 한다. 콜백을 직접 호출로
    바꾸면 join() 시점에 이미 워커 id로 도착해 있어 두 단언 모두 실패한다."""

    def test_progress_from_worker_arrives_on_main_thread(self, wired):
        item = _make_item()
        wired.start(item)
        event = ProgressEvent(downloaded_size=50, total_size=100, speed=1.0)

        worker_ident = _call_on_worker(wired.submission["on_progress"], event)

        assert worker_ident != MAIN_THREAD
        assert wired.content.calls == [], (
            "processEvents 전에 content가 불렸다 — 워커 스레드 직접 호출"
        )
        wired.pump()
        [(name, args, ident, _)] = wired.content.calls
        assert name == "update_progress"
        assert args == (item, "00:00:00", "50", "1.0 MB/s", 50)
        assert ident == MAIN_THREAD

    def test_finished_from_worker_arrives_on_main_thread(self, wired):
        item = _make_item()
        wired.start(item)
        wired.submission["data"].model.finish()

        worker_ident = _call_on_worker(wired.submission["on_finished"])

        assert worker_ident != MAIN_THREAD
        assert wired.content.calls == []
        assert wired.vm.isDownloading() is True  # 후처리(참조 정리)도 메인 스레드 몫이라 아직이다
        wired.pump()
        assert wired.content.calls == [
            ("finish", (item, "00:01:01"), MAIN_THREAD, DownloadState.FINISHED)
        ]
        assert wired.vm.isDownloading() is False

    def test_failed_from_worker_arrives_on_main_thread(self, wired):
        item = _make_item("m3u8")
        wired.start(item)
        item.post_process = True

        worker_ident = _call_on_worker(wired.submission["on_failed"], RuntimeError("boom"))

        assert worker_ident != MAIN_THREAD
        # 병합 표시 해제(데이터 플래그)는 콜백에서 바로 — 문서화된 유일한 예외
        assert item.post_process is False
        assert wired.content.calls == []
        assert wired.vm.isDownloading() is True
        wired.pump()
        assert wired.content.calls == [("fail", (item, ""), MAIN_THREAD, DownloadState.WAITING)]
        assert wired.vm.isDownloading() is False

    def test_merge_start_from_worker_sets_flag_synchronously(self, wired):
        """병합 시작은 위젯이 아니라 데이터 플래그만 만지므로 워커 스레드에서 즉시 기록된다 — content 호출은 없다."""
        item = _make_item("m3u8")
        wired.start(item)

        _call_on_worker(wired.submission["on_merge_start"])

        assert item.post_process is True
        wired.pump()
        assert wired.content.calls == []
