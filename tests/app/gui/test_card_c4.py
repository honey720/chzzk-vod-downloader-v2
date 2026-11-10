"""카드 C4 — 선택 모드 · 지우기 되돌리기 · 카드 키 (#561 — 설계 §12.1 「선택 모드」 · §7 「카드 선택」).

카드는 모델에 바로 넣는다(`model.addItem` → 목록 · 순서 뷰모델 실배선). 카드 누름은 카드 면에 마우스 이벤트를
보낸다(`QTest.mouseClick` — 커서는 옮기지 않는다). 키는 초점 위젯에 키 이벤트를 보낸다 — 키 처리는 단축키가 아니라
키 이벤트라 창 활성 여부와 무관하다. 확인 창은 모듈 함수를 갈아 끼운다.
"""

from collections.abc import Iterator
from typing import Never

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import CVDv2 as main_module
import app.theme as theme
import app.viewmodels.content_viewmodel as content_viewmodel
import app.widgets.view as view_module
from app.viewmodels import card_display
from app.viewmodels.data import ContentItem
from app.views.mainWindow import VodDownloader
from app.widgets.widget import ContentItemWidget
from core.models.download_state import DownloadState
from tests.app.gui.card_helpers import drop_new_top_levels, hold_style, snapshot_top_levels

D = DownloadState
CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
FACE = QPoint(4, 4)  # 카드 면의 바탕 자리(버튼 · 글 밖) — 누름이 카드로 간다


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp) -> Iterator[None]:
    """실제 전역 QSS · 다크 테마로 잰다."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))
    yield


@pytest.fixture(autouse=True)
def _drop_windows(qapp) -> Iterator[None]:
    before = snapshot_top_levels()
    yield
    drop_new_top_levels(before)


@pytest.fixture(autouse=True)
def no_network(monkeypatch) -> None:
    class _FailingSession:
        def head(self, *a, **k) -> Never:
            raise RuntimeError("network disabled in tests")

        def get(self, *a, **k) -> Never:
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())


@pytest.fixture(autouse=True)
def animations_off() -> Iterator[None]:
    """선택 줄이 투명도 애니메이션 없이 바로 나타나고 사라진다(기본 설정과 무관하게 잰다)."""
    card_display.use(card_display.CardDisplay(animations=False))
    yield
    card_display.use(None)


@pytest.fixture
def window(qapp, tmp_path) -> Iterator[VodDownloader]:
    win = VodDownloader()
    assert win.saveLocations.choose(str(tmp_path))
    win.resize(900, 700)
    QApplication.processEvents()
    yield win
    win.hide()


def _item(title: str, state: DownloadState = D.WAITING) -> ContentItem:
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {"title": title, "category": "", "channelName": "채널", "createdDate": "", "duration": 60},
        [],
        None,
        "",
        "",
        "video",
        None,
    )
    item.downloadState = state
    return item


def _add(win: VodDownloader, *states: DownloadState) -> list[ContentItem]:
    items = [_item(f"카드 {n}", state) for n, state in enumerate(states)]
    for item in items:
        win.contentManager.model.addItem(item)
    QApplication.processEvents()
    return items


def _card(win: VodDownloader, item: ContentItem) -> ContentItemWidget:
    widget = win.listView.widgetFor(item)
    assert widget is not None
    return widget


def _click(win: VodDownloader, item: ContentItem, modifiers=Qt.KeyboardModifier.NoModifier) -> None:
    QTest.mouseClick(_card(win, item).contentFrame, Qt.MouseButton.LeftButton, modifiers, FACE)
    QApplication.processEvents()


def _picked(win: VodDownloader) -> list[ContentItem]:
    return win.listView.selectedItems()


def _drawn_picked(win: VodDownloader, items: list[ContentItem]) -> list[ContentItem]:
    """고른 카드 모양(강조색 테두리)을 그리는 카드."""
    return [item for item in items if _card(win, item).contentFrame.isSelected()]


def _screen_order(win: VodDownloader) -> list[ContentItem]:
    layout = win.listView._layout
    widgets = [layout.itemAt(n).widget() for n in range(layout.count())]
    return [w.item for w in widgets if isinstance(w, ContentItemWidget)]


def _settle() -> None:
    for _ in range(3):
        QApplication.processEvents()


def _key(widget, key: Qt.Key, modifiers=Qt.KeyboardModifier.NoModifier) -> None:
    QTest.keyClick(widget, key, modifiers)
    _settle()


# ---- 들어가기 · 나가기 ----


class TestEnterAndExit:
    def test_select_button_enters_and_pressing_it_again_exits(self, window) -> None:
        """선택 버튼을 누르면 선택 모드 · 선택 줄이 보이고, 다시 누르면 나와 선택 줄이 숨어야 한다.

        카드 둘, 선택 버튼 클릭 -> 선택 모드 참 · 선택 줄 보임, 다시 클릭 -> 거짓 · 숨음
        """
        _add(window, D.WAITING, D.WAITING)
        window.selectButton.click()
        _settle()
        assert window.listView.isSelecting() and window.selectBar.isVisible()
        window.selectButton.click()
        _settle()
        assert not window.listView.isSelecting() and not window.selectBar.isVisible()

    def test_ctrl_click_enters_and_picks_that_card(self, window) -> None:
        """평소 Ctrl+클릭하면 선택 모드로 들어가며 그 카드만 골라야 한다.

        카드 셋, 둘째 Ctrl+클릭 -> 선택 모드 · 고른 카드 [둘째]
        """
        items = _add(window, D.WAITING, D.WAITING, D.WAITING)
        _click(window, items[1], CTRL)
        assert window.listView.isSelecting()
        assert _picked(window) == [items[1]]

    def test_shift_click_enters_and_picks_that_card(self, window) -> None:
        """평소 Shift+클릭하면 선택 모드로 들어가며 그 카드만 골라야 한다(기준 카드가 아직 없다).

        카드 셋, 셋째 Shift+클릭 -> 선택 모드 · 고른 카드 [셋째]
        """
        items = _add(window, D.WAITING, D.WAITING, D.WAITING)
        _click(window, items[2], SHIFT)
        assert window.listView.isSelecting()
        assert _picked(window) == [items[2]]

    def test_ctrl_a_on_the_list_enters_and_picks_every_visible_card(self, window) -> None:
        """목록에서 Ctrl/⌘+A를 누르면 선택 모드로 들어가며 보이는 카드를 모두 골라야 한다.

        카드 셋, 목록에 Ctrl+A -> 선택 모드 · 고른 카드 셋(보이는 차례)
        """
        items = _add(window, D.WAITING, D.FINISHED, D.FAILED)
        _key(window.listView, Qt.Key.Key_A, CTRL)
        assert window.listView.isSelecting()
        assert _picked(window) == items

    def test_plain_click_outside_select_mode_picks_nothing(self, window) -> None:
        """선택 모드 밖에서 그냥 클릭하면 선택 모드로 들어가지 않아야 한다.

        카드 하나 클릭 -> 선택 모드 거짓 · 고른 카드 없음
        """
        items = _add(window, D.WAITING)
        _click(window, items[0])
        assert not window.listView.isSelecting()
        assert _picked(window) == []

    def test_cancel_button_exits(self, window) -> None:
        """선택 줄의 취소를 누르면 선택 모드에서 나오고 고른 카드가 풀려야 한다.

        카드 둘 모두 고름 -> 취소 클릭 -> 선택 모드 거짓 · 고른 카드 없음 · 고른 모양 없음
        """
        items = _add(window, D.WAITING, D.WAITING)
        _key(window.listView, Qt.Key.Key_A, CTRL)
        window.selectBar.cancelButton.click()
        _settle()
        assert not window.listView.isSelecting()
        assert _picked(window) == [] and _drawn_picked(window, items) == []

    def test_escape_exits(self, window) -> None:
        """선택 모드에서 Esc를 누르면 나와야 한다.

        카드 둘 모두 고름 -> 목록에 Esc -> 선택 모드 거짓
        """
        _add(window, D.WAITING, D.WAITING)
        _key(window.listView, Qt.Key.Key_A, CTRL)
        _key(window.listView, Qt.Key.Key_Escape)
        assert not window.listView.isSelecting()


# ---- 고르기 ----


class TestPicking:
    def test_click_toggles_one_card(self, window) -> None:
        """선택 모드에서 클릭은 그 카드 하나를 고르고, 다시 클릭하면 풀어야 한다.

        카드 셋, 선택 모드, 첫째 클릭 -> [첫째], 셋째 클릭 -> [첫째, 셋째], 첫째 다시 클릭 -> [셋째]
        """
        items = _add(window, D.WAITING, D.WAITING, D.WAITING)
        window.selectButton.click()
        _click(window, items[0])
        assert _picked(window) == [items[0]]
        _click(window, items[2])
        assert _picked(window) == [items[0], items[2]]
        _click(window, items[0])
        assert _picked(window) == [items[2]]
        assert _drawn_picked(window, items) == [items[2]]

    def test_ctrl_click_adds_and_removes(self, window) -> None:
        """선택 모드에서 Ctrl+클릭은 그 카드를 더하고 빼야 한다.

        카드 셋, 첫째 Ctrl+클릭(들어감) · 둘째 Ctrl+클릭 -> [첫째, 둘째], 첫째 Ctrl+클릭 -> [둘째]
        """
        items = _add(window, D.WAITING, D.WAITING, D.WAITING)
        _click(window, items[0], CTRL)
        _click(window, items[1], CTRL)
        assert _picked(window) == [items[0], items[1]]
        _click(window, items[0], CTRL)
        assert _picked(window) == [items[1]]

    def test_shift_click_adds_the_range_from_the_anchor_in_shown_order(self, window) -> None:
        """Shift+클릭은 기준 카드부터 그 카드까지를 보이는 차례로 더해야 한다.

        카드 다섯, 둘째 클릭(기준) · 다섯째 Shift+클릭 -> [둘째 ~ 다섯째]
        """
        items = _add(window, *[D.WAITING] * 5)
        window.selectButton.click()
        _click(window, items[1])
        _click(window, items[4], SHIFT)
        assert _picked(window) == items[1:5]

    def test_shift_range_skips_cards_hidden_by_the_filter(self, window) -> None:
        """Shift 범위는 필터로 가린 카드를 넣지 않아야 한다.

        대기 · 완료 · 대기 · 완료 · 대기, 대기 칩 -> 첫째 클릭 · 다섯째 Shift+클릭 -> [첫째, 셋째, 다섯째]
        """
        items = _add(window, D.WAITING, D.FINISHED, D.WAITING, D.FINISHED, D.WAITING)
        window.filterChips["waiting"].click()
        _settle()
        window.selectButton.click()
        _click(window, items[0])
        _click(window, items[4], SHIFT)
        assert _picked(window) == [items[0], items[2], items[4]]


class TestCheckBox:
    def test_three_states_follow_the_visible_cards(self, window) -> None:
        """체크박스는 보이는 카드 기준 하나도 0 · 일부 1 · 모두 2여야 한다.

        카드 셋, 선택 모드 -> 0, 하나 고름 -> 1, 셋 고름 -> 2
        """
        items = _add(window, D.WAITING, D.WAITING, D.WAITING)
        window.selectButton.click()
        _settle()
        assert window.selectBar.checkBox.state() == 0
        _click(window, items[0])
        _settle()
        assert window.selectBar.checkBox.state() == 1
        _click(window, items[1])
        _click(window, items[2])
        _settle()
        assert window.selectBar.checkBox.state() == 2

    def test_clicking_it_with_some_picked_picks_all_then_clears(self, window) -> None:
        """일부를 골랐을 때 체크박스를 누르면 모두 고르고, 다시 누르면 모두 풀어야 한다.

        카드 셋 · 하나 고름 -> 클릭 -> 셋 · 상태 2, 클릭 -> 없음 · 상태 0
        """
        items = _add(window, D.WAITING, D.WAITING, D.WAITING)
        _click(window, items[0], CTRL)
        window.selectBar.checkBox.click()
        _settle()
        assert _picked(window) == items and window.selectBar.checkBox.state() == 2
        window.selectBar.checkBox.click()
        _settle()
        assert _picked(window) == [] and window.selectBar.checkBox.state() == 0


class TestFilterKeepsPicks:
    def test_changing_the_filter_keeps_the_picked_cards(self, window) -> None:
        """필터를 바꿔도 고른 카드가 그대로여야 한다.

        대기 · 완료 둘 다 고름 -> 대기 칩 -> 고른 카드 [대기, 완료] · 가린 고른 카드 1 -> 전체 칩 -> 그대로
        """
        items = _add(window, D.WAITING, D.FINISHED)
        _key(window.listView, Qt.Key.Key_A, CTRL)
        window.filterChips["waiting"].click()
        _settle()
        assert _picked(window) == items
        assert window.listView.hiddenSelectedCount() == 1
        window.filterChips["all"].click()
        _settle()
        assert _picked(window) == items and _drawn_picked(window, items) == items


# ---- 지우기 ----


class TestDeletePicked:
    def test_all_visible_deletes_without_asking(self, window, monkeypatch) -> None:
        """고른 카드가 모두 보이면 묻지 않고 지우고 선택 모드에서 나와야 한다.

        카드 셋 중 둘 고름 -> 지우기 -> 숨은 카드 확인 0번 · 남은 카드 [셋째] · 선택 모드 거짓
        """
        asked: list = []
        monkeypatch.setattr(view_module, "confirm_hidden_delete", lambda *a: asked.append(a))
        items = _add(window, D.WAITING, D.FINISHED, D.WAITING)
        _click(window, items[0], CTRL)
        _click(window, items[1], CTRL)
        window.selectBar.deleteButton.click()
        _settle()
        assert asked == []
        assert window.contentManager.model.items == [items[2]]
        assert not window.listView.isSelecting()

    @pytest.mark.parametrize(("answer", "left"), [(True, 1), (False, 3)])
    def test_hidden_picks_ask_first(self, window, monkeypatch, answer: bool, left: int) -> None:
        """고른 카드 가운데 필터로 가린 것이 있으면 수를 알리는 확인을 묻고, 고르면 지우고 · 취소면 그대로여야 한다.

        대기 · 완료 · 완료, 대기 · 완료 둘째 고름 · 대기 칩 -> 지우기 -> 확인 (전체 2, 숨음 1) 한 번,
        고름 -> 남은 카드 1장, 취소 -> 3장 · 선택 모드 그대로
        """
        asked: list = []
        monkeypatch.setattr(
            view_module,
            "confirm_hidden_delete",
            lambda parent, total, hidden: asked.append((total, hidden)) or answer,
        )
        items = _add(window, D.WAITING, D.FINISHED, D.FINISHED)
        _click(window, items[0], CTRL)
        _click(window, items[1], CTRL)
        window.filterChips["waiting"].click()
        _settle()
        window.selectBar.deleteButton.click()
        _settle()
        assert asked == [(2, 1)]
        assert window.contentManager.model.rowCount() == left
        assert window.listView.isSelecting() is (not answer)

    def test_delete_key_in_select_mode_deletes_the_picked_cards(self, window) -> None:
        """선택 모드에서 목록에 Delete를 누르면 고른 카드를 지워야 한다.

        카드 셋 중 첫째 고름 -> Delete -> 남은 카드 [둘째, 셋째]
        """
        items = _add(window, D.WAITING, D.WAITING, D.WAITING)
        _click(window, items[0], CTRL)
        _key(window.listView, Qt.Key.Key_Delete)
        assert window.contentManager.model.items == items[1:]

    def test_delete_key_on_the_select_bar_deletes_the_picked_cards(self, window) -> None:
        """초점이 선택 줄 버튼에 있을 때 Delete를 누르면 고른 카드를 지워야 한다.

        카드 둘 중 첫째 고름 -> 선택 줄 체크박스에 Delete -> 남은 카드 [둘째]
        """
        items = _add(window, D.WAITING, D.WAITING)
        _click(window, items[0], CTRL)
        _key(window.selectBar.checkBox, Qt.Key.Key_Delete)
        assert window.contentManager.model.items == [items[1]]

    @pytest.mark.parametrize("busy", [D.RUNNING, D.PAUSED])
    @pytest.mark.parametrize(("answer", "left"), [(True, 1), (False, 3)])
    def test_busy_picks_ask_to_stop_first(
        self, window, monkeypatch, busy: DownloadState, answer: bool, left: int
    ) -> None:
        """고른 카드에 받는 중 · 일시정지가 섞이면 「멈추고 지우기」를 묻고, 고르면 지우고 · 취소면 그대로여야 한다.

        대기 · 받는 중(일시정지) · 대기, 앞 둘 고름 -> 지우기 -> 확인 (전체 2, 작업 중 1) 한 번,
        고름 -> 남은 1장, 취소 -> 3장
        """
        asked: list = []
        monkeypatch.setattr(
            view_module,
            "confirm_stop_and_delete",
            lambda parent, total=1, working=1: asked.append((total, working)) or answer,
        )
        items = _add(window, D.WAITING, busy, D.WAITING)
        _click(window, items[0], CTRL)
        _click(window, items[1], CTRL)
        window.selectBar.deleteButton.click()
        _settle()
        assert asked == [(2, 1)]
        assert window.contentManager.model.rowCount() == left

    def test_busy_pick_that_finished_while_asking_is_only_deleted(
        self, window, monkeypatch
    ) -> None:
        """확인 창이 열린 사이 받는 중 카드가 끝나면 엔진을 멈추지 않고 지우기만 해야 한다.

        대기 · 받는 중 고름, 확인 중 받는 중 → 완료 -> 멈추고 지우기 0번 · 지우기 1번(2장)
        """
        items = _add(window, D.WAITING, D.RUNNING)
        stopped: list = []
        removed: list = []
        window.listView.stopAndRemoveRequested.connect(stopped.append)
        window.listView.removeRequested.connect(removed.append)

        def finish_while_asking(parent, total=1, working=1) -> bool:
            items[1].downloadState = D.FINISHED
            return True

        monkeypatch.setattr(view_module, "confirm_stop_and_delete", finish_while_asking)
        _key(window.listView, Qt.Key.Key_A, CTRL)
        window.selectBar.deleteButton.click()
        _settle()
        assert (stopped, removed) == ([], [items])

    def test_main_window_stops_the_engine_only_when_it_holds_a_deleted_card(self, qapp) -> None:
        """멈추고 지우기에서 엔진이 지울 카드를 쥐고 있을 때만 다운로드를 멈추고, 카드는 늘 지워야 한다.

        엔진이 받는 중 · 엔진의 카드 = 첫째, 둘째만 지움 -> 멈춤 0번, 첫째 지움 -> 멈춤 1번 · 지움 두 번
        """
        from types import SimpleNamespace

        first, second = _item("첫째", D.RUNNING), _item("둘째", D.RUNNING)
        calls: dict[str, list] = {"stop": [], "remove": []}
        window = SimpleNamespace(
            downloadViewModel=SimpleNamespace(isDownloading=lambda: True, item=first),
            stopDownload=lambda: calls["stop"].append(1),
            contentManager=SimpleNamespace(removeItems=calls["remove"].append),
        )
        VodDownloader.onStopAndRemove(window, [second])
        assert calls["stop"] == []
        VodDownloader.onStopAndRemove(window, [first])
        assert (calls["stop"], calls["remove"]) == ([1], [[second], [first]])


# ---- 선택 모드 안의 카드 ----


class TestCardInSelectMode:
    def test_card_buttons_stay_hidden_while_hovered(self, window) -> None:
        """선택 모드에서는 호버 · 초점이어도 카드 버튼이 숨고, 나오면 호버 때 다시 보여야 한다.

        대기 카드 호버 -> 버튼 상자 보임, 선택 모드 -> 숨음, 나옴 -> 보임
        """
        items = _add(window, D.WAITING)
        card = _card(window, items[0])
        card.setHovered(True)
        assert card.hoverBox.isVisible()
        window.selectButton.click()
        _settle()
        card.setHovered(True)
        assert not card.hoverBox.isVisible() and not card.hoverShade.isVisible()
        window.selectButton.click()
        _settle()
        card.setHovered(False)
        card.setHovered(True)
        assert card.hoverBox.isVisible()

    @pytest.mark.parametrize("state", [D.WAITING, D.FINISHED])
    def test_double_click_does_not_open_anything(self, window, monkeypatch, state) -> None:
        """선택 모드에서 대기 · 완료 카드를 더블 클릭하면 편집 창도 폴더 열기도 일어나지 않아야 한다.

        선택 모드, 카드 더블 클릭 -> 편집 요청 0번 · 폴더 열기 0번
        """
        items = _add(window, state)
        card = _card(window, items[0])
        edits: list = []
        opened: list = []
        window.listView.editRequested.connect(edits.append)
        monkeypatch.setattr(card, "requestOpenDir", lambda: opened.append(1))
        window.selectButton.click()
        _settle()
        QTest.mouseDClick(
            card.contentFrame, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, FACE
        )
        _settle()
        assert (edits, opened) == ([], [])

    def test_double_click_outside_select_mode_still_opens_the_edit_dialog(self, window) -> None:
        """선택 모드 밖에서 대기 카드를 더블 클릭하면 편집 요청이 한 번 가야 한다(C2 그대로).

        대기 카드 더블 클릭 -> 편집 요청 [그 카드]
        """
        items = _add(window, D.WAITING)
        edits: list = []
        window.listView.editRequested.disconnect()
        window.listView.editRequested.connect(edits.append)
        card = _card(window, items[0])
        QTest.mouseDClick(
            card.contentFrame, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, FACE
        )
        _settle()
        assert edits == [items[0]]


# ---- 되살리기 ----


def _undo(win: VodDownloader) -> None:
    _key(win.listView, Qt.Key.Key_Z, CTRL)


class TestUndo:
    def test_deleting_and_restoring_many_cards_sends_one_count_notice_each(self, window) -> None:
        """여러 장을 한 번에 지우거나 되살리면 칩 숫자 알림은 한 번씩이고 숫자는 맞아야 한다.

        대기 셋 · 완료 둘 모두 고름 -> 지움 -> 알림 1번 · 전체 0, Ctrl+Z -> 알림 1번 · 전체 5 · 완료 2
        """
        _add(window, D.WAITING, D.WAITING, D.WAITING, D.FINISHED, D.FINISHED)
        order = window.contentManager.order
        notices: list = []
        order.countsChanged.connect(lambda: notices.append(1))
        _key(window.listView, Qt.Key.Key_A, CTRL)
        window.selectBar.deleteButton.click()
        _settle()
        assert (len(notices), order.counts()["all"]) == (1, 0)
        notices.clear()
        _undo(window)
        assert (len(notices), order.counts()["all"], order.counts()["finished"]) == (1, 5, 2)

    def test_ctrl_z_restores_deleted_cards_to_their_places(self, window) -> None:
        """여러 장을 한 번에 지운 뒤 Ctrl/⌘+Z 한 번이면 모두 지운 자리로 돌아와야 한다.

        카드 다섯 중 1 · 3 고름 -> 지움 -> Ctrl+Z -> 목록 · 화면 차례 · 순번이 지우기 전과 같음
        """
        items = _add(window, *[D.WAITING] * 5)
        _click(window, items[1], CTRL)
        _click(window, items[3], CTRL)
        window.selectBar.deleteButton.click()
        _settle()
        assert window.contentManager.model.items == [items[0], items[2], items[4]]
        _undo(window)
        assert window.contentManager.model.items == items
        assert _screen_order(window) == items
        assert [_card(window, item).index for item in items] == [0, 1, 2, 3, 4]

    def test_single_delete_is_restored_too(self, window) -> None:
        """카드 ✕ 한 장 지우기도 Ctrl/⌘+Z로 그 자리에 돌아와야 한다.

        카드 셋, 가운데 ✕ -> Ctrl+Z -> 목록 [첫째, 둘째, 셋째]
        """
        items = _add(window, D.WAITING, D.WAITING, D.WAITING)
        _card(window, items[1]).deleteButton.click()
        _settle()
        assert window.contentManager.model.items == [items[0], items[2]]
        _undo(window)
        assert window.contentManager.model.items == items

    def test_restored_card_reuses_its_widget(self, window) -> None:
        """되살린 카드는 지우기 전과 같은 위젯이어야 한다(새로 만들지 않는다).

        카드 하나 ✕ -> Ctrl+Z -> 위젯 객체 같음 · 보임
        """
        items = _add(window, D.WAITING)
        before = _card(window, items[0])
        before.deleteButton.click()
        _settle()
        _undo(window)
        assert _card(window, items[0]) is before and before.isVisible()

    @pytest.mark.parametrize(
        ("state", "restored"),
        [
            (D.WAITING, D.WAITING),
            (D.FINISHED, D.FINISHED),
            (D.FAILED, D.FAILED),
            (D.RUNNING, D.WAITING),
            (D.PAUSED, D.WAITING),
        ],
    )
    def test_restored_state_rule(self, window, monkeypatch, state, restored) -> None:
        """되살린 카드는 대기 · 완료 · 실패면 그대로, 받는 중 · 일시정지를 멈추고 지웠으면 대기여야 한다.

        카드 하나(진행 37 · 속도 글 있음) 고름 -> 지움(받는 중이면 멈추고 지우기) -> Ctrl+Z -> 상태 · 대기면 진행 0
        """
        monkeypatch.setattr(view_module, "confirm_stop_and_delete", lambda *a, **k: True)
        items = _add(window, state)
        item = items[0]
        item.download_progress = 37
        item.download_speed = "18.4 MB/s"
        _click(window, item, CTRL)
        window.selectBar.deleteButton.click()
        _settle()
        _undo(window)
        assert item.downloadState == restored
        if state in (D.RUNNING, D.PAUSED):
            assert (item.download_progress, item.download_speed) == (0, "")
        assert _card(window, item).statusLabel.property("state") == _card(window, item)._cardState()

    def test_only_the_last_fifty_deletes_can_be_restored(self, window) -> None:
        """지우기를 51번 하면 Ctrl/⌘+Z로 되살리는 것은 마지막 50번이고 첫 카드의 위젯은 놓여야 한다.

        카드 51장을 한 장씩 ✕ -> Ctrl+Z 51번 -> 목록 50장(첫 카드 빠짐), 첫 카드는 되살릴 수 없음
        """
        assert content_viewmodel.UNDO_LIMIT == 50
        items = _add(window, *[D.WAITING] * 51)
        for item in items:
            _card(window, item).deleteButton.click()
        _settle()
        assert window.contentManager.model.rowCount() == 0
        for _ in range(51):
            _undo(window)
        assert window.contentManager.model.items == items[1:]
        assert not window.contentManager.model.isKept(items[0])
        assert items[0] not in window.listView._parked

    def test_a_new_delete_after_undo_releases_the_undone_one_only_if_gone(self, window) -> None:
        """되살린 뒤 새로 지우면 되살린 카드는 그대로 남고, 새로 지운 카드만 되살릴 수 있어야 한다.

        카드 둘, 첫째 ✕ · Ctrl+Z(되살림) · 둘째 ✕ · Ctrl+Z -> 목록 [첫째, 둘째], 한 번 더 Ctrl+Z -> 그대로
        """
        items = _add(window, D.WAITING, D.WAITING)
        _card(window, items[0]).deleteButton.click()
        _settle()
        _undo(window)
        _card(window, items[1]).deleteButton.click()
        _settle()
        _undo(window)
        assert window.contentManager.model.items == items
        _undo(window)
        assert window.contentManager.model.items == items

    def test_ctrl_z_in_the_url_field_does_not_restore_cards(self, window) -> None:
        """URL 칸에서 Ctrl/⌘+Z를 누르면 URL 칸의 되돌리기만 하고 카드는 되살리지 않아야 한다.

        카드 하나 ✕ -> URL 칸에 글을 쓰고 Ctrl+Z -> 목록 비어 있음
        """
        items = _add(window, D.WAITING)
        _card(window, items[0]).deleteButton.click()
        _settle()
        QTest.keyClicks(window.urlInput, "abc")
        _key(window.urlInput, Qt.Key.Key_Z, CTRL)
        assert window.contentManager.model.rowCount() == 0

    def test_a_loading_card_whose_lookup_ended_is_not_restored(self, window) -> None:
        """조회가 끝난 조회 중 카드를 지운 뒤 Ctrl/⌘+Z를 누르면 되살리지 않고 놓아야 한다.

        조회 중 카드(도는 조회 없음) · 대기 카드 둘 다 ✕ -> Ctrl+Z 두 번 -> 목록 [대기] · 조회 중 카드는 들고 있지 않음
        """
        from app.viewmodels.item_state import ItemState

        loading, waiting = _item("조회 중"), _item("대기")
        loading.downloadState = ItemState.LOADING
        for item in (loading, waiting):
            window.contentManager.model.addItem(item)
        _settle()
        for item in (loading, waiting):
            _card(window, item).deleteButton.click()
            _settle()
        _undo(window)
        _undo(window)
        assert window.contentManager.model.items == [waiting]
        assert not window.contentManager.model.isKept(loading)


# ---- 카드 키 ----


class TestCardKeys:
    def test_arrow_keys_move_focus_in_shown_order(self, window) -> None:
        """초점 카드에서 ↓ · ↑는 보이는 차례로 다음 · 앞 카드에 초점을 옮기고 끝에서는 그대로여야 한다.

        대기 · 완료 · 대기, 대기 칩(첫째 · 셋째만 보임) -> 첫째 ↓ -> 셋째, ↓ -> 셋째, ↑ -> 첫째
        """
        items = _add(window, D.WAITING, D.FINISHED, D.WAITING)
        window.filterChips["waiting"].click()
        _settle()
        first, third = _card(window, items[0]), _card(window, items[2])
        first.setFocus(Qt.FocusReason.OtherFocusReason)
        assert window.focusWidget() is first
        _key(first, Qt.Key.Key_Down)
        assert window.focusWidget() is third
        _key(third, Qt.Key.Key_Down)
        assert window.focusWidget() is third
        _key(third, Qt.Key.Key_Up)
        assert window.focusWidget() is first

    def test_arrow_keys_with_the_keypad_modifier_still_move_focus(self, window) -> None:
        """↓에 키패드 수정자가 붙어 와도(macOS의 방향키) 초점이 다음 카드로 가야 한다.

        카드 둘, 첫째에 ↓ + KeypadModifier -> 둘째, ↓ + Shift -> 그대로
        """
        items = _add(window, D.WAITING, D.WAITING)
        first, second = _card(window, items[0]), _card(window, items[1])
        first.setFocus(Qt.FocusReason.OtherFocusReason)
        _key(first, Qt.Key.Key_Down, Qt.KeyboardModifier.KeypadModifier)
        assert window.focusWidget() is second
        _key(second, Qt.Key.Key_Up, SHIFT)
        assert window.focusWidget() is second

    def test_delete_key_on_a_focused_card_deletes_it(self, window) -> None:
        """선택 모드 밖에서 초점 카드에 Delete를 누르면 그 카드를 지우고 초점은 다음 카드로 가야 한다.

        카드 셋, 둘째에 초점 · Delete -> 목록 [첫째, 셋째] · 초점 셋째
        """
        items = _add(window, D.WAITING, D.WAITING, D.WAITING)
        card = _card(window, items[1])
        # Tab 까닭이면 목록이 보이는 차례의 첫 카드로 옮긴다(목록 밖에서 들어온 Tab) — 그 카드에 바로 둔다
        card.setFocus(Qt.FocusReason.OtherFocusReason)
        assert window.focusWidget() is card
        _key(card, Qt.Key.Key_Delete)
        assert window.contentManager.model.items == [items[0], items[2]]
        assert window.focusWidget() is _card(window, items[2])

    @pytest.mark.parametrize(("answer", "left"), [(True, 0), (False, 1)])
    def test_delete_key_on_a_busy_card_asks_to_stop_first(
        self, window, monkeypatch, answer: bool, left: int
    ) -> None:
        """받는 중 카드에 초점을 두고 Delete를 누르면 「멈추고 지우기」를 묻고 ✕와 같이 해야 한다.

        받는 중 카드에 Delete -> 확인 한 번, 고름 -> 0장, 취소 -> 1장
        """
        asked: list = []
        monkeypatch.setattr(
            view_module, "confirm_stop_and_delete", lambda *a, **k: asked.append(1) or answer
        )
        items = _add(window, D.RUNNING)
        card = _card(window, items[0])
        card.setFocus(Qt.FocusReason.OtherFocusReason)
        _key(card, Qt.Key.Key_Delete)
        assert asked == [1]
        assert window.contentManager.model.rowCount() == left

    def test_enter_on_a_failed_card_retries_it(self, window) -> None:
        """실패 카드에서 Enter를 누르면 다시 시도(대기로 되돌림)여야 한다.

        실패 카드에 Enter -> 대기 · 사유 비움
        """
        items = _add(window, D.FAILED)
        items[0].stateMessage = "Failed to save the file"
        card = _card(window, items[0])
        _key(card, Qt.Key.Key_Return)
        assert items[0].downloadState == D.WAITING and items[0].stateMessage == ""

    def test_enter_in_select_mode_opens_nothing(self, window) -> None:
        """선택 모드에서 대기 카드에 Enter를 누르면 편집 요청이 가지 않아야 한다.

        선택 모드, 대기 카드에 Enter -> 편집 요청 0번
        """
        items = _add(window, D.WAITING)
        edits: list = []
        window.listView.editRequested.disconnect()
        window.listView.editRequested.connect(edits.append)
        window.selectButton.click()
        _settle()
        _key(_card(window, items[0]), Qt.Key.Key_Return)
        assert edits == []


# ---- 선택 줄 자리 ----


def test_pressed_select_button_still_draws_the_focus_border(window) -> None:
    """선택 모드(눌린 모양)의 선택 버튼도 키보드 초점을 받으면 위 테두리가 강조색이어야 한다.

    선택 모드 -> 선택 버튼에 Tab 초점 -> 위 테두리 가운데 화소 ≈ accent
    """
    from PySide6.QtGui import QColor

    _add(window, D.WAITING)
    window.selectButton.click()
    _settle()
    window.activateWindow()
    window.selectButton.setFocus(Qt.FocusReason.TabFocusReason)
    _settle()
    assert window.selectButton.hasFocus(), "전제: 초점을 받았다"
    image = window.selectButton.grab().toImage()
    ratio = image.devicePixelRatio()
    got = image.pixelColor(int(window.selectButton.width() / 2 * ratio), 0)
    want = QColor(theme.current_tokens()["accent"])
    assert (
        abs(got.red() - want.red())
        + abs(got.green() - want.green())
        + abs(got.blue() - want.blue())
        <= 24
    )


class TestSelectBarPlace:
    @pytest.mark.parametrize("width", ["min", 900, 1600])
    def test_right_edge_is_the_select_button_right_edge_below_the_header(
        self, window, width
    ) -> None:
        """선택 줄 오른쪽 끝은 선택 버튼 오른쪽 끝이고 위끝은 헤더바 아래끝보다 아래여야 한다.

        카드 둘 · 선택 모드, 창 폭 최소 · 900 · 1600 -> 오른쪽 끝 같음 · 위끝 > 헤더바 아래끝
        """
        _add(window, D.WAITING, D.WAITING)
        target = window.minimumWidth() if width == "min" else width
        window.resize(target, 700)
        _settle()
        assert window.width() == target
        window.selectButton.click()
        _settle()
        bar = window.selectBar
        assert bar.isVisible()
        column = window.contentColumn
        button_right = window.selectButton.mapTo(column, QPoint(window.selectButton.width(), 0)).x()
        header_bottom = window.headerFrame.mapTo(column, QPoint(0, window.headerFrame.height())).y()
        assert bar.geometry().right() + 1 == button_right
        assert bar.geometry().top() > header_bottom
        assert bar.geometry().left() >= window.headerFrame.mapTo(column, QPoint(0, 0)).x()

    @pytest.mark.parametrize("count", [1, 12])
    def test_count_text_is_never_cut(self, window, count: int) -> None:
        """「N개 선택」 글은 잘리지 않아야 한다 — 글 폭이 글 칸 안쪽 폭 이하.

        카드 12장 중 1 · 12장 고름 -> 글 폭(글꼴로 잼) ≤ 글 칸 안쪽 폭
        """
        items = _add(window, *[D.WAITING] * 12)
        for item in items[:count]:
            _click(window, item, CTRL)
        _settle()
        label = window.selectBar.countLabel
        assert label.isVisible()
        assert str(count) in label.text()
        need = QFontMetrics(label.font()).horizontalAdvance(label.text())
        assert need <= label.contentsRect().width()
