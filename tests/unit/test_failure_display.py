"""다운로드 실패의 카드 표시·배치 계속 검증 (#134) — 실배선 통과.

핸들러 직접 호출 테스트는 시그널 연결·객체 수명을 검증하지 못한 전례가
있다 (#125). 여기서는 mainWindow.setupThreadSignals와 동일한 배선을 최소
하네스로 재현해, 엔진 실패 콜백 → 브리지 내부 Signal → failed Signal →
ContentManager.fail → 뷰/위젯 갱신 → 배치 계속까지를 실제 시그널 체인으로
지나간다. 다운로드 실행은 페이크 서비스로 대체한다 (실네트워크 없음).
"""

import pytest
from PySide6.QtCore import QObject

from content.data import ContentItem
from content.manager import ContentManager
from content.view import ContentListView
from core.downloaders.base import PostprocessError
from download.qt_bridge import QtDownloadBridge
from download.state import DownloadState


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
    widget = view.indexWidget(manager.model.index(0, 0))
    label = widget.statusLabel.text()
    assert label.startswith("Download failed")  # 번역기 미설치 — 키 원문
    assert "Postprocessing failed" in label  # 사유가 함께 보인다
    # ② 원시 문자열(ffmpeg stderr·실행 경로) 미노출
    assert "ffmpeg" not in label
    assert "remux" not in label
    # 빨간 프레임 — 완료(파란)와도 구분된다
    assert "#FF6969" in widget.contentFrame.styleSheet()

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
    widget = view.indexWidget(manager.model.index(0, 0))
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
