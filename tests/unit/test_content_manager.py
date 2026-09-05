"""ContentManager의 메타데이터 조회 상태(LOADING) 게이트 검증 (#124).

조회 중 아이템은 다운로드 대상에서 제외되고, 조회 완료 시 같은 자리에서
완성된 아이템으로 교체되며, 조회 실패·조회 중 삭제가 안전해야 한다.

교체 경로 테스트는 핸들러 직접 호출이 아니라 fetchContent가 만드는 실제
시그널 배선(스레드풀 emit → 큐 전달)을 그대로 탄다. 워커 참조를 테스트가
잡아 두지 않는 것이 중요하다 — run() 종료 직후 워커가 파괴될 때 전달이
유실되는 회귀(#124 스모크 실패)를 재현하는 조건이기 때문이다.
외부 API 실호출은 하지 않는다 (가짜 워커 + 네트워크 차단).
"""

import pytest
from PySide6.QtCore import QObject, Signal

import content.manager as manager_mod
from app.viewmodels.data import ContentItem
from content.manager import ContentManager
from app.widgets.view import ContentListView
from core.models.download_state import DownloadState


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """위젯의 썸네일·파일 크기 조회 스레드가 실네트워크에 나가지 않게 차단한다."""

    class _FailingSession:
        def head(self, *args, **kwargs):
            raise RuntimeError("network disabled in tests")

        def get(self, *args, **kwargs):
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())
    monkeypatch.setattr("content.network.get_thread_session", lambda: _FailingSession())


class FakeWorker(QObject):
    """ContentWorker와 같은 시그널 계약의 가짜 워커. 풀 스레드에서 emit한다."""

    finished = Signal(object, str)
    error = Signal(str)

    #: None이면 성공 결과를, 문자열이면 해당 메시지로 error를 emit한다
    fail_message = None

    def __init__(self, vod_url, cookies, downloadPath):
        super().__init__()
        self.vod_url = vod_url
        self.downloadPath = downloadPath

    def run(self):
        if self.fail_message is not None:
            self.error.emit(self.fail_message)
            return
        result = (
            self.vod_url,
            _metadata("제목:" + self.vod_url),
            [["1080", "http://example.invalid/1080"]],
            "1080",
            "http://example.invalid/1080",
            self.downloadPath,
            None,
        )
        self.finished.emit(result, "video")


@pytest.fixture
def manager(qapp, monkeypatch):
    monkeypatch.setattr(manager_mod, "ContentWorker", FakeWorker)
    monkeypatch.setattr(FakeWorker, "fail_message", None)
    view = ContentListView()
    manager = ContentManager(view)
    yield manager
    manager.threadpool.waitForDone(3000)
    qapp.processEvents()
    view.deleteLater()


def _drain(manager, qapp):
    """풀 스레드의 emit이 큐 전달로 메인 스레드에 도착할 때까지 처리한다."""
    assert manager.threadpool.waitForDone(3000)
    for _ in range(20):
        qapp.processEvents()


def _metadata(title="테스트 제목"):
    return {
        "title": title,
        "category": "게임",
        "channelName": "채널",
        "createdDate": "2026-07-29",
        "duration": 60,
    }


def _make_item(state, title="완성 아이템"):
    """지정한 상태의 완성 ContentItem을 만든다."""
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        _metadata(title),
        [["1080", "http://example.invalid/1080"]],
        "1080",
        "http://example.invalid/1080",
        ".",
        "video",
        None,
    )
    item.downloadState = state
    return item


class TestFindItemGate:
    def test_loading_item_is_not_download_target(self, manager, qapp):
        manager.fetchContent("https://chzzk.naver.com/video/1", {}, ".")
        # 워커가 끝나기 전 = LOADING 상태 (emit은 아직 전달되지 않음)
        found, item, _ = manager.findItem()
        assert not found
        assert item is None
        _drain(manager, qapp)

    def test_waiting_item_is_still_found_when_another_is_loading(self, manager, qapp):
        """일부만 로딩 중일 때 완성된 항목은 정상 다운로드 대상이어야 한다."""
        waiting = _make_item(DownloadState.WAITING)
        manager.model.addItem(waiting)
        manager.fetchContent("https://chzzk.naver.com/video/2", {}, ".")
        found, item, _ = manager.findItem()
        assert found
        assert item is waiting
        _drain(manager, qapp)

    def test_finished_and_failed_are_still_skipped(self, manager):
        manager.model.addItem(_make_item(DownloadState.FINISHED))
        manager.model.addItem(_make_item(DownloadState.FAILED))
        found, _, _ = manager.findItem()
        assert not found

    def test_has_loading_items(self, manager, qapp):
        assert not manager.hasLoadingItems()
        manager.fetchContent("https://chzzk.naver.com/video/1", {}, ".")
        assert manager.hasLoadingItems()
        _drain(manager, qapp)
        assert not manager.hasLoadingItems()


class TestFetchSignalPath:
    """실제 시그널 배선을 타는 교체 경로 검증 — 워커 참조는 잡지 않는다."""

    def test_placeholder_becomes_waiting_item_after_fetch(self, manager, qapp):
        manager.fetchContent("https://chzzk.naver.com/video/1", {}, ".")
        _drain(manager, qapp)

        assert manager.model.rowCount() == 1
        item = manager.model.items[0]
        assert item.downloadState == DownloadState.WAITING
        assert item.title == "제목:https://chzzk.naver.com/video/1"
        assert not manager._pendingPlaceholders

    def test_multiple_urls_all_complete_in_order(self, manager, qapp):
        """스모크 실패 재현 조건: URL 3개 연속 추가 → 전부 완성 카드로 교체."""
        urls = [f"https://chzzk.naver.com/video/{i}" for i in range(3)]
        for url in urls:
            manager.fetchContent(url, {}, ".")
        _drain(manager, qapp)

        assert manager.model.rowCount() == 3
        for url, item in zip(urls, manager.model.items):
            assert item.downloadState == DownloadState.WAITING, url
            assert item.vod_url == url

    def test_replacement_keeps_row_position(self, manager, qapp):
        first = _make_item(DownloadState.WAITING, title="앞 아이템")
        manager.model.addItem(first)
        manager.fetchContent("https://chzzk.naver.com/video/2", {}, ".")
        last = _make_item(DownloadState.WAITING, title="뒤 아이템")
        manager.model.addItem(last)
        _drain(manager, qapp)

        assert manager.model.rowCount() == 3
        assert manager.model.items[0] is first
        assert manager.model.items[1].downloadState == DownloadState.WAITING
        assert manager.model.items[1].vod_url == "https://chzzk.naver.com/video/2"
        assert manager.model.items[2] is last

    def test_result_is_discarded_when_placeholder_was_deleted(self, manager, qapp):
        manager.fetchContent("https://chzzk.naver.com/video/1", {}, ".")
        placeholder = manager.model.items[0]
        manager.removeItem(placeholder)
        _drain(manager, qapp)

        assert manager.model.rowCount() == 0
        assert not manager._pendingPlaceholders


class TestFetchErrorPath:
    def test_placeholder_is_removed_and_error_emitted(self, manager, qapp, monkeypatch):
        monkeypatch.setattr(FakeWorker, "fail_message", "boom")
        errors = []
        manager.contentError.connect(errors.append)

        manager.fetchContent("https://chzzk.naver.com/video/1", {}, ".")
        assert manager.model.rowCount() == 1  # 자리표시는 먼저 뜬다
        _drain(manager, qapp)

        assert manager.model.rowCount() == 0
        assert errors == ["boom"]
        assert not manager._pendingPlaceholders

    def test_error_after_manual_delete_only_emits_message(self, manager, qapp, monkeypatch):
        monkeypatch.setattr(FakeWorker, "fail_message", "boom")
        errors = []
        manager.contentError.connect(errors.append)

        manager.fetchContent("https://chzzk.naver.com/video/1", {}, ".")
        manager.removeItem(manager.model.items[0])
        _drain(manager, qapp)

        assert manager.model.rowCount() == 0
        assert errors == ["boom"]
