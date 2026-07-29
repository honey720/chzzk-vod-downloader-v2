"""ContentManager의 메타데이터 조회 상태(LOADING) 게이트 검증 (#124).

조회 중 아이템은 다운로드 대상에서 제외되고, 조회 완료 시 같은 자리에서
완성된 아이템으로 교체되며, 조회 실패·조회 중 삭제가 안전해야 한다.
워커 스레드는 돌리지 않고 핸들러를 직접 호출한다 (외부 API 실호출 금지).
"""

import pytest

from content.data import ContentItem
from content.manager import ContentManager
from content.view import ContentListView
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


@pytest.fixture
def manager(qapp):
    view = ContentListView()
    manager = ContentManager(view)
    yield manager
    view.deleteLater()


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


def _add_placeholder(manager, url="https://chzzk.naver.com/video/1"):
    """fetchContent가 만드는 것과 동일한 LOADING 자리표시 아이템을 모델에 넣는다."""
    placeholder = ContentItem(
        url,
        {"title": url, "category": "", "channelName": "", "createdDate": "", "duration": 0},
        [],
        None,
        "",
        ".",
        "",
        None,
    )
    placeholder.downloadState = DownloadState.LOADING
    manager.model.addItem(placeholder)
    return placeholder


def _result_tuple(url="https://chzzk.naver.com/video/1"):
    return (
        url,
        _metadata(),
        [["1080", "http://example.invalid/1080"]],
        "1080",
        "http://example.invalid/1080",
        ".",
        None,
    )


class TestFindItemGate:
    def test_loading_item_is_not_download_target(self, manager):
        _add_placeholder(manager)
        found, item, _ = manager.findItem()
        assert not found
        assert item is None

    def test_waiting_item_is_still_found_when_another_is_loading(self, manager):
        """일부만 로딩 중일 때 완성된 항목은 정상 다운로드 대상이어야 한다."""
        _add_placeholder(manager)
        waiting = _make_item(DownloadState.WAITING)
        manager.model.addItem(waiting)
        found, item, _ = manager.findItem()
        assert found
        assert item is waiting

    def test_finished_and_failed_are_still_skipped(self, manager):
        manager.model.addItem(_make_item(DownloadState.FINISHED))
        manager.model.addItem(_make_item(DownloadState.FAILED))
        found, _, _ = manager.findItem()
        assert not found

    def test_has_loading_items(self, manager):
        assert not manager.hasLoadingItems()
        _add_placeholder(manager)
        assert manager.hasLoadingItems()


class TestWorkerFinished:
    def test_placeholder_is_replaced_in_place_with_waiting_item(self, manager):
        first = _make_item(DownloadState.WAITING, title="앞 아이템")
        manager.model.addItem(first)
        placeholder = _add_placeholder(manager)
        last = _make_item(DownloadState.WAITING, title="뒤 아이템")
        manager.model.addItem(last)

        manager.onWorkerFinished(placeholder, _result_tuple(), "video")

        assert manager.model.rowCount() == 3
        replaced = manager.model.items[1]
        assert replaced is not placeholder
        assert replaced.title == "테스트 제목"
        assert replaced.downloadState == DownloadState.WAITING
        # 교체는 같은 자리에서 — 앞뒤 순서가 유지된다
        assert manager.model.items[0] is first
        assert manager.model.items[2] is last

    def test_result_is_discarded_when_placeholder_was_deleted(self, manager):
        placeholder = _add_placeholder(manager)
        manager.removeItem(placeholder)

        manager.onWorkerFinished(placeholder, _result_tuple(), "video")

        assert manager.model.rowCount() == 0


class TestWorkerError:
    def test_placeholder_is_removed_and_error_emitted(self, manager):
        placeholder = _add_placeholder(manager)
        errors = []
        manager.contentError.connect(errors.append)

        manager.onWorkerError(placeholder, "boom")

        assert manager.model.rowCount() == 0
        assert errors == ["boom"]

    def test_error_after_manual_delete_only_emits_message(self, manager):
        placeholder = _add_placeholder(manager)
        manager.removeItem(placeholder)
        errors = []
        manager.contentError.connect(errors.append)

        manager.onWorkerError(placeholder, "boom")

        assert manager.model.rowCount() == 0
        assert errors == ["boom"]
