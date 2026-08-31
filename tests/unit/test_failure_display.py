"""다운로드 실패의 카드 표시·배치 계속 검증 (#134) — 실배선 통과.

핸들러 직접 호출 테스트는 시그널 연결·객체 수명을 검증하지 못한 전례가
있다 (#125). 여기서는 mainWindow.setupThreadSignals와 동일한 배선을 최소
하네스로 재현해, 엔진 실패 콜백 → 브리지 내부 Signal → failed Signal →
ContentManager.fail → 뷰/위젯 갱신 → 배치 계속까지를 실제 시그널 체인으로
지나간다. 다운로드 실행은 페이크 서비스로 대체한다 (실네트워크 없음).
"""

import time

import pytest
from PySide6.QtCore import QObject

import main as main_module
import theme
from content.data import ContentItem
from content.manager import ContentManager
from content.view import ContentListView
from core.downloaders.base import PostprocessError
from download.qt_bridge import QtDownloadBridge
from core.models.download_state import DownloadState


@pytest.fixture(autouse=True)
def _apply_dark_card_qss(qapp):
    """카드 테두리 색 검증(아래 실패 카드 border_pixel 확인)이 실제
    프로덕션 스타일시트를 필요로 해서 이 파일에만 국소 적용한다.

    ⚠️⚠️⚠️ **반드시 `scope="function"`(기본값)으로 유지할 것 — 넓히면
    macOS CI에서 프로세스 종료 시점 SIGSEGV가 재발한다.** 상세 근거·
    실측 경계·크래시 스택은 `tests/unit/test_widget_theme.py`의 같은
    이름 픽스처 docstring 참고 — 요약: 범위(몇 개 테스트)가 아니라
    수명(만든 `theme.build_style()` 객체가 테스트 하나를 넘어 `qapp`에
    계속 걸려 있는가)이 경계다. `scope="function"`만 macOS 통과를
    재현했다(session·module 스코프는 좁혀도 둘 다 크래시 재현됨).
    """
    theme.set_color_scheme("dark")
    qapp.setStyle(theme.build_style())  # 검증 ②: 이 파일만 우회 제외 — 크래시가 여기서 나는지, 다른 지점으로 옮겨 가는지
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """위젯의 썸네일·파일 크기 조회 스레드가 실네트워크에 나가지 않게 차단한다."""

    class _FailingSession:
        def head(self, *args, **kwargs):
            raise RuntimeError("network disabled in tests")

        def get(self, *args, **kwargs):
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("content.widget.get_thread_session", lambda: _FailingSession())
    monkeypatch.setattr("content.network.get_thread_session", lambda: _FailingSession())


class FakeHandle:
    """DownloadHandle 대역 — 브리지가 쓰는 인터페이스만 제공한다."""

    def __init__(self, data):
        self.data = data

    def elapsed_seconds(self) -> float:
        return 1.0

    def wait(self, timeout=None) -> bool:
        return True


class FakeService:
    """DownloadService 대역 — submit 인자를 기록하고 페이크 핸들을 돌려준다."""

    def __init__(self):
        self.submissions: list[dict] = []

    def submit(self, content, **kwargs):
        self.submissions.append({"content": content, **kwargs})
        return FakeHandle(kwargs["data"])


class FakeLogger:
    """DownloadLogger 대역 — 파일 생성 없이 무동작으로 받는다."""

    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class WindowHarness(QObject):
    """mainWindow의 다운로드 배선만 재현한 최소 하네스.

    연결은 mainWindow.setupThreadSignals·startDownload와 동일한 방향이며,
    바운드 메서드로 연결한다 (순수 콜러블 연결의 수명 함정 — CLAUDE 규칙).
    """

    def __init__(self, manager: ContentManager, bridge: QtDownloadBridge):
        super().__init__()
        self.manager = manager
        self.bridge = bridge
        self.started: list[ContentItem] = []
        manager.downloadRequested.connect(self.startDownload)
        bridge.failed.connect(self._onFailed)
        bridge.finished.connect(self._onFinished)

    def startDownload(self, item: ContentItem) -> None:
        # mainWindow.startDownload와 동일: 카드 상태 갱신 → 다운로드 시작
        self.started.append(item)
        self.manager.start(item)
        self.bridge.start(item)

    def _onFailed(self, item: ContentItem, message: str) -> None:
        self.manager.fail(item, message)

    def _onFinished(self, item: ContentItem, download_time: str) -> None:
        self.manager.finish(item, download_time)


def _metadata(title: str) -> dict:
    return {
        "title": title,
        "category": "게임",
        "channelName": "채널",
        "createdDate": "2026-07-30",
        "duration": 60,
    }


def _make_item(download_path: str, title: str) -> ContentItem:
    return ContentItem(
        "https://chzzk.naver.com/video/1",
        _metadata(title),
        [["1080", "http://example.invalid/1080"]],
        "1080",
        "http://example.invalid/1080",
        download_path,
        "video",
        None,
    )


@pytest.fixture
def wired(qapp, monkeypatch, tmp_path):
    """실배선된 (manager, bridge, harness, finished_all 스파이)를 준비한다."""
    monkeypatch.setattr("download.qt_bridge.DownloadLogger", FakeLogger)
    view = ContentListView()
    manager = ContentManager(view)
    bridge = QtDownloadBridge(service=FakeService())
    harness = WindowHarness(manager, bridge)
    finished_all = []
    manager.finishedAllRequested.connect(lambda: finished_all.append(True))
    yield manager, bridge, harness, finished_all, view
    view.deleteLater()
    qapp.processEvents()


def test_failed_card_shows_failure_and_batch_continues(wired, qapp, tmp_path):
    """완료 조건 ①②③: 실패가 카드에 사유와 함께 표시되고(정지와 구분),
    원시 문자열이 노출되지 않으며, 배치가 다음 항목으로 계속된다."""
    manager, bridge, harness, finished_all, view = wired

    item1 = _make_item(str(tmp_path), "첫 항목")
    item2 = _make_item(str(tmp_path), "둘째 항목")
    manager.model.addItem(item1)
    manager.model.addItem(item2)
    qapp.processEvents()

    manager.downloadItem()  # 배치 시작
    assert harness.started == [item1]

    # 엔진 실패 주입 — 서비스에 등록된 실제 실패 콜백을 통해 시그널 체인을 탄다
    raw = "후처리(remux) 실패: ffmpeg stderr tail... [C:\\tools\\ffmpeg.exe]"
    bridge._service.submissions[0]["on_failed"](PostprocessError(raw))
    qapp.processEvents()

    # ① 실패가 실패로 보인다 — WAITING(정지·대기)이 아니라 FAILED
    assert item1.downloadState is DownloadState.FAILED
    widget = view.widgetFor(manager.model.items[0])
    label = widget.statusLabel.text()
    # #245 상태별 슬롯: 실패 사유가 있으면 "✕ 사유"만 보인다(오너 확정 —
    # "Download failed —" 접두는 사유 없는 경우의 폴백으로만 쓰인다)
    assert label.startswith("✕ ")
    # 첫 줄=핵심(할 일 포함)만 3행에 보인다(#245) — 상세는 툴팁
    assert "corrupted" in label and "download the video again" in label
    assert "\n" not in label, "둘째 줄(상세)이 3행에 새어 나왔다"
    # ② 원시 문자열(ffmpeg stderr·실행 경로) 미노출
    assert "ffmpeg" not in label
    assert "remux" not in label
    # 빨간 실패 표시 — 완료(초록)와도 구분된다. `[state="..."]` 규칙이 옳은
    # 토큰을 쓰는지는 test_theme.py가 QSS 소스로(폰트 무관) 보고, 여기서는
    # 슬롯 라벨이 그 규칙을 타는 동적 속성을 실제로 세팅했는지 확인한다.
    assert widget.statusLabel.property("state") == "failed"
    assert widget.statusLabel.isVisible() or not widget.isVisible()  # 실패 슬롯은 숨겨지지 않는다

    # ③ 배치는 실패 항목에서 멈추지 않고 다음 항목으로 계속된다
    assert harness.started == [item1, item2]
    assert item2.downloadState is DownloadState.RUNNING


def test_stop_still_shows_waiting_not_failed(wired, qapp, tmp_path):
    """유저의 정지는 여전히 대기로 표시된다 — 실패와 시각적으로 구분 (#134)."""
    manager, bridge, harness, _finished_all, view = wired

    item = _make_item(str(tmp_path), "정지 항목")
    manager.model.addItem(item)
    qapp.processEvents()

    manager.downloadItem()
    assert harness.started == [item]

    bridge.stop()
    qapp.processEvents()

    assert item.downloadState is DownloadState.WAITING
    widget = view.widgetFor(manager.model.items[0])
    # 정지 카드에는 실패 표시가 없다
    assert "Download failed" not in widget.statusLabel.text()


def test_all_failed_batch_reaches_end(wired, qapp, tmp_path):
    """마지막 항목이 실패해도 배치 종료 신호가 발화한다 — 조용한 멈춤 없음."""
    manager, bridge, harness, finished_all, _view = wired

    item = _make_item(str(tmp_path), "단독 항목")
    manager.model.addItem(item)
    qapp.processEvents()

    manager.downloadItem()
    bridge._service.submissions[0]["on_failed"](RuntimeError("boom"))
    qapp.processEvents()

    assert item.downloadState is DownloadState.FAILED
    assert finished_all == [True]  # 다음 항목이 없으면 배치 종료로 이어진다
    # 배치 종료 안내 분기(#128 후속 ⑤)의 근거 — 전 항목 실패로 집계된다
    assert manager.downloadResultCounts() == (0, 1)


def test_download_result_counts_reflect_screen_state(wired, qapp, tmp_path):
    """배치 종료 안내 분기용 집계 (#128 후속 ⑤): 화면(모델)의 완료·실패 수를 센다.

    별도 배치 장부가 아니라 화면 상태를 세는 이유: 안내의 역할은 지금 화면에
    보이는 결과와 모순되지 않는 것이고, 배치의 경계는 진행 중 추가·삭제가
    가능해 정확한 장부가 존재하지 않는다 (근거는 PR 본문).
    """
    manager, _bridge, _harness, _finished_all, _view = wired

    finished = _make_item(str(tmp_path), "완료 항목")
    finished.downloadState = DownloadState.FINISHED
    failed = _make_item(str(tmp_path), "실패 항목")
    failed.downloadState = DownloadState.FAILED
    waiting = _make_item(str(tmp_path), "대기 항목")
    for it in (finished, failed, waiting):
        manager.model.addItem(it)
    qapp.processEvents()

    # 대기·로딩은 세지 않는다 — 완료/실패만 안내 분기의 근거다
    assert manager.downloadResultCounts() == (1, 1)


class _HeadResponse:
    """총 크기 조회(HEAD)용 응답 흉내 — prepare()가 파트 분할에 쓴다."""

    headers = {"content-length": str(4 * 1024 * 1024)}

    def raise_for_status(self):
        pass


class _DeadMountSession:
    """마운트가 끊긴 네트워크 드라이브 흉내 — 조회는 성공했지만 전송이 죽는다.

    get()의 OSError(9)는 requests 예외가 아니라 FileDownloader의
    _failure_exceptions에 잡히지 않고 워커에서 전파된다 — 실측 로그
    ([Errno 9] Bad file descriptor)와 동일한 실패 경로
    (_download_completed_callback → on_failed)다.
    """

    def head(self, url, **kwargs):
        return _HeadResponse()

    def get(self, url, **kwargs):
        raise OSError(9, "Bad file descriptor")


def test_dead_mount_worker_failure_does_not_freeze_app(qapp, tmp_path, monkeypatch):
    """프리즈 회귀 재현 (PR #135 코멘트): 워커가 OSError로 죽는 실패는 run 루프가
    살아 있는 중에 통지된다. 종점이 모델을 FAILED로 전이하면 루프가 끝나지 않고
    (WAITING만 종료 신호, FAILED→WAITING 불허), handle.wait()가 메인 스레드를
    영원히 붙잡아 앱이 얼어붙는다.

    실제 네트워크 드라이브 없이 재현하는 근거: 얼림의 원인은 파일 I/O의 OS 수준
    대기가 아니라 "루프 미종료 + 메인 스레드 무한 대기"라는 결정론적 데드락으로
    특정됐다 (진단 스크립트 실측 — model.fail() 후 run() 미종료, model.stop() 후
    3초 내 종료). 따라서 OSError를 던지는 세션만으로 같은 실패 경로가 재현되며,
    OS 수준 무기한 I/O 대기 자체는 흉내 낼 수단이 없어 재현 대상에서 제외한다.

    이 테스트는 실제 DownloadService·FileDownloader·실행 루프를 그대로 쓴다 —
    고장난 종점에서는 processEvents가 handle.wait()에 갇혀 테스트가 매달린다.
    """
    monkeypatch.setattr("download.qt_bridge.DownloadLogger", FakeLogger)
    import core.downloaders.file_downloader as fmod

    monkeypatch.setattr(fmod, "get_thread_session", lambda: _DeadMountSession())

    view = ContentListView()
    manager = ContentManager(view)
    bridge = QtDownloadBridge()  # 실제 서비스 — 페이크 아님
    harness = WindowHarness(manager, bridge)

    item = _make_item(str(tmp_path), "죽은 마운트 항목")
    manager.model.addItem(item)
    qapp.processEvents()

    manager.downloadItem()
    assert harness.started == [item]
    handle = bridge.handle  # 참조 정리 전에 붙잡는다 — 엔진 종료 검증용
    assert handle is not None

    # 워커 실패 → 큐 전달 → 메인 스레드 종점 → 카드 FAILED까지 (상한 10초)
    deadline = time.time() + 10
    while item.downloadState is not DownloadState.FAILED and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.02)

    assert item.downloadState is DownloadState.FAILED  # 앱이 살아서 실패로 끝났다
    # 실행 루프가 실제로 종료됐다 — 루프 미종료(스핀) 회귀 방지.
    # 이 대기는 테스트(메인) 스레드지만 상한이 있고, 위에서 FAILED 도달로
    # 종점 처리가 끝났음이 확인된 뒤다
    assert handle.wait(5), "엔진 run()이 종료되지 않았다 — 실행 루프 미종료 회귀"

    view.deleteLater()
    qapp.processEvents()
