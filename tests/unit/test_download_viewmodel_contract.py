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
from app.download_logger import DownloadLogger

# ============ 대역 ============


class FakeHandle:
    """DownloadHandle(서비스가 돌려주는 실행 핸들) 대역 — 정리 규칙의 증거를 모은다.

    기록하는 것은 `wait` 호출 횟수와 그때의 timeout 인자다. 정리 규칙은 셋이고
    전부 이 두 값으로 판정한다: 정상 종료·중지는 wait **1회**, 실패 경로는
    wait **0회**(죽은 마운트 I/O에 갇힌 엔진을 메인 스레드가 기다리면 UI가
    얼어붙는다 — PR #135), 그리고 어느 경우든 timeout은 **유한**(None이면 무한
    대기 = 앱 프리즈, #137). 실제 핸들은 스레드 이벤트를 기다리지만 여기서는
    즉시 True를 돌려준다 — 엔진이 없으므로 기다릴 대상이 없다.
    """

    def __init__(self, data):
        self.data = data
        self.wait_calls = 0
        self.wait_timeouts: list = []

    def elapsed_seconds(self) -> float:
        """완료 통지에 실리는 소요 시간 — 61초로 고정해 "00:01:01" 서식 변환까지 잰다."""
        return 61.0

    def wait(self, timeout=None) -> bool:
        """엔진 종료 대기의 대역 — 호출 사실과 timeout만 남기고 즉시 끝난 것으로 답한다."""
        self.wait_calls += 1
        self.wait_timeouts.append(timeout)
        return True


class StuckHandle(FakeHandle):
    """파일 I/O에 갇힌 워커 흉내 — wait가 절대 끝나지 않는다 (#136·#137)."""

    def wait(self, timeout=None) -> bool:
        """기록은 남기되 "끝나지 않았다"(False)로 답한다 — 호출자가 포기하고 슬롯을 방출해야 한다."""
        super().wait(timeout)
        return False


class FakeService:
    """DownloadService(core 오케스트레이터) 대역 — 위쪽 경계의 관찰 지점.

    실제 서비스는 다운로더를 골라 워커 스레드에서 돌리지만, 여기서는 아무것도
    실행하지 않고 두 가지만 남긴다:

    - `submit` 인자 전부(`submissions`) — 콜백 4개는 테스트가 꺼내 **엔진 대신**
      직접(또는 별도 스레드에서) 호출한다. 그래서 이 기록이 곧 "엔진이 통지하는
      입구"다.
    - **제출 시점의 스냅샷**(`state_at_submit`·`log_calls_at_submit`) — "제출 전에
      RUNNING 전이와 다운로드 정보 로깅이 끝나 있다"는 순서 계약은 제출이 끝난
      뒤에 검사하면 안 보인다(그때는 어느 순서였든 둘 다 참이다).

    `abandon`은 호출된 핸들만 모은다 — 정상 종료에서는 비어 있어야 하고, 갇힌
    워커에서는 정확히 그 핸들 하나여야 한다(#137의 슬롯 방출).
    """

    def __init__(self, log_calls: list, handle_factory=FakeHandle):
        """log_calls: `log_calls` 픽스처의 기록 리스트(제출 시점 길이를 잰다).
        handle_factory: 돌려줄 핸들 대역 — 갇힌 워커 시나리오만 StuckHandle을 준다."""
        self._log_calls = log_calls
        self._handle_factory = handle_factory
        self.submissions: list[dict] = []
        self.handles: list[FakeHandle] = []
        self.abandoned: list = []
        self.state_at_submit: DownloadState | None = None
        self.log_calls_at_submit: int | None = None

    def submit(self, content, **kwargs):
        """제출 시점 스냅샷을 먼저 뜨고, 인자를 보관한 뒤, 핸들 대역을 돌려준다."""
        self.state_at_submit = kwargs["data"].model.state
        self.log_calls_at_submit = len(self._log_calls)
        self.submissions.append({"content": content, **kwargs})
        handle = self._handle_factory(kwargs["data"])
        self.handles.append(handle)
        return handle

    def abandon(self, handle):
        """갇힌 워커의 슬롯 방출 — 호출된 핸들을 남긴다."""
        self.abandoned.append(handle)


class RecordingContent(QObject):
    """content 자리(제품에서는 ContentManager)의 기록 대역 — 아래쪽 경계의 관찰 지점.

    viewmodel이 부르는 6개 메서드를 같은 시그니처로 받되 카드를 그리는 대신
    `(호출 이름, 인자, 호출 스레드 id, 호출 시점 아이템 상태)` 한 줄을 남긴다.
    네 항목이 각각 다른 계약을 증명한다:

    - **이름·인자**: 어느 시나리오에 어떤 통지가 몇 번 오는가(실패는 fail 1회이고
      stop은 없다, 완료는 "HH:MM:SS"를 싣는다 등). 리스트 전체를 통째로 단언해
      빠진 것·덧붙은 것을 함께 잡는다.
    - **스레드 id**: 이 호출 뒤에는 위젯 갱신이 있으므로 메인 스레드여야 한다.
      기록해 두면 어느 스레드에서 왔는지가 사실로 남고, 단언은 그 사실을 메인
      스레드 id와 비교하기만 한다(스레드 축 테스트 참조).
    - **호출 시점의 `item.downloadState`**: 카드는 이 호출을 받는 순간의 상태로
      그린다. 모델 전이보다 통지가 먼저 오면 카드가 이전 상태로 그려지므로
      "전이 → 통지" 순서는 content 쪽에서 관찰 가능한 계약이다.

    QObject인 이유: 제품의 ContentManager가 QObject라 Signal 연결의 스레드
    친화성이 같아야 한다 — 큐 연결의 도착 스레드는 수신자가 사는 스레드로
    정해지므로, 수신자가 메인 스레드의 QObject여야 "메인 스레드 도착"이 제품과
    같은 조건에서 검증된다.
    """

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, tuple, int, DownloadState]] = []

    def _record(self, name: str, item: ContentItem, *args) -> None:
        """한 호출을 네 항목으로 남긴다 — 스레드 id와 아이템 상태는 **지금 이 순간**의 값이다."""
        self.calls.append((name, (item, *args), threading.get_ident(), item.downloadState))

    def update_progress(self, rem, size, spd, prog, item: ContentItem):
        """진행 통지 자리 — 인자 순서(남은 시간·크기·속도·%·item)는 ContentManager와 같다."""
        self._record("update_progress", item, rem, size, spd, prog)

    def pause(self, item: ContentItem):
        """일시정지 통지 자리."""
        self._record("pause", item)

    def resume(self, item: ContentItem):
        """재개 통지 자리."""
        self._record("resume", item)

    def stop(self, item: ContentItem):
        """중지 통지 자리 — 실패 경로에서는 오지 않아야 한다."""
        self._record("stop", item)

    def finish(self, item: ContentItem, download_time: str):
        """완료 통지 자리 — download_time은 "HH:MM:SS" 문자열이다."""
        self._record("finish", item, download_time)

    def fail(self, item: ContentItem, message: str = ""):
        """실패 통지 자리 — message는 매핑을 거친 사유 문구(매핑 밖이면 "")다."""
        self._record("fail", item, message)

    @property
    def names(self) -> list[str]:
        """호출 이름만 순서대로 — 인자·스레드까지 볼 필요 없는 단언용."""
        return [c[0] for c in self.calls]


def _make_item(content_type: str = "video") -> ContentItem:
    """카드 한 장의 최소 아이템 — content_type이 파일("video")/세그먼트("m3u8") 변환 분기를 가른다.

    output_path는 제품에서 다운로드 직전에 채워지는 값이라 여기서 직접 넣는다 — submit 인자 대조에 쓴다.
    """
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
    """다운로드 한 건의 배선 묶음 — 제품 배선(DownloadViewModel ↔ ContentManager ↔ DownloadService)에서
    양 끝만 대역으로 바꾼 것.

    viewmodel은 실물이고 그 위·아래가 기록 대역이다. 테스트는 viewmodel의 공개
    API로 조작하고, 두 대역의 기록으로만 판정한다 — 브리지의 이름·속성에는
    손대지 않는다. 그래서 흡수(B1)로 내부가 통째로 바뀌어도 이 클래스와 그 위의
    테스트는 그대로다.
    """

    def __init__(self, qapp, log_calls: list, handle_factory=FakeHandle):
        """qapp: 큐 연결을 배달할 이벤트 루프. log_calls: `log_calls` 픽스처의 기록.
        handle_factory: 서비스 대역이 돌려줄 핸들 종류(갇힌 워커 시나리오용)."""
        self.qapp = qapp
        self.content = RecordingContent()
        self.service = FakeService(log_calls, handle_factory)
        self.vm = DownloadViewModel(self.content, service=self.service)
        self.log_calls = log_calls

    @property
    def submission(self) -> dict:
        """첫 제출의 인자 — 여기서 꺼낸 콜백 4개가 "엔진이 통지하는 입구"다."""
        return self.service.submissions[0]

    @property
    def handle(self) -> FakeHandle:
        """서비스가 돌려준 핸들 — 정리 규칙(wait·abandon) 관찰용. 서비스 쪽 기록이다."""
        return self.service.handles[0]

    def start(self, item: ContentItem) -> None:
        """viewmodel 공개 API로 시작 — 제출·핸들 기록은 서비스 대역이 남긴다."""
        self.vm.start(item)

    def pump(self) -> None:
        """큐에 쌓인 Signal을 배달한다 — 워커 스레드에서 emit된 통지는 이 호출 전에는 content에 닿지 않는다."""
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
    """정상 핸들(FakeHandle)로 배선한 기본 묶음 — 갇힌 워커 시나리오만 직접 Wired를 만든다."""
    return Wired(qapp, log_calls)


MAIN_THREAD = threading.main_thread().ident


# ============ 시작 ============


class TestStart:
    """시작 계약 — 서비스에 무엇이 어떤 상태로 제출되는가, 그리고 content는 아직 조용한가."""

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
    """유저 조작(일시정지·재개·중지)의 통지 — 횟수와 "전이 → 통지" 순서, 중지 뒤 정리 규칙."""

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
    """완료 통지와, 정리가 끝난 뒤 늦게 도착한 통지의 무시."""

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
    """HTTPError에 실을 상태 코드만 가진 응답 흉내 — 사유 매핑이 보는 것은 status_code뿐이다."""

    def __init__(self, status_code: int):
        self.status_code = status_code


def _http_error(status: int) -> requests.HTTPError:
    """상태 코드가 붙은 HTTPError — 401/403/404/그 밖이 서로 다른 사유로 갈리는지 재는 재료."""
    return requests.HTTPError(f"HTTP {status}", response=_FakeHttpResponse(status))


def _postprocess_error(cause: BaseException | None) -> PostprocessError:
    """원인을 체인한 PostprocessError — 사유는 예외 자체가 아니라 `__cause__`의 종류로 갈린다 (#180).

    메시지에 ffmpeg 경로·stderr를 일부러 넣는다: 그것이 유저 문구에 새지 않는 것도 단언한다.
    """
    exc = PostprocessError("후처리(remux) 실패: ffmpeg stderr tail... [C:\\tools\\ffmpeg.exe]")
    exc.__cause__ = cause
    return exc


class TestFailure:
    """실패 통지 — fail 하나만 오고(stop 없음), 사유는 매핑 문구이며 원시 예외 문자열은 새지 않는다."""

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
    """정리 규칙 중 병리 경로 — 끝나지 않는 워커를 유한하게 기다리고 포기한다 (#136·#137)."""

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
    """ProgressEvent → (남은 시간, 크기, 속도, %) 변환값 — 파일은 바이트 기준, 세그먼트는 개수 기준(병합 단계 전환 포함)."""

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
    """엔진 워커 스레드를 흉내 낸다 — fn을 별도 스레드에서 실행하고 **끝날 때까지 기다린 뒤** 그 스레드 id를 돌려준다.

    join()이 판정의 전제다: 워커가 이미 끝났으므로 그 뒤에 content에 아무것도
    없다면 그것은 "아직 안 왔다"가 아니라 "큐에 있다"이고, 그 뒤 processEvents로
    배달된 호출의 스레드 id는 메인일 수밖에 없다. 워커가 살아 있으면 판정이
    타이밍에 기대게 되므로 살아 있을 때는 실패시킨다.

    돌려주는 id는 "정말 다른 스레드였다"는 대조군이다 — 이것이 메인과 같으면
    테스트 자체가 아무것도 안 잰 것이다.
    """
    ident: list[int] = []

    def run():
        """스레드 본체 — 자기 id를 남기고 콜백을 부른다."""
        ident.append(threading.get_ident())
        fn(*args)

    worker = threading.Thread(target=run, name="FakeEngineWorker")
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive(), "콜백이 워커 스레드에서 끝나지 않았다"
    return ident[0]


class TestThreadBoundary:
    """스레드 축 — 워커 스레드의 콜백이 content에는 **메인 스레드에서** 닿는가. 이 파일의 핵심이다.

    왜 재는가: content 뒤에는 위젯 갱신이 있다. 콜백을 Signal emit 없이 직접
    호출로 바꾸면(흡수 중 떠오르는 단순화) 위젯이 워커 스레드에서 갱신되고,
    그 고장은 조용하고 산발적이라 다른 어떤 테스트도 잡지 못한다. 지금은
    큐 연결이 지키는데, 그 장치가 흡수 뒤에도 남았는지를 여기서만 잰다.

    단언이 두 단계인 이유(각 테스트가 같은 모양이다):
    1. `_call_on_worker`로 콜백을 돌리고 join한 직후 — content 호출이 **0건**.
       워커는 이미 끝났으므로 통지는 큐에 있거나(정상) 이미 워커에서 배달됐거나
       (고장) 둘 중 하나다. 0건이면 전자다.
    2. `pump()`(processEvents) 뒤 — 도착한 호출의 **스레드 id가 메인**과 같다.
       1이 통과해도 2가 필요하다: 큐에 있다가 배달된 것이 메인 스레드에서 불렸다는
       사실을 기록으로 확인해야 "큐 연결"이 증명된다.

    타이밍이 아니라 식별자로 판정하는 이유: "잠깐 기다렸는데 안 왔다"는 느린
    CI에서 거짓 통과(아직 안 왔을 뿐)나 거짓 실패(이미 왔다)를 낳는다. join으로
    워커를 끝내 두면 시간 축이 사라지고, 남는 것은 "어느 스레드에서 불렸는가"라는
    사실뿐이다. 그 사실은 RecordingContent가 호출 순간의 id로 남긴다.
    """

    def test_progress_from_worker_arrives_on_main_thread(self, wired):
        """진행 통지 — viewmodel의 바깥 연결(progress → content)이 큐 연결인가.

        ④a 고장(연결을 DirectConnection으로)을 잡는 유일한 케이스다: 완료·실패와
        달리 진행은 후처리 없이 한 번의 연결로 content에 닿는다.
        """
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
        """완료 통지 — 참조 정리(후처리)까지 메인 스레드 몫이라 pump 전에는 `isDownloading()`도 그대로다.

        진행과 달리 완료는 후처리 슬롯을 거쳐 content에 닿는다. 콜백이 그 슬롯을
        직접 부르면(④b 고장) 정리와 통지가 워커에서 일어나 두 단언 모두 실패한다.
        """
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
        """실패 통지 — 완료와 같은 후처리 경로. 단 병합 표시 해제만은 콜백에서 즉시 한다.

        `post_process`는 위젯이 아니라 진행 변환용 데이터 플래그라 워커에서 바로
        내려도 된다고 문서화돼 있다. 그래서 join 직후 이미 False이고, content
        호출(fail)은 여전히 pump 뒤 메인 스레드다 — 둘을 같은 테스트에서 갈라 둔다.
        """
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
