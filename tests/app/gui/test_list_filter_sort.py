"""헤더바 상태 필터 · 정렬 · 다운로드 차례 게이트 (#495 — 셸 S1, 설계 §2 · §12.1).

- 칩 숫자는 카드 더하기 · 지우기 · 상태 바뀜을 따른다(상태 바뀜은 카드 갱신 알림 뒤 한 차례 안에)
- 고른 칩을 다시 누르면 전체로 돌아간다
- 정렬 다섯 × 오름 · 내림의 화면 순서. 정렬해도 목록(추가된 순서)은 그대로
- 다운로드 차례 = 화면 순서에서 위부터 첫 대기 카드. 필터로 가린 카드도 든다. 받는 중에 정렬을
  바꾸면 다음 고를 때 다시 본다

카드는 모델에 바로 넣는다(`model.addItem` → 목록 뷰 · 순서 뷰모델 실배선). 칩 · 정렬 메뉴는 실제로
누른다(`click` · 메뉴 `trigger`). 화면 순서는 목록 레이아웃의 위젯 차례로 잰다.
"""

import pytest
from PySide6.QtWidgets import QApplication

import app.system.settings as settings
from app.viewmodels.data import ContentItem
from app.views.mainWindow import VodDownloader
from core.models.download_state import DownloadState
from typing import Never


@pytest.fixture(autouse=True)
def no_network(monkeypatch) -> None:
    """카드의 썸네일 · 크기 조회 스레드가 실네트워크에 나가지 않게 막는다."""

    class _FailingSession:
        def head(self, *args, **kwargs) -> Never:
            raise RuntimeError("network disabled in tests")

        def get(self, *args, **kwargs) -> Never:
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())


@pytest.fixture
def window(qapp, tmp_path):
    win = VodDownloader()
    assert win.saveLocations.choose(str(tmp_path))
    yield win
    win.hide()
    win.deleteLater()
    QApplication.processEvents()


def _item(title: str, channel: str = "채널", date: str = "", path: str = "") -> ContentItem:
    metadata = {"title": title, "category": "", "channelName": channel, "createdDate": date, "duration": 60}
    return ContentItem("https://chzzk.naver.com/video/1", metadata, [], None, "", path, "video", None)


def _add(win, *items, states=None) -> list:
    for n, item in enumerate(items):
        if states:
            item.downloadState = states[n]
        win.contentManager.model.addItem(item)
    QApplication.processEvents()
    return list(items)


def _set_state(win, item, state) -> None:
    """카드 상태를 바꾸고 제품 길과 같이 갱신 알림을 낸다 — 알림 뒤 한 차례를 돌린다."""
    item.downloadState = state
    win.contentManager.model.notifyChanged(item)
    QApplication.processEvents()


def _screen_order(win) -> list:
    """목록 레이아웃의 카드 차례(숨은 카드 포함) — 화면 순서."""
    layout = win.listView._layout
    widgets = [layout.itemAt(n).widget() for n in range(layout.count())]
    return [w.item for w in widgets if w is not None]


def _visible_titles(win) -> list[str]:
    return [item.title for item in _screen_order(win) if win.listView.widgetFor(item).isVisibleTo(win.listView)]


def _chip_counts(win) -> dict:
    return {key: chip.property("count") for key, chip in win.filterChips.items()}


def _sort_menu(win):
    button = win.sortButton
    menu = button.menuWidget
    menu.popup(button.mapToGlobal(button.rect().bottomLeft()))
    QApplication.processEvents()
    return menu


def _pick_sort(win, by: str, direction: str) -> None:
    """정렬 메뉴를 열어 기준을 누르고, 다시 열어 방향을 누른다."""
    for key in (("by", by), ("direction", direction)):
        menu = _sort_menu(win)
        next(a for a in menu.choices() if tuple(a.data()) == key).trigger()
        menu.hide()
    QApplication.processEvents()


class TestChipCounts:
    def test_counts_follow_adding_cards(self, window) -> None:
        """대기 둘 · 완료 하나 · 실패 하나를 넣으면 칩 숫자는 전체 4 · 대기 2 · 완료 1 · 실패 1이어야 한다."""
        D = DownloadState
        _add(window, _item("가"), _item("나"), _item("다"), _item("라"),
             states=[D.WAITING, D.WAITING, D.FINISHED, D.FAILED])
        assert _chip_counts(window) == {
            "all": 4, "waiting": 2, "running": 0, "paused": 0, "finished": 1, "failed": 1
        }

    def test_counts_follow_a_state_change(self, window) -> None:
        """대기 카드 하나가 받는 중으로 바뀌고 갱신 알림이 오면 대기 칩은 줄고 진행 칩은 늘어야 한다."""
        (item,) = _add(window, _item("가"))
        _set_state(window, item, DownloadState.RUNNING)
        counts = _chip_counts(window)
        assert (counts["waiting"], counts["running"]) == (0, 1)

    @pytest.mark.parametrize("signal", ["itemStarted", "itemStopped", "itemPaused", "itemResumed", "itemFinished"])
    def test_counts_follow_a_download_notice(self, window, signal) -> None:
        """상태를 바꾼 뒤 다운로드 알림(itemStarted 등)만 오면 그 차례 끝에 칩 숫자가 따라가야 한다."""
        (item,) = _add(window, _item("가"))
        item.downloadState = DownloadState.FINISHED
        notice = getattr(window.contentManager, signal)
        notice.emit(item, True) if signal == "itemFinished" else notice.emit(item)
        QApplication.processEvents()
        assert _chip_counts(window)["finished"] == 1

    def test_counts_follow_removing_a_card(self, window) -> None:
        """완료 카드를 지우면 전체 · 완료 칩이 하나씩 줄어야 한다."""
        D = DownloadState
        _, done = _add(window, _item("가"), _item("나"), states=[D.WAITING, D.FINISHED])
        window.contentManager.removeItem(done)
        QApplication.processEvents()
        counts = _chip_counts(window)
        assert (counts["all"], counts["finished"]) == (1, 0)

    def test_the_chip_text_shows_its_count(self, window) -> None:
        """칩 글은 상태 이름 뒤에 그 칩의 숫자여야 한다."""
        _add(window, _item("가"), _item("나"))
        assert window.filterChips["waiting"].text() == "Waiting 2"

    def test_the_failed_chip_is_emphasized_only_with_failures(self, window) -> None:
        """실패 카드가 없으면 실패 칩의 강조 속성이 꺼져 있고, 생기면 켜져야 한다."""
        (item,) = _add(window, _item("가"))
        assert window.filterChips["failed"].property("alert") is False
        _set_state(window, item, DownloadState.FAILED)
        assert window.filterChips["failed"].property("alert") is True


class TestFilter:
    @pytest.fixture
    def mixed(self, window):
        D = DownloadState
        _add(window, _item("대기"), _item("완료"), _item("실패"), states=[D.WAITING, D.FINISHED, D.FAILED])
        return window

    def test_a_chip_shows_only_its_state(self, mixed) -> None:
        """완료 칩을 누르면 완료 카드만 보여야 한다."""
        mixed.filterChips["finished"].click()
        QApplication.processEvents()
        assert _visible_titles(mixed) == ["완료"]

    def test_clicking_the_chosen_chip_again_goes_back_to_all(self, mixed) -> None:
        """고른 완료 칩을 다시 누르면 전체 칩이 눌린 상태가 되고 카드가 다 보여야 한다."""
        mixed.filterChips["finished"].click()
        mixed.filterChips["finished"].click()
        QApplication.processEvents()
        assert mixed.filterChips["all"].isChecked()
        assert _visible_titles(mixed) == ["대기", "완료", "실패"]

    def test_clicking_the_chosen_all_chip_again_keeps_it_pressed(self, mixed) -> None:
        """전체 칩이 고른 상태에서 전체 칩을 다시 누르면 전체 칩만 눌린 채여야 한다(#503 리뷰).

        처음 상태(전체) -> 전체 칩 click()
        -> 전체 칩 눌림, 나머지 칩 안 눌림, 카드 셋 다 보임
        """
        assert mixed.filterChips["all"].isChecked(), "전제: 처음엔 전체"
        mixed.filterChips["all"].click()
        QApplication.processEvents()
        assert [k for k, chip in mixed.filterChips.items() if chip.isChecked()] == ["all"]
        assert _visible_titles(mixed) == ["대기", "완료", "실패"]

    def test_a_card_leaving_the_chosen_state_is_hidden(self, mixed) -> None:
        """실패 칩을 고른 채 실패 카드가 대기로 바뀌면 그 카드가 숨어야 한다."""
        mixed.filterChips["failed"].click()
        failed = next(i for i in _screen_order(mixed) if i.title == "실패")
        _set_state(mixed, failed, DownloadState.WAITING)
        assert _visible_titles(mixed) == []


ADDED = ["다", "가", "나"]  # 추가된 순서


@pytest.fixture
def sortable(window):
    """제목 · 채널 · 날짜 · 상태가 서로 다른 순서인 카드 셋(추가된 순서 다 · 가 · 나)."""
    D = DownloadState
    _add(
        window,
        _item("다", channel="Bchan", date="2026-01-02 10:00:00"),
        _item("가", channel="Cchan", date="2026-03-01 10:00:00"),
        _item("나", channel="Achan", date="2026-02-01 10:00:00"),
        states=[D.FINISHED, D.WAITING, D.RUNNING],
    )
    return window


EXPECTED = {
    "added": ["다", "가", "나"],
    "title": ["가", "나", "다"],
    "channel": ["나", "다", "가"],  # Achan · Bchan · Cchan
    "upload": ["가", "나", "다"],  # 오름차순 = 최신 먼저(오너 2026-11-06)
    "state": ["나", "가", "다"],  # 받는 중 · 대기 · 완료
}


class TestSort:
    @pytest.mark.parametrize("by", list(EXPECTED))
    @pytest.mark.parametrize("direction", ["asc", "desc"])
    def test_screen_order(self, sortable, by, direction) -> None:
        """정렬 다섯 × 오름 · 내림에서 화면 순서가 그 기준의 차례(내림은 뒤집힌 차례)여야 한다."""
        _pick_sort(sortable, by, direction)
        expected = EXPECTED[by] if direction == "asc" else EXPECTED[by][::-1]
        assert [i.title for i in _screen_order(sortable)] == expected

    def test_sorting_keeps_the_list_in_added_order(self, sortable) -> None:
        """제목 내림차순으로 정렬해도 목록(모델)의 차례는 추가된 순서 그대로여야 한다."""
        _pick_sort(sortable, "title", "desc")
        model = sortable.contentManager.model
        assert [model.itemAt(n).title for n in range(model.rowCount())] == ADDED

    def test_only_the_chosen_rows_are_checked(self, sortable) -> None:
        """제목순 · 내림차순을 고르면 정렬 메뉴의 ✓는 그 두 줄에만 있어야 한다."""
        _pick_sort(sortable, "title", "desc")
        menu = _sort_menu(sortable)
        assert sorted(map(tuple, menu.markedKeys())) == [("by", "title"), ("direction", "desc")]
        menu.hide()

    def test_unknown_upload_dates_go_last_in_both_directions(self, window) -> None:
        """날짜를 모르는 카드는 업로드순 오름 · 내림 모두 맨 끝이어야 한다."""
        _add(window, _item("모름", date="Unknown Date"), _item("앞", date="2026-01-01 00:00:00"),
             _item("뒤", date="2026-02-01 00:00:00"))
        _pick_sort(window, "upload", "asc")
        assert [i.title for i in _screen_order(window)] == ["뒤", "앞", "모름"]
        _pick_sort(window, "upload", "desc")
        assert [i.title for i in _screen_order(window)] == ["앞", "뒤", "모름"]

    def test_a_new_card_lands_in_its_sorted_place(self, sortable) -> None:
        """제목순일 때 새 카드는 맨 끝이 아니라 제목 차례의 자리에 놓여야 한다."""
        _pick_sort(sortable, "title", "asc")
        _add(sortable, _item("까"))
        assert [i.title for i in _screen_order(sortable)] == ["가", "까", "나", "다"]

    def test_descending_added_order_puts_a_new_card_on_top(self, sortable) -> None:
        """추가된 순서 내림차순이면 새 카드가 맨 위에 놓여야 한다."""
        _pick_sort(sortable, "added", "desc")
        _add(sortable, _item("새"))
        assert _screen_order(sortable)[0].title == "새"

    def test_the_chosen_sort_is_remembered(self, sortable, qapp) -> None:
        """고른 정렬은 sortOrder에 저장되고 새 창도 그 정렬로 시작해야 한다."""
        _pick_sort(sortable, "channel", "desc")
        assert settings.load_settings()["sortOrder"] == "channel:desc"
        again = VodDownloader()
        try:
            order = again.contentManager.order
            assert (order.sortBy(), order.isDescending()) == ("channel", True)
            assert again.sortButton.fullText() == "Channel"
        finally:
            again.deleteLater()
            QApplication.processEvents()


class TestDownloadOrder:
    def _next(self, win):
        found, item, _row = win.contentManager.findItem()
        return item if found else None

    def test_the_first_waiting_card_on_screen_is_next(self, window) -> None:
        """제목 내림차순이면 다음 다운로드는 목록 첫 대기 카드가 아니라 화면 맨 위 대기 카드여야 한다."""
        _add(window, _item("가"), _item("나"), _item("다"))
        _pick_sort(window, "title", "desc")
        assert self._next(window).title == "다"

    def test_a_card_hidden_by_the_filter_still_takes_its_turn(self, window) -> None:
        """완료 칩을 골라 대기 카드가 모두 숨어도 다음 다운로드는 화면 순서의 첫 대기 카드여야 한다."""
        D = DownloadState
        _add(window, _item("가"), _item("나"), states=[D.FINISHED, D.WAITING])
        window.filterChips["finished"].click()
        QApplication.processEvents()
        assert self._next(window).title == "나"

    def test_changing_the_sort_while_downloading_is_seen_at_the_next_pick(self, window) -> None:
        """한 카드를 받는 중에 정렬을 바꾸면 그다음 고르는 카드는 바뀐 정렬의 첫 대기 카드여야 한다."""
        D = DownloadState
        running, *_ = _add(window, _item("가"), _item("나"), _item("다"), states=[D.RUNNING, D.WAITING, D.WAITING])
        _pick_sort(window, "title", "desc")  # 받는 중에 바꾼다 — 추가된 순서라면 다음은 「나」
        _set_state(window, running, D.FINISHED)
        assert self._next(window).title == "다"

    def test_state_sort_moves_a_card_when_its_state_changes(self, window) -> None:
        """상태순에서 대기 카드가 받는 중이 되면 그 카드가 맨 위로 옮겨져야 한다."""
        _add(window, _item("가"), _item("나"))
        _pick_sort(window, "state", "asc")
        second = _screen_order(window)[1]
        _set_state(window, second, DownloadState.RUNNING)
        assert _screen_order(window)[0] is second