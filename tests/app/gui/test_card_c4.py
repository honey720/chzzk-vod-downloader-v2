"""카드 C4 — 선택 모드 · 지우기 되돌리기 · 카드 키 · focus ring (#561 — 설계 §12.1 「선택 모드」 · §7 「카드 선택」).

카드는 모델에 바로 넣는다(`model.addItem` → 목록 · 순서 뷰모델 실배선). 카드 누름은 카드 면에 마우스 이벤트를
보낸다(`QTest.mouseClick` — 커서는 옮기지 않는다). 누름 · 옮김 · 뗌을 나눠 재는 테스트는 창(`QWindow`)에 합성
이벤트를 보낸다 — 누른 채 카드 밖으로 나간 옮김 · 뗌도 Qt가 누른 카드로 보낸다. 키는 초점 위젯에 키 이벤트를
보낸다 — 키 처리는 단축키가 아니라 키 이벤트라 창 활성 여부와 무관하다. focus ring은 초점 이벤트를 따르므로 창이
활성이 될 때까지 조건 대기한 뒤에 초점을 준다(`_focus`). 확인 창은 모듈 함수를 갈아 끼운다.
"""

from collections.abc import Iterator
from typing import Never

import pytest
from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt
from PySide6.QtGui import QColor, QFontMetrics, QImage, QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from pytestqt.qtbot import QtBot

import CVDv2 as main_module
import app.theme as theme
import app.viewmodels.content_viewmodel as content_viewmodel
import app.widgets.view as view_module
from app.viewmodels import card_display
from app.viewmodels.data import ContentItem
from app.views.mainWindow import VodDownloader
from app.widgets.focus_ring import FocusRing
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
    """카드 면을 누르고 뗀다 — 굴러 나가 안 보이는 카드는 먼저 보이게 굴린다(보이는 카드만 누를 수 있다)."""
    card = _card(win, item)
    win.listView.ensureWidgetVisible(card, 0, 0)
    QTest.mouseClick(card.contentFrame, Qt.MouseButton.LeftButton, modifiers, FACE)
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


def _focus(qtbot: QtBot, win: VodDownloader, widget: QWidget, reason: Qt.FocusReason) -> None:
    """창이 활성이 된 뒤에 그 위젯에 초점을 준다 — 초점 이벤트가 요청한 `reason` 그대로 온다.

    활성이 아닌 창에서 `setFocus()`를 부르면 초점 이벤트가 바로 오지 않고, 창이 활성이 될 때
    `ActiveWindowFocusReason`으로 온다 — 마우스 초점을 준 카드가 키보드 초점을 받는다. ubuntu CI(xvfb)에서는
    활성 요청 한 번으로 5초 안에 초점이 오지 않은 적이 있어, 활성이 될 때까지 다시 요청한다.
    """

    def focused() -> bool:
        if not win.isActiveWindow():
            win.activateWindow()
            return False
        widget.setFocus(reason)
        return widget.hasFocus()

    qtbot.waitUntil(focused)


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

    def test_space_in_select_mode_picks_and_unpicks_the_focused_card(self, window) -> None:
        """선택 모드에서 초점 카드에 Space를 누르고 떼면 그 카드를 고르고, 다시 누르고 떼면 풀어야 한다.

        선택 모드, 둘째 카드에 초점 · Space(누름 · 뗌) -> [둘째] · 초점 그대로, Space(누름 · 뗌) -> []
        누름 · 뗌을 나눠 재는 테스트는 `TestSpaceOnRelease`
        """
        items = _add(window, D.WAITING, D.WAITING)
        window.selectButton.click()
        _settle()
        card = _card(window, items[1])
        card.setFocus(Qt.FocusReason.OtherFocusReason)
        _key(card, Qt.Key.Key_Space)
        assert _picked(window) == [items[1]]
        assert _drawn_picked(window, items) == [items[1]]
        assert window.focusWidget() is card
        _key(card, Qt.Key.Key_Space)
        assert _picked(window) == []

    def test_arrow_then_space_picks_a_second_card(self, window) -> None:
        """Space로 한 장을 고른 뒤 ↓로 옮겨 Space를 누르면 두 장이 골라져야 한다.

        선택 모드, 카드 셋, 첫째 Space · ↓ · ↓ · Space -> [첫째, 셋째]
        """
        items = _add(window, D.WAITING, D.WAITING, D.WAITING)
        window.selectButton.click()
        _settle()
        _card(window, items[0]).setFocus(Qt.FocusReason.OtherFocusReason)
        _key(window.focusWidget(), Qt.Key.Key_Space)
        _key(window.focusWidget(), Qt.Key.Key_Down)
        _key(window.focusWidget(), Qt.Key.Key_Down)
        assert window.focusWidget() is _card(window, items[2])
        _key(window.focusWidget(), Qt.Key.Key_Space)
        assert _picked(window) == [items[0], items[2]]

    def test_shift_space_adds_the_range_from_the_anchor_without_hidden_cards(self, window) -> None:
        """Shift+Space는 기준 카드부터 초점 카드까지를 필터로 가린 카드 없이 더해야 한다.

        대기 · 완료 · 대기 · 완료 · 대기, 대기 칩, 선택 모드 -> 첫째 Space · ↓ · ↓ · Shift+Space
        -> [첫째, 셋째, 다섯째]
        """
        items = _add(window, D.WAITING, D.FINISHED, D.WAITING, D.FINISHED, D.WAITING)
        window.filterChips["waiting"].click()
        _settle()
        window.selectButton.click()
        _settle()
        _card(window, items[0]).setFocus(Qt.FocusReason.OtherFocusReason)
        _key(window.focusWidget(), Qt.Key.Key_Space)
        _key(window.focusWidget(), Qt.Key.Key_Down)
        _key(window.focusWidget(), Qt.Key.Key_Down)
        assert window.focusWidget() is _card(window, items[4])
        _key(window.focusWidget(), Qt.Key.Key_Space, SHIFT)
        assert _picked(window) == [items[0], items[2], items[4]]

    @pytest.mark.parametrize("modifiers", [Qt.KeyboardModifier.NoModifier, SHIFT, CTRL])
    def test_space_outside_select_mode_does_not_enter_it(
        self, window, modifiers: Qt.KeyboardModifier
    ) -> None:
        """선택 모드 밖에서 초점 카드에 Space를 누르면 선택 모드로 들어가지 않고 아무것도 고르지 않아야 한다.

        카드 하나에 초점, Space · Shift+Space · Ctrl+Space -> 선택 모드 아님 · 고른 카드 0
        """
        items = _add(window, D.WAITING)
        card = _card(window, items[0])
        card.setFocus(Qt.FocusReason.OtherFocusReason)
        _key(card, Qt.Key.Key_Space, modifiers)
        assert not window.listView.isSelecting()
        assert _picked(window) == []

    def test_auto_repeated_space_does_not_toggle_again(self, window) -> None:
        """Space로 고른 뒤 오는 키 반복 누름 이벤트는 고른 카드를 바꾸지 않아야 한다.

        선택 모드, 초점 카드에 Space(누름 · 뗌) -> [카드], 키 반복 누름 두 번 -> 한 번마다 [카드] 그대로.
        누른 채 오는 키 반복은 `TestSpaceOnRelease::test_auto_repeat_while_holding_does_not_pick`
        """
        items = _add(window, D.WAITING)
        window.selectButton.click()
        _settle()
        card = _card(window, items[0])
        card.setFocus(Qt.FocusReason.OtherFocusReason)
        _key(card, Qt.Key.Key_Space)
        assert _picked(window) == [items[0]]
        for _ in range(2):
            repeat = QKeyEvent(
                QEvent.Type.KeyPress, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier, " ", True
            )
            QApplication.sendEvent(card, repeat)
            _settle()
            # 한 번마다 본다 — 두 번 뒤에만 보면 풀었다가 다시 고른 것도 같은 값이다
            assert _picked(window) == [items[0]]


# ---- 선택 줄 자리 ----


def test_pressed_select_button_still_draws_the_focus_border(
    window: VodDownloader, qtbot: QtBot
) -> None:
    """선택 모드(눌린 모양)의 선택 버튼도 키보드 초점을 받으면 위 테두리가 강조색이어야 한다.

    선택 모드 -> 선택 버튼에 Tab 초점 -> 위 테두리 가운데 화소 ≈ accent
    """
    from PySide6.QtGui import QColor

    _add(window, D.WAITING)
    window.selectButton.click()
    _settle()
    _focus(qtbot, window, window.selectButton, Qt.FocusReason.TabFocusReason)
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


# ---- 체크 원 · focus ring · 뗄 때 고르기 ----

QSS_PATH = main_module.resource_path(theme.QSS_RELATIVE_PATH)
COLOUR_SLACK = 24  # 색이 같다고 보는 한도 — 세 채널 차의 합(안티에일리어싱이 섞는 몫)
RING_SLACK = 48  # 원 테두리 화소의 한도 — 원 가장자리라 바깥 바탕이 조금 더 섞인다
MARK_SIDE = {False: 20, True: 16}  # 썸네일 줄이기 끔 · 켬의 원 지름(px)
MARK_INSET = 4  # 썸네일 왼쪽 위에서 원까지(px)


def _toggle_select_mode(win: VodDownloader, on: bool) -> None:
    win.selectButton.click()
    _settle()
    assert win.listView.isSelecting() is on, "전제: 선택 모드가 바뀌었다"


def _show_compact(compact: bool) -> None:
    """뒤에 만드는 카드의 썸네일 줄이기를 정한다 — 되돌리기는 `animations_off`가 한다."""
    card_display.use(card_display.CardDisplay(animations=False, compact_thumbnail=compact))


def _focus_by_key(qtbot: QtBot, win: VodDownloader, item: ContentItem) -> ContentItemWidget:
    """그 카드에 키보드 초점을 준다 — 마우스 초점은 focus ring을 그리지 않는다."""
    card = _card(win, item)
    _focus(qtbot, win, card, Qt.FocusReason.OtherFocusReason)
    assert card.hasKeyFocus(), "전제: 키보드 초점을 받았다"
    return card


RING_GAP = 2  # focus ring을 카드 테두리에서 띄우는 px — 지시 값(제품 토큰을 읽지 않는다)
RING_WIDTH = 2  # focus ring 굵기(px) — 지시 값
RING_REACH = RING_GAP + RING_WIDTH  # focus ring의 바깥 끝이 카드 밖으로 나간 px
LEFT = Qt.MouseButton.LeftButton
NO_MODIFIER = Qt.KeyboardModifier.NoModifier


def _ring(win: VodDownloader) -> FocusRing:
    return win.listView.focusRing()


def _wrapped(card: QWidget) -> QRect:
    """그 카드를 네 변 모두 RING_REACH만큼 띄워 두른 사각형 — 카드 컨테이너 좌표."""
    return card.geometry().adjusted(-RING_REACH, -RING_REACH, RING_REACH, RING_REACH)


def _ring_wraps(win: VodDownloader, card: ContentItemWidget) -> bool:
    """focus ring이 보이고 그 카드를 네 변 모두 띄워 두르는지."""
    ring = _ring(win)
    return (
        ring.isVisible()
        and ring.parentWidget() is card.parentWidget()
        and ring.geometry() == _wrapped(card)
    )


def _outside(win: VodDownloader, card: ContentItemWidget, out: int) -> QColor:
    """카드 왼쪽 테두리에서 바깥으로 `out`번째(1부터) 화소 — 세로 가운데, 창을 그려서 읽는다."""
    assert card.isVisible(), "전제: 카드가 보인다"
    spot = card.mapTo(win, QPoint(-out, card.height() // 2))
    return _colour_at(win.grab().toImage(), spot.x(), spot.y())


def _spot(win: VodDownloader, item: ContentItem) -> QPoint:
    """그 카드 면의 바탕 자리 — 창 좌표. 굴러 나간 카드는 먼저 보이게 굴린다."""
    card = _card(win, item)
    win.listView.ensureWidgetVisible(card, 0, 0)
    _settle()
    return card.contentFrame.mapTo(win, FACE)


def _press(
    win: VodDownloader,
    item: ContentItem,
    modifiers: Qt.KeyboardModifier = NO_MODIFIER,
    button: Qt.MouseButton = LEFT,
) -> None:
    """창에 누름 이벤트만 보낸다 — 떼지 않는다."""
    QTest.mousePress(win.windowHandle(), button, modifiers, _spot(win, item))
    _settle()


def _drag_to(win: VodDownloader, item: ContentItem) -> None:
    """누른 채 그 카드 면으로 옮긴다 — 창에 보내는 합성 이벤트라 실제 커서는 그대로다."""
    QTest.mouseMove(win.windowHandle(), _spot(win, item))
    _settle()


def _release(
    win: VodDownloader,
    item: ContentItem,
    modifiers: Qt.KeyboardModifier = NO_MODIFIER,
    button: Qt.MouseButton = LEFT,
) -> None:
    """그 카드 면 위에서 뗀다."""
    QTest.mouseRelease(win.windowHandle(), button, modifiers, _spot(win, item))
    _settle()


def _colour_at(image: QImage, x: float, y: float) -> QColor:
    ratio = image.devicePixelRatio()
    return image.pixelColor(int(x * ratio), int(y * ratio))


def _gap(got: QColor, want: QColor) -> int:
    return (
        abs(got.red() - want.red())
        + abs(got.green() - want.green())
        + abs(got.blue() - want.blue())
    )


def _is(got: QColor, token: str, slack: int = COLOUR_SLACK) -> bool:
    """그 화소가 지금 테마의 `token` 색인지."""
    return _gap(got, QColor(theme.current_tokens()[token])) <= slack


def _edge(card: ContentItemWidget, depth: int) -> QColor:
    """카드 면 왼쪽 가장자리에서 `depth`번째(0부터) 화소 — 세로 가운데(모서리 둥글림 밖)."""
    face = card.contentFrame
    assert face.isVisible(), "전제: 카드 면이 보인다"
    return _colour_at(face.grab().toImage(), depth, face.height() / 2)


def _mark_state(card: ContentItemWidget) -> tuple[bool, bool]:
    """(체크 원을 그리는지, 고른 모양인지)."""
    thumbnail = card.thumbnailLabel
    return thumbnail.hasPickMark(), thumbnail.isMarkPicked()


def _thumbnail_image(card: ContentItemWidget) -> QImage:
    """썸네일을 그린 이미지 — 체크 원은 썸네일이 그리므로 썸네일을 그려서 읽는다."""
    assert card.thumbnailLabel.isVisible(), "전제: 썸네일이 보인다"
    return card.thumbnailLabel.grab().toImage()


def _mark_colour(card: ContentItemWidget, x: float, y: float) -> QColor:
    """체크 원 자리(썸네일 왼쪽 위에서 MARK_INSET 안쪽)의 왼쪽 위에서 (x, y) 떨어진 화소."""
    return _colour_at(_thumbnail_image(card), MARK_INSET + x, MARK_INSET + y)


class _PaintCounter(QObject):
    """위젯 하나에 온 그리기 이벤트를 센다 — 그 위젯에만 거는 필터다."""

    def __init__(self, watched: QWidget) -> None:
        super().__init__(watched)
        self.count = 0
        watched.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Paint:
            self.count += 1
        return False


class TestPickMark:
    def test_mark_is_drawn_only_after_entering_select_mode(self, window) -> None:
        """선택 모드 밖의 카드에는 체크 원이 없고, 들어가면 모든 카드에 빈 원이 그려져야 한다.

        카드 둘 -> 원 없음, 선택 버튼 클릭 -> 두 카드 모두 (원 그림, 빔) · 원 왼쪽 끝 화소 ≈ pickMarkRing
        """
        items = _add(window, D.WAITING, D.FINISHED)
        cards = [_card(window, item) for item in items]
        assert [_mark_state(card) for card in cards] == [(False, False)] * 2
        _toggle_select_mode(window, True)
        assert [_mark_state(card) for card in cards] == [(True, False)] * 2
        middle = MARK_SIDE[False] / 2
        for card in cards:
            assert _is(_mark_colour(card, 0, middle), "pickMarkRing", RING_SLACK)

    def test_mark_hides_on_leaving_and_shows_again_on_entering(self, window) -> None:
        """선택 모드에서 나오면 썸네일이 들어가기 전 모습으로 돌아가고, 다시 들어가면 원이 그려져야 한다.

        카드 하나, 들어가기 전 썸네일 이미지 T -> 들어감: T와 다름 -> 나옴: T와 같음 ->
        다시 들어감: T와 다름
        """
        items = _add(window, D.WAITING)
        card = _card(window, items[0])
        plain = _thumbnail_image(card)
        _toggle_select_mode(window, True)
        assert _thumbnail_image(card) != plain
        _toggle_select_mode(window, False)
        assert _mark_state(card) == (False, False)
        assert _thumbnail_image(card) == plain
        _toggle_select_mode(window, True)
        assert _thumbnail_image(card) != plain

    def test_thumbnail_on_screen_is_repainted_on_entering_and_leaving(self, window, qtbot) -> None:
        """화면에 나온 카드의 썸네일은 선택 모드를 드나들 때마다 다시 그려져야 한다.

        카드 하나(썸네일이 화면에 나와 있음), 썸네일의 그리기 이벤트를 셈 -> 선택 버튼 클릭 -> 1번 이상,
        다시 클릭 -> 더 늘어남
        """
        items = _add(window, D.WAITING)
        thumbnail = _card(window, items[0]).thumbnailLabel
        _settle()
        assert not thumbnail.visibleRegion().isEmpty(), "전제: 썸네일이 화면에 나와 있다"
        paints = _PaintCounter(thumbnail)
        _toggle_select_mode(window, True)
        qtbot.waitUntil(lambda: paints.count >= 1)
        entered = paints.count
        _toggle_select_mode(window, False)
        qtbot.waitUntil(lambda: paints.count > entered)

    def test_mark_follows_picking_and_unpicking(self, window) -> None:
        """카드를 고르면 그 카드의 원만 고른 모양이 되고, 풀면 빈 원으로 돌아가야 한다.

        선택 모드, 카드 둘, 첫째 클릭 -> 원 [고름, 빔], 첫째 다시 클릭 -> [빔, 빔]
        """
        items = _add(window, D.WAITING, D.WAITING)
        _toggle_select_mode(window, True)
        cards = [_card(window, item) for item in items]
        _click(window, items[0])
        assert _picked(window) == [items[0]], "전제: 첫째를 골랐다"
        assert [_mark_state(card) for card in cards] == [(True, True), (True, False)]
        _click(window, items[0])
        assert [_mark_state(card) for card in cards] == [(True, False), (True, False)]

    def test_entering_by_ctrl_a_fills_every_mark(self, window) -> None:
        """평소 Ctrl/⌘+A로 선택 모드에 들어가면 모든 카드의 원이 그려지고 고른 모양이어야 한다.

        카드 셋(원 없음), 목록에 Ctrl+A -> 원 셋 모두 (원 그림, 고름)
        """
        items = _add(window, D.WAITING, D.FINISHED, D.FAILED)
        _key(window.listView, Qt.Key.Key_A, CTRL)
        assert [_mark_state(_card(window, item)) for item in items] == [(True, True)] * 3

    def test_marks_are_empty_after_leaving_with_picked_cards(self, window) -> None:
        """고른 카드가 있는 채 선택 모드를 나갔다 들어오면 원이 모두 비어 있어야 한다.

        카드 둘 모두 고름 -> 나옴 -> 다시 들어감 -> 원 [빔, 빔]
        """
        items = _add(window, D.WAITING, D.WAITING)
        _key(window.listView, Qt.Key.Key_A, CTRL)
        cards = [_card(window, item) for item in items]
        assert [_mark_state(card) for card in cards] == [(True, True)] * 2, "전제: 둘 다 골랐다"
        _toggle_select_mode(window, False)
        _toggle_select_mode(window, True)
        assert [_mark_state(card) for card in cards] == [(True, False)] * 2

    def test_click_on_the_mark_picks_the_card(self, window) -> None:
        """체크 원 위를 누르면 그 카드가 골라져야 한다.

        선택 모드, 카드 둘, 둘째 카드의 원 가운데를 창에 합성 클릭 -> 고른 카드 [둘째]
        """
        items = _add(window, D.WAITING, D.WAITING)
        _toggle_select_mode(window, True)
        thumbnail = _card(window, items[1]).thumbnailLabel
        assert thumbnail.isVisible() and thumbnail.hasPickMark(), "전제: 둘째 카드에 원을 그린다"
        centre = MARK_INSET + MARK_SIDE[False] // 2
        QTest.mouseClick(
            window.windowHandle(),
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            thumbnail.mapTo(window, QPoint(centre, centre)),
        )
        _settle()
        assert _picked(window) == [items[1]]

    @pytest.mark.parametrize("compact", [False, True])
    def test_mark_sits_inside_the_thumbnail_top_left(self, window, compact: bool) -> None:
        """체크 원은 썸네일 왼쪽 위 4px 안쪽에 지름 20(썸네일 줄이기는 16)으로 그려져야 한다.

        썸네일 줄이기 끔 · 켬, 들어가기 전 썸네일 이미지 T -> 선택 모드 -> 원 자리 (4, 4, 지름, 지름) ·
        썸네일 안, 원 오른쪽 끝 화소 ≈ pickMarkRing, 원에서 오른쪽으로 1px 떨어진 화소는 T와 같음
        """
        _show_compact(compact)
        items = _add(window, D.WAITING)
        card = _card(window, items[0])
        assert card.compactThumbnail is compact, "전제: 카드가 그 썸네일 설정으로 만들어졌다"
        plain = _thumbnail_image(card)
        _toggle_select_mode(window, True)
        thumbnail = card.thumbnailLabel
        assert thumbnail.hasPickMark()
        side = MARK_SIDE[compact]
        assert thumbnail.pickMarkRect() == QRect(MARK_INSET, MARK_INSET, side, side)
        assert thumbnail.rect().contains(thumbnail.pickMarkRect())
        assert _is(_mark_colour(card, side - 1, side / 2), "pickMarkRing", RING_SLACK)
        beyond = _colour_at(plain, MARK_INSET + side + 1, MARK_INSET + side / 2)
        assert _mark_colour(card, side + 1, side / 2) == beyond

    @pytest.mark.parametrize("compact", [False, True])
    def test_mark_changes_no_card_or_window_size(self, window, compact: bool) -> None:
        """체크 원이 보여도 카드 크기 · 크기 힌트 · 창 최소폭은 그대로여야 한다.

        카드 둘, 선택 모드에 들어가 하나 고름 -> 카드 sizeHint · minimumSizeHint · 크기 · 썸네일 크기,
        창 최소폭이 들어가기 전과 같음
        """
        _show_compact(compact)
        items = _add(window, D.WAITING, D.FINISHED)
        cards = [_card(window, item) for item in items]

        def sizes() -> list:
            _settle()
            per_card = [
                (c.sizeHint(), c.minimumSizeHint(), c.size(), c.thumbnailLabel.size())
                for c in cards
            ]
            return [per_card, window.minimumWidth(), window.minimumSizeHint().width()]

        before = sizes()
        _toggle_select_mode(window, True)
        _click(window, items[0])
        assert [_mark_state(card) for card in cards] == [(True, True), (True, False)]
        assert sizes() == before

    def test_restored_card_gets_a_mark_in_select_mode(self, window) -> None:
        """선택 모드에서 되살린 카드에도 빈 체크 원이 그려져야 한다.

        카드 둘, 첫째 ✕(선택 모드 밖) -> 선택 모드 -> Ctrl+Z -> 되살린 카드 (원 그림, 빔)
        """
        items = _add(window, D.WAITING, D.WAITING)
        _card(window, items[0]).deleteButton.click()
        _settle()
        assert window.contentManager.model.items == [items[1]], "전제: 첫째를 지웠다"
        _toggle_select_mode(window, True)
        _undo(window)
        assert window.listView.isSelecting(), "전제: 되살린 뒤에도 선택 모드다"
        restored = _card(window, items[0])
        assert restored.isVisible()
        assert _mark_state(restored) == (True, False)

    def test_card_restored_outside_select_mode_hides_its_emptied_mark(self, window) -> None:
        """골라서 지운 카드를 선택 모드 밖에서 되살리면 원이 없고, 다시 들어가면 빈 원이어야 한다.

        카드 둘 모두 골라 지움(선택 모드에서 나옴) -> Ctrl+Z -> 원 없음, 선택 모드 -> (원 그림, 빔)
        """
        items = _add(window, D.WAITING, D.WAITING)
        _key(window.listView, Qt.Key.Key_A, CTRL)
        picked = [_mark_state(_card(window, item)) for item in items]
        assert picked == [(True, True)] * 2, "전제: 둘 다 골랐다"
        window.selectBar.deleteButton.click()
        _settle()
        assert not window.listView.isSelecting(), "전제: 지우면서 선택 모드에서 나왔다"
        _undo(window)
        assert [_card(window, item).isVisible() for item in items] == [True, True]
        assert [_mark_state(_card(window, item)) for item in items] == [(False, False)] * 2
        _toggle_select_mode(window, True)
        assert [_mark_state(_card(window, item)) for item in items] == [(True, False)] * 2

    def test_card_added_in_select_mode_gets_a_mark(self, window) -> None:
        """선택 모드 중에 새로 넣은 카드에도 빈 체크 원이 그려져야 한다.

        카드 하나 · 선택 모드 -> 카드 하나 더 넣음 -> 새 카드 (원 그림, 빔)
        """
        _add(window, D.WAITING)
        _toggle_select_mode(window, True)
        added = _card(window, _add(window, D.WAITING)[0])
        assert added.isVisible()
        assert _mark_state(added) == (True, False)


class TestPickMarkPixels:
    @pytest.mark.parametrize("compact", [False, True])
    def test_picked_mark_is_accent_filled_with_a_ring_and_a_check(
        self, window, compact: bool
    ) -> None:
        """고른 원은 강조색 채움 · 원 테두리 색 테두리 · 채움 안 글자 색 ✓여야 한다.

        썸네일 줄이기 끔 · 켬, 선택 모드에서 카드를 고름 -> 원의 위쪽 안(✓ 밖) ≈ accent,
        왼쪽 끝 가운데 ≈ pickMarkRing, ✓가 꺾이는 점 ≈ onAccent
        """
        _show_compact(compact)
        items = _add(window, D.WAITING)
        _toggle_select_mode(window, True)
        _click(window, items[0])
        card = _card(window, items[0])
        assert _mark_state(card) == (True, True), "전제: 고른 원이다"
        side = MARK_SIDE[compact]
        scale = side / 20  # ✓ 좌표는 지름 20 격자다
        assert _is(_mark_colour(card, side / 2, side / 4), "accent")
        assert _is(_mark_colour(card, 0, side / 2), "pickMarkRing", RING_SLACK)
        assert _is(_mark_colour(card, 8.8 * scale, 13.6 * scale), "onAccent")

    def test_empty_mark_darkens_the_thumbnail_inside_a_ring(self, window) -> None:
        """안 고른 원은 원 테두리 색 테두리 안에서 썸네일을 원 바탕 색으로 어둡게 덮어야 한다.

        카드 하나, 들어가기 전 썸네일 화소 P(원 가운데 자리) -> 선택 모드 -> 원 가운데 ≈
        pickMarkScrim을 PICK_MARK_SCRIM_ALPHA로 P에 얹은 색(P와는 다름) · 왼쪽 끝 가운데 ≈ pickMarkRing
        """
        items = _add(window, D.WAITING)
        card = _card(window, items[0])
        centre = MARK_INSET + MARK_SIDE[False] / 2
        beneath = _colour_at(_thumbnail_image(card), centre, centre)
        _toggle_select_mode(window, True)
        assert _mark_state(card) == (True, False), "전제: 빈 원이다"
        scrim = QColor(theme.current_tokens()["pickMarkScrim"])
        alpha = theme.PICK_MARK_SCRIM_ALPHA
        want = QColor(
            round(scrim.red() * alpha + beneath.red() * (1 - alpha)),
            round(scrim.green() * alpha + beneath.green() * (1 - alpha)),
            round(scrim.blue() * alpha + beneath.blue() * (1 - alpha)),
        )
        got = _mark_colour(card, MARK_SIDE[False] / 2, MARK_SIDE[False] / 2)
        assert _gap(want, beneath) > COLOUR_SLACK, "전제: 덮은 색이 밑 색과 구별된다"
        assert _gap(got, want) <= COLOUR_SLACK
        assert _is(_mark_colour(card, 0, MARK_SIDE[False] / 2), "pickMarkRing", RING_SLACK)

    def test_mark_repaints_in_the_new_theme_after_a_runtime_switch(self, qapp, window) -> None:
        """실행 중 라이트 테마로 바꾸면 고른 원이 라이트의 강조색 · ✓ 색으로 다시 그려져야 한다.

        다크 · 선택 모드에서 카드를 고름 -> apply_color_scheme("light") -> 원 위쪽 안 ≈ 라이트 accent,
        ✓가 꺾이는 점 ≈ 라이트 onAccent
        """
        items = _add(window, D.WAITING)
        _toggle_select_mode(window, True)
        _click(window, items[0])
        card = _card(window, items[0])
        assert _is(_mark_colour(card, 10, 5), "accent"), "전제: 다크의 강조색으로 그렸다"
        try:
            theme.apply_color_scheme(qapp, "light", QSS_PATH)
            _settle()
            assert _gap(_mark_colour(card, 10, 5), QColor(theme.LIGHT["accent"])) <= COLOUR_SLACK
            assert (
                _gap(_mark_colour(card, 8.8, 13.6), QColor(theme.LIGHT["onAccent"])) <= COLOUR_SLACK
            )
        finally:
            theme.apply_color_scheme(qapp, "dark", QSS_PATH)


class TestFocusRing:
    def test_ring_is_hidden_until_a_card_takes_key_focus(
        self, window: VodDownloader, qtbot: QtBot
    ) -> None:
        """focus ring은 카드가 키보드 초점을 받기 전에는 숨어 있고 받으면 나타나야 한다.

        카드 셋 -> 숨음 -> 둘째에 키보드 초점 -> 보임
        """
        items = _add(window, D.WAITING, D.WAITING, D.WAITING)
        assert not _ring(window).isVisible()
        _focus_by_key(qtbot, window, items[1])
        qtbot.waitUntil(_ring(window).isVisible)

    def test_ring_wraps_the_key_focused_card_four_pixels_out(
        self, window: VodDownloader, qtbot: QtBot
    ) -> None:
        """focus ring은 키보드 초점 카드를 네 변 모두 4px(띄움 2 + 굵기 2) 밖에서 둘러야 한다.

        카드 셋(스크롤바 없음), 둘째에 키보드 초점 -> focus ring 자리 = 카드 자리에서 네 변 4px 밖,
        붙여 그린 변 없음, 부모 = 카드의 부모(카드 컨테이너)
        """
        items = _add(window, D.WAITING, D.WAITING, D.WAITING)
        assert not window.listView.verticalScrollBar().isVisible(), "전제: 스크롤바가 없다"
        card = _focus_by_key(qtbot, window, items[1])
        qtbot.waitUntil(lambda: _ring_wraps(window, card))
        assert _ring(window).attachedSides() == ()

    @pytest.mark.parametrize(
        ("key", "modifiers", "start", "end"),
        [
            (Qt.Key.Key_Down, NO_MODIFIER, 0, 1),
            (Qt.Key.Key_Up, NO_MODIFIER, 2, 1),
            (Qt.Key.Key_Tab, NO_MODIFIER, 0, 1),
            (Qt.Key.Key_Tab, SHIFT, 2, 1),
        ],
    )
    def test_ring_follows_the_arrow_and_tab_keys(
        self,
        window: VodDownloader,
        qtbot: QtBot,
        key: Qt.Key,
        modifiers: Qt.KeyboardModifier,
        start: int,
        end: int,
    ) -> None:
        """↑↓ · Tab · Shift+Tab으로 초점을 옮기면 focus ring이 새 초점 카드를 둘러야 한다.

        카드 셋, start번째에 키보드 초점 · 키 한 번 -> end번째가 키보드 초점, focus ring이 end번째를 두름
        """
        items = _add(window, D.WAITING, D.WAITING, D.WAITING)
        first = _focus_by_key(qtbot, window, items[start])
        qtbot.waitUntil(lambda: _ring_wraps(window, first))
        _key(first, key, modifiers)
        target = _card(window, items[end])
        qtbot.waitUntil(target.hasKeyFocus)
        assert _ring_wraps(window, target)

    def test_ring_scrolls_with_its_card_and_is_clipped_by_the_viewport(
        self, window: VodDownloader, qtbot: QtBot
    ) -> None:
        """스크롤하면 focus ring이 카드와 함께 움직이고 화면 밖으로 나간 부분은 그려지지 않아야 한다.

        카드 12장, 첫째에 키보드 초점 -> 스크롤 값 = 카드 높이 절반 -> 창에서 본 focus ring 위쪽이 그만큼
        올라가고 보이는 높이가 0과 전체 사이 -> 스크롤 값 = 카드 높이의 두 배 -> 보이는 부분 없음
        """
        items = _add(window, *[D.WAITING] * 12)
        bar = window.listView.verticalScrollBar()
        card = _focus_by_key(qtbot, window, items[0])
        ring = _ring(window)
        qtbot.waitUntil(lambda: ring.isVisible() and ring.geometry().top() == card.y() - RING_REACH)
        assert bar.maximum() >= 2 * card.height(), "전제: 카드 두 장 높이만큼 굴릴 수 있다"
        bar.setValue(0)
        _settle()
        before = ring.mapTo(window, QPoint(0, 0)).y()
        assert ring.visibleRegion().boundingRect().height() == ring.height(), "전제: 다 보인다"
        half = card.height() // 2
        bar.setValue(half)
        _settle()
        assert ring.mapTo(window, QPoint(0, 0)).y() == before - half
        assert ring.geometry().top() == card.y() - RING_REACH
        assert 0 < ring.visibleRegion().boundingRect().height() < ring.height()
        bar.setValue(2 * card.height())
        _settle()
        assert ring.visibleRegion().isEmpty()

    def test_ring_has_room_on_every_side_while_the_scrollbar_shows(
        self, window: VodDownloader, qtbot: QtBot
    ) -> None:
        """스크롤바가 보여도 카드 ↔ 스크롤바 사이에 focus ring이 들어가 네 변 모두 4px 밖이어야 한다.

        카드 12장(스크롤바가 보임), 첫째에 키보드 초점 -> 카드 오른쪽 ↔ 컨테이너 오른쪽(스크롤바 앞)
        >= 4, focus ring = 카드를 네 변 4px씩 넓힌 사각형, 붙여 그린 변 없음 (#561)
        """
        items = _add(window, *[D.WAITING] * 12)
        assert window.listView.verticalScrollBar().isVisible(), "전제: 스크롤바가 보인다"
        card = _focus_by_key(qtbot, window, items[0])
        ring = _ring(window)
        qtbot.waitUntil(ring.isVisible)
        box = card.geometry()
        room = card.parentWidget().width() - (box.x() + box.width())

        assert room >= RING_REACH, f"카드 ↔ 스크롤바 사이가 {room}px"
        assert ring.geometry() == box.adjusted(-RING_REACH, -RING_REACH, RING_REACH, RING_REACH)
        assert ring.attachedSides() == ()

    @pytest.mark.parametrize("wide", [True, False])
    def test_ring_follows_the_window_width(
        self, window: VodDownloader, qtbot: QtBot, wide: bool
    ) -> None:
        """창 폭이 바뀌어 카드 폭이 바뀌면 focus ring도 새 카드 자리를 둘러야 한다.

        카드 셋, 둘째에 키보드 초점 -> 창 폭 1200 또는 최소폭 -> 카드 폭이 바뀜, focus ring이 그 카드를 두름
        """
        items = _add(window, D.WAITING, D.WAITING, D.WAITING)
        card = _focus_by_key(qtbot, window, items[1])
        qtbot.waitUntil(lambda: _ring_wraps(window, card))
        before = card.width()
        window.resize(1200 if wide else window.minimumWidth(), 700)
        qtbot.waitUntil(
            lambda: card.width() != before
        )  # resize()는 요청이다 — 실제로 바뀐 것을 본다
        assert (card.width() > before) is wide
        qtbot.waitUntil(lambda: _ring_wraps(window, card))

    def test_ring_follows_the_card_to_its_sorted_place(
        self, window: VodDownloader, qtbot: QtBot
    ) -> None:
        """정렬로 초점 카드의 자리가 바뀌면 focus ring이 새 자리를 둘러야 한다.

        카드 셋(카드 0 · 1 · 2), 첫째에 키보드 초점 -> 제목 내림차순 정렬 -> 첫째의 y가 커짐,
        키보드 초점은 그대로, focus ring이 첫째를 두름
        """
        items = _add(window, D.WAITING, D.WAITING, D.WAITING)
        card = _focus_by_key(qtbot, window, items[0])
        qtbot.waitUntil(lambda: _ring_wraps(window, card))
        before = card.y()
        window.contentManager.order.setSort("title", True)
        qtbot.waitUntil(lambda: card.y() > before)
        assert card.hasKeyFocus()
        qtbot.waitUntil(lambda: _ring_wraps(window, card))

    def test_ring_follows_when_a_filter_hides_the_card_above(
        self, window: VodDownloader, qtbot: QtBot
    ) -> None:
        """필터가 위 카드를 숨겨 초점 카드가 올라가면 focus ring이 그 자리를 둘러야 한다.

        완료 · 대기 카드, 대기 카드에 키보드 초점 -> 「대기」 필터 -> 완료 카드가 숨고 대기 카드의 y가 줄어듦,
        키보드 초점은 그대로, focus ring이 대기 카드를 두름
        """
        items = _add(window, D.FINISHED, D.WAITING)
        card = _focus_by_key(qtbot, window, items[1])
        qtbot.waitUntil(lambda: _ring_wraps(window, card))
        before = card.y()
        window.filterChips["waiting"].click()
        qtbot.waitUntil(lambda: card.y() < before)
        assert _card(window, items[0]).isHidden()
        assert card.hasKeyFocus()
        qtbot.waitUntil(lambda: _ring_wraps(window, card))

    def test_ring_hides_when_a_filter_hides_the_focused_card(
        self, window: VodDownloader, qtbot: QtBot
    ) -> None:
        """필터가 초점 카드를 숨기면 focus ring도 숨어야 한다.

        완료 · 대기 카드, 완료 카드에 키보드 초점 -> 「대기」 필터 -> 완료 카드가 숨음, focus ring이 숨음
        """
        items = _add(window, D.FINISHED, D.WAITING)
        card = _focus_by_key(qtbot, window, items[0])
        qtbot.waitUntil(lambda: _ring_wraps(window, card))
        window.filterChips["waiting"].click()
        qtbot.waitUntil(card.isHidden)
        qtbot.waitUntil(lambda: not _ring(window).isVisible())

    def test_ring_moves_to_the_next_card_after_delete_and_back_after_undo(
        self, window: VodDownloader, qtbot: QtBot
    ) -> None:
        """초점 카드를 지우면 focus ring이 초점을 넘겨받은 카드를 두르고, 되살려 자리가 밀려도 따라가야 한다.

        카드 셋, 첫째에 키보드 초점 · Delete -> 둘째가 키보드 초점, focus ring이 둘째를 두름
        -> Ctrl+Z -> 둘째의 y가 커짐, focus ring이 둘째를 두름
        """
        items = _add(window, D.WAITING, D.WAITING, D.WAITING)
        first = _focus_by_key(qtbot, window, items[0])
        qtbot.waitUntil(lambda: _ring_wraps(window, first))
        _key(first, Qt.Key.Key_Delete)
        assert window.listView.widgetFor(items[0]) is None, "전제: 첫째가 지워졌다"
        second = _card(window, items[1])
        qtbot.waitUntil(second.hasKeyFocus)
        qtbot.waitUntil(lambda: _ring_wraps(window, second))
        before = second.y()
        _undo(window)
        qtbot.waitUntil(lambda: second.y() > before)
        assert window.listView.widgetFor(items[0]) is not None
        qtbot.waitUntil(lambda: _ring_wraps(window, second))

    def test_ring_hides_when_focus_leaves_the_list(
        self, window: VodDownloader, qtbot: QtBot
    ) -> None:
        """초점이 목록 밖으로 나가면 focus ring이 숨어야 한다.

        카드 둘, 첫째에 키보드 초점 -> URL 입력칸에 초점 -> focus ring이 숨음
        """
        items = _add(window, D.WAITING, D.WAITING)
        card = _focus_by_key(qtbot, window, items[0])
        qtbot.waitUntil(lambda: _ring_wraps(window, card))
        _focus(qtbot, window, window.urlInput, Qt.FocusReason.TabFocusReason)
        qtbot.waitUntil(lambda: not _ring(window).isVisible())

    def test_mouse_focus_does_not_show_the_ring(self, window: VodDownloader, qtbot: QtBot) -> None:
        """마우스로 온 초점은 focus ring을 나타내지 않아야 한다.

        카드 둘, 첫째에 마우스 초점(MouseFocusReason) -> 첫째가 초점을 받음, 키보드 초점 아님, focus ring 숨음
        -> 둘째에 키보드 초점 -> focus ring 보임(같은 창에서 초점 이벤트가 온다는 표식)
        """
        items = _add(window, D.WAITING, D.WAITING)
        first = _card(window, items[0])
        _focus(qtbot, window, first, Qt.FocusReason.MouseFocusReason)
        assert not first.hasKeyFocus()
        assert not _ring(window).isVisible()
        second = _focus_by_key(qtbot, window, items[1])
        qtbot.waitUntil(lambda: _ring_wraps(window, second))

    def test_clicking_another_card_hides_the_ring(
        self, window: VodDownloader, qtbot: QtBot
    ) -> None:
        """키보드 초점 카드가 있을 때 다른 카드를 클릭하면 focus ring이 숨어야 한다.

        카드 둘, 첫째에 키보드 초점 -> 둘째 카드 면 클릭 -> 첫째가 키보드 초점을 잃음, focus ring 숨음
        """
        items = _add(window, D.WAITING, D.WAITING)
        first = _focus_by_key(qtbot, window, items[0])
        qtbot.waitUntil(lambda: _ring_wraps(window, first))
        _click(window, items[1])
        qtbot.waitUntil(lambda: not first.hasKeyFocus())
        qtbot.waitUntil(lambda: not _ring(window).isVisible())

    def test_ring_is_the_same_in_and_out_of_select_mode(
        self, window: VodDownloader, qtbot: QtBot
    ) -> None:
        """focus ring의 자리와 색은 선택 모드 안팎에서 같아야 한다.

        카드 셋, 둘째에 키보드 초점 -> focus ring 자리 · 카드 바깥 화소 1~5 기록 -> Ctrl+A로 들어가 Space로
        둘째의 고름을 풂 -> 자리 · 화소가 같음
        """
        items = _add(window, D.WAITING, D.WAITING, D.WAITING)
        card = _focus_by_key(qtbot, window, items[1])
        qtbot.waitUntil(lambda: _ring_wraps(window, card))
        place = _ring(window).geometry()
        pixels = [_outside(window, card, out).name() for out in range(1, 6)]
        _key(card, Qt.Key.Key_A, CTRL)
        assert window.listView.isSelecting() and card.hasKeyFocus()
        _key(card, Qt.Key.Key_Space)
        assert not card.isSelected(), "전제: 초점 카드는 고르지 않은 모양이다"
        assert _ring(window).isVisible() and _ring(window).geometry() == place
        assert [_outside(window, card, out).name() for out in range(1, 6)] == pixels

    def test_ring_takes_no_mouse(self, window: VodDownloader, qtbot: QtBot) -> None:
        """focus ring이 덮은 자리의 클릭은 밑의 카드로 가야 한다.

        선택 모드(Ctrl+A로 들어가 Space로 둘 다 풂), 첫째에 키보드 초점(focus ring이 카드 전체를 덮음)
        -> 창의 그 카드 면 자리를 클릭 -> 그 자리의 위젯이 focus ring이 아님, 첫째가 골라짐
        """
        items = _add(window, D.WAITING, D.WAITING)
        card = _focus_by_key(qtbot, window, items[0])
        qtbot.waitUntil(lambda: _ring_wraps(window, card))
        _key(card, Qt.Key.Key_A, CTRL)
        _key(card, Qt.Key.Key_Space)
        _key(_card(window, items[1]), Qt.Key.Key_Space)
        assert window.listView.isSelecting() and _picked(window) == [], "전제: 고른 카드가 없다"
        spot = _spot(window, items[0])
        ring = _ring(window)
        assert ring.isVisible() and ring.geometry().contains(card.geometry()), "전제: 덮고 있다"
        assert window.childAt(spot) is not ring
        QTest.mouseClick(window.windowHandle(), LEFT, NO_MODIFIER, spot)
        _settle()
        assert _picked(window) == [items[0]]

    def test_ring_is_the_focus_token_colour_two_pixels_off_the_card(
        self, window: VodDownloader, qtbot: QtBot
    ) -> None:
        """focus ring은 카드 테두리에서 2px 띄운 2px 굵기의 focusOnAccent 색 선이어야 한다.

        카드 셋, 둘째에 키보드 초점 -> 카드 왼쪽 바깥 화소 1 · 2 ≈ windowBg(틈), 3 · 4 ≈ focusOnAccent,
        5 ≈ windowBg
        """
        items = _add(window, D.WAITING, D.WAITING, D.WAITING)
        card = _focus_by_key(qtbot, window, items[1])
        qtbot.waitUntil(lambda: _ring_wraps(window, card))
        assert _gap(QColor(theme.DARK["focusOnAccent"]), QColor(theme.DARK["windowBg"])) > 200
        assert _is(_outside(window, card, 1), "windowBg") and _is(
            _outside(window, card, 2), "windowBg"
        )
        assert _is(_outside(window, card, 3), "focusOnAccent")
        assert _is(_outside(window, card, 4), "focusOnAccent")
        assert _is(_outside(window, card, 5), "windowBg")

    def test_ring_repaints_in_the_new_theme_after_a_runtime_switch(
        self, qapp: QApplication, window: VodDownloader, qtbot: QtBot
    ) -> None:
        """실행 중 라이트 테마로 바꾸면 focus ring이 라이트의 focusOnAccent 색으로 다시 그려져야 한다.

        다크, 카드에 키보드 초점 -> 바깥 화소 3 ≈ 다크 focusOnAccent -> apply_color_scheme("light")
        -> 바깥 화소 3 ≈ 라이트 focusOnAccent
        """
        items = _add(window, D.WAITING, D.WAITING)
        card = _focus_by_key(qtbot, window, items[0])
        qtbot.waitUntil(lambda: _ring_wraps(window, card))
        assert _is(_outside(window, card, 3), "focusOnAccent"), "전제: 다크 색으로 그렸다"
        light = QColor(theme.LIGHT["focusOnAccent"])
        assert _gap(QColor(theme.DARK["focusOnAccent"]), light) > COLOUR_SLACK
        try:
            theme.apply_color_scheme(qapp, "light", QSS_PATH)
            _settle()
            assert _gap(_outside(window, card, 3), light) <= COLOUR_SLACK
        finally:
            theme.apply_color_scheme(qapp, "dark", QSS_PATH)

    @pytest.mark.parametrize("selecting", [False, True])
    def test_focused_card_keeps_its_ordinary_border(
        self, window: VodDownloader, qtbot: QtBot, selecting: bool
    ) -> None:
        """키보드 초점을 받은(안 고른) 카드의 테두리는 초점 없는 카드와 같은 평소 테두리여야 한다.

        카드 둘(선택 모드 밖 · 안), 첫째에 키보드 초점 -> 첫째의 가장자리 화소 0 = 둘째의 화소 0 ≈ border,
        화소 1 ≈ cardBg
        """
        items = _add(window, D.WAITING, D.WAITING)
        if selecting:
            _toggle_select_mode(window, True)
        card = _focus_by_key(qtbot, window, items[0])
        qtbot.waitUntil(lambda: _ring_wraps(window, card))
        assert not card.isSelected(), "전제: 고르지 않았다"
        assert _gap(QColor(theme.DARK["border"]), QColor(theme.DARK["accent"])) > COLOUR_SLACK
        assert _edge(card, 0) == _edge(_card(window, items[1]), 0)
        assert _is(_edge(card, 0), "border", 8)
        assert _is(_edge(card, 1), "cardBg")

    def test_picked_and_focused_is_accent_border_gap_and_ring(
        self, window: VodDownloader, qtbot: QtBot
    ) -> None:
        """고른 카드가 키보드 초점을 받으면 강조색 2px 테두리 · 2px 틈 · focus ring이 차례로 보여야 한다.

        선택 모드, 카드에 키보드 초점 · Space -> 가장자리 화소 0 · 1 ≈ accent, 화소 2 ≈ cardBg(안쪽 선 없음),
        바깥 화소 1 · 2 ≈ windowBg, 3 · 4 ≈ focusOnAccent
        """
        items = _add(window, D.WAITING, D.WAITING)
        _toggle_select_mode(window, True)
        card = _focus_by_key(qtbot, window, items[0])
        qtbot.waitUntil(lambda: _ring_wraps(window, card))
        _key(card, Qt.Key.Key_Space)
        assert card.isSelected() and card.hasKeyFocus()
        assert _is(_edge(card, 0), "accent") and _is(_edge(card, 1), "accent")
        assert _is(_edge(card, 2), "cardBg")
        assert _is(_outside(window, card, 1), "windowBg") and _is(
            _outside(window, card, 2), "windowBg"
        )
        assert _is(_outside(window, card, 3), "focusOnAccent")
        assert _is(_outside(window, card, 4), "focusOnAccent")

    def test_picked_only_is_an_accent_border_two_wide(
        self, window: VodDownloader, qtbot: QtBot
    ) -> None:
        """고르기만 한(키보드 초점 없는) 카드는 강조색 2px 테두리이고 바깥에 focus ring이 없어야 한다.

        선택 모드, 카드 둘, 첫째 클릭 · 둘째에 키보드 초점 -> 첫째의 화소 0 · 1 ≈ accent, 화소 2 ≈ cardBg,
        첫째의 바깥 화소 3 ≈ windowBg, focus ring은 둘째를 두름
        """
        items = _add(window, D.WAITING, D.WAITING)
        _toggle_select_mode(window, True)
        _click(window, items[0])
        second = _focus_by_key(qtbot, window, items[1])
        qtbot.waitUntil(lambda: _ring_wraps(window, second))
        card = _card(window, items[0])
        assert card.isSelected() and not card.hasKeyFocus()
        assert _is(_edge(card, 0), "accent") and _is(_edge(card, 1), "accent")
        assert _is(_edge(card, 2), "cardBg")
        assert _is(_outside(window, card, 3), "windowBg")


class TestFocusRingWidget:
    """`FocusRing` 하나만 빈 위젯 위에 놓고 잰다 — 목록 배선 없이 따라가기 규칙만."""

    @pytest.fixture
    def host(self, qapp: QApplication) -> Iterator[QWidget]:
        """보이는 창 안의 400×300 빈 위젯 — 창이 아니라서 `resize()`가 바로 그 크기가 된다.

        창은 이 픽스처가 붙들고 있다가 `_drop_windows`가 정리한다.
        """
        top = QWidget()
        top.resize(400, 300)
        inner = QWidget(top)
        inner.setGeometry(0, 0, 400, 300)
        top.show()
        _settle()
        yield inner

    @staticmethod
    def _plain_card(host: QWidget, box: QRect) -> QWidget:
        card = QWidget(host)
        card.setGeometry(box)
        card.show()
        return card

    def test_follow_wraps_the_target_and_none_hides(self, host: QWidget) -> None:
        """`follow(card)`는 그 위젯을 4px 밖에서 두르고 `follow(None)`은 숨겨야 한다.

        위젯 (40, 30, 200, 80) -> follow -> focus ring (36, 26, 208, 88) · 보임 · target = 그 위젯
        -> follow(None) -> 숨음 · target = None
        """
        card = self._plain_card(host, QRect(40, 30, 200, 80))
        ring = FocusRing(host)
        assert not ring.isVisible()
        ring.follow(card)
        assert ring.isVisible() and ring.target() is card
        assert ring.geometry() == QRect(36, 26, 208, 88)
        ring.follow(None)
        assert not ring.isVisible() and ring.target() is None

    def test_ring_hides_and_shows_with_its_target(self, host: QWidget) -> None:
        """따라가는 위젯이 숨으면 focus ring도 숨고 다시 보이면 나타나야 한다.

        follow -> 위젯 hide() -> focus ring 숨음 -> 위젯 show() -> focus ring 보임
        """
        card = self._plain_card(host, QRect(40, 30, 200, 80))
        ring = FocusRing(host)
        ring.follow(card)
        assert ring.isVisible(), "전제: 보였다"
        card.hide()
        _settle()
        assert not ring.isVisible()
        card.show()
        _settle()
        assert ring.isVisible() and ring.geometry() == QRect(36, 26, 208, 88)

    def test_ring_follows_move_and_resize_of_its_target(self, host: QWidget) -> None:
        """따라가는 위젯을 옮기거나 크기를 바꾸면 focus ring이 새 자리를 둘러야 한다.

        follow -> 위젯을 (60, 100)으로 옮김 -> focus ring (56, 96, 208, 88)
        -> 위젯 크기 (120, 40) -> focus ring (56, 96, 128, 48)
        """
        card = self._plain_card(host, QRect(40, 30, 200, 80))
        ring = FocusRing(host)
        ring.follow(card)
        card.move(60, 100)
        _settle()
        assert ring.geometry() == QRect(56, 96, 208, 88)
        card.resize(120, 40)
        _settle()
        assert ring.geometry() == QRect(56, 96, 128, 48)

    def test_side_without_room_is_attached_and_host_resize_replaces(self, host: QWidget) -> None:
        """여유가 4px보다 작은 변만 붙여 그리고, 부모 크기가 바뀌어 여유가 달라지면 다시 놓아야 한다.

        위젯 (2, 30, 200, 80) -> 왼쪽은 2px만 나감 · 붙여 그린 변 = 왼쪽, focus ring (0, 26, 206, 88)
        -> 부모 폭 203(오른쪽 여유 1) -> 오른쪽은 1px만 나감, 붙여 그린 변 = 왼쪽 · 오른쪽
        """
        card = self._plain_card(host, QRect(2, 30, 200, 80))
        ring = FocusRing(host)
        ring.follow(card)
        assert ring.geometry() == QRect(0, 26, 206, 88)
        assert ring.attachedSides() == ("left",)
        host.resize(203, 300)
        _settle()
        assert host.width() == 203, "전제: 부모 폭이 바뀌었다"
        assert ring.geometry() == QRect(0, 26, 203, 88)
        assert ring.attachedSides() == ("left", "right")

    def test_ring_is_transparent_for_the_mouse(self, host: QWidget) -> None:
        """focus ring은 마우스 이벤트를 받지 않아 그 자리의 위젯 조회가 밑의 위젯을 돌려줘야 한다.

        follow -> WA_TransparentForMouseEvents 켜짐, host.childAt(위젯 가운데) = 그 위젯
        """
        card = self._plain_card(host, QRect(40, 30, 200, 80))
        ring = FocusRing(host)
        ring.follow(card)
        assert ring.isVisible(), "전제: 보인다"
        assert ring.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        assert host.childAt(card.geometry().center()) is card


class TestPickedBorderOverProgressBar:
    @pytest.mark.parametrize("state", [D.RUNNING, D.PAUSED, D.FINISHED])
    def test_bottom_border_of_a_picked_card_is_accent_over_the_bar(
        self, window: VodDownloader, state: DownloadState
    ) -> None:
        """progress bar가 보이는 카드를 고르면 아래 변 2px가 progress bar 위에서도 강조색이어야 한다.

        받는 중 · 일시정지 · 완료 카드(progress bar 보임), 선택 모드에서 클릭 -> 카드 면 가로 가운데의
        아래에서 첫째 · 둘째 화소 ≈ accent(고르기 전 둘째 화소는 accent가 아님), 셋째 화소는 accent가 아님
        """
        items = _add(window, state)
        card = _card(window, items[0])
        face = card.contentFrame
        assert card.progressBar.isVisible(), "전제: progress bar가 보인다"
        x, bottom = face.width() / 2, face.height() - 1
        before = face.grab().toImage()
        assert not _is(_colour_at(before, x, bottom - 1), "accent"), (
            "전제: 고르기 전은 강조색이 아니다"
        )
        _toggle_select_mode(window, True)
        _click(window, items[0])
        assert card.isSelected(), "전제: 골랐다"
        image = face.grab().toImage()
        assert _is(_colour_at(image, x, bottom), "accent")
        assert _is(_colour_at(image, x, bottom - 1), "accent")
        assert not _is(_colour_at(image, x, bottom - 2), "accent")


class TestPickOnRelease:
    """창(`QWindow`)에 누름 · 옮김 · 뗌을 따로 보내 잰다 — 누른 뒤의 이벤트는 Qt가 누른 카드로 보낸다."""

    def test_pressing_does_not_pick_until_released_inside(self, window: VodDownloader) -> None:
        """선택 모드에서 카드를 누르기만 하면 고르지 않고, 카드 안에서 떼면 골라야 한다.

        선택 모드, 카드 둘, 첫째를 누름 -> 고른 카드 없음 · 눌린 표시 켜짐 -> 그 자리에서 뗌
        -> 고른 카드 = 첫째 · 눌린 표시 꺼짐
        """
        items = _add(window, D.WAITING, D.WAITING)
        _toggle_select_mode(window, True)
        card = _card(window, items[0])
        _press(window, items[0])
        assert _picked(window) == []
        assert card.isPressed()
        _release(window, items[0])
        assert _picked(window) == [items[0]]
        assert not card.isPressed()

    def test_releasing_outside_the_card_does_nothing(self, window: VodDownloader) -> None:
        """누른 채 카드 밖으로 나가 떼면 아무 카드도 고르지 않아야 한다.

        선택 모드, 카드 둘, 첫째를 누름 -> 둘째 위로 옮겨 뗌 -> 고른 카드 없음, 두 카드 모두 눌린 표시 꺼짐
        """
        items = _add(window, D.WAITING, D.WAITING)
        _toggle_select_mode(window, True)
        _press(window, items[0])
        assert _card(window, items[0]).isPressed(), "전제: 눌린 표시가 켜졌다"
        _drag_to(window, items[1])
        _release(window, items[1])
        assert _picked(window) == []
        assert not _card(window, items[0]).isPressed()
        assert not _card(window, items[1]).isPressed()

    def test_pressed_mark_follows_leaving_and_coming_back(self, window: VodDownloader) -> None:
        """눌린 표시는 누른 채 카드 밖으로 나가면 꺼지고 돌아오면 켜지며, 돌아와 떼면 골라야 한다.

        선택 모드, 카드 둘, 첫째를 누름(켜짐) -> 둘째 위로 옮김(꺼짐 · 둘째도 꺼짐) -> 첫째로 돌아옴(켜짐)
        -> 뗌(꺼짐) -> 고른 카드 = 첫째
        """
        items = _add(window, D.WAITING, D.WAITING)
        _toggle_select_mode(window, True)
        first, second = _card(window, items[0]), _card(window, items[1])
        _press(window, items[0])
        assert first.isPressed() and first.contentFrame.property("pressed") is True
        _drag_to(window, items[1])
        assert not first.isPressed() and first.contentFrame.property("pressed") is not True
        assert not second.isPressed()
        assert _picked(window) == []
        _drag_to(window, items[0])
        assert first.isPressed() and first.contentFrame.property("pressed") is True
        _release(window, items[0])
        assert not first.isPressed()
        assert _picked(window) == [items[0]]

    @pytest.mark.parametrize(
        ("state", "pressed", "hover"),
        [
            (D.WAITING, "cardPressedBg", "cardHoverBg"),
            (D.FINISHED, "cardTintFinishedPressed", "cardTintFinishedHover"),
            (D.FAILED, "cardTintFailedPressed", "cardTintFailedHover"),
        ],
    )
    def test_pressed_card_ground_is_the_pressed_token(
        self, window: VodDownloader, state: DownloadState, pressed: str, hover: str
    ) -> None:
        """누르고 있는 카드의 바탕은 그 카드 종류의 눌린 색이고 떼면 눌린 색이 아니어야 한다.

        선택 모드, 대기 · 완료 · 실패 카드를 누름 -> 카드 면 왼쪽에서 넷째 화소 ≈ 눌린 색 토큰(한도 8),
        호버 색 토큰은 아님 -> 카드 밖으로 옮김 -> 눌린 색이 아님
        """
        items = _add(window, state, D.WAITING)
        _toggle_select_mode(window, True)
        card = _card(window, items[0])
        tokens = theme.current_tokens()
        assert _gap(QColor(tokens[pressed]), QColor(tokens[hover])) > 16, (
            "전제: 두 색이 한도보다 멀다"
        )
        _press(window, items[0])
        assert card.isPressed(), "전제: 눌린 표시가 켜졌다"
        assert _is(_edge(card, 3), pressed, 8)
        assert not _is(_edge(card, 3), hover, 8)
        _drag_to(window, items[1])
        assert not _is(_edge(card, 3), pressed, 8)
        _release(window, items[1])

    @pytest.mark.parametrize(
        ("state", "token"),
        [(D.RUNNING, "cardFillRunning"), (D.PAUSED, "cardFillPaused")],
    )
    def test_pressed_fill_is_the_pressed_fill_token(
        self, window: VodDownloader, state: DownloadState, token: str
    ) -> None:
        """배경 채움 방식에서 누르고 있는 카드의 채운 자리는 채움의 눌린 색이어야 한다.

        배경 채움 · 선택 모드, 40% 받는 중 · 일시정지 카드 -> 면 왼쪽에서 넷째 화소 ≈ 채움 색
        -> 누름 -> ≈ 채움의 눌린 색(한도 8), 채움 색은 아님
        """
        card_display.use(card_display.CardDisplay(animations=False, progress="fill"))
        item = _item("채움 카드", state)
        item.download_progress = 40
        window.contentManager.model.addItem(item)
        _settle()
        _toggle_select_mode(window, True)
        card = _card(window, item)
        tokens = theme.current_tokens()
        assert _gap(QColor(tokens[token]), QColor(tokens[token + "Pressed"])) > 16
        assert _is(_edge(card, 3), token, 8), "전제: 누르기 전은 채움 색이다"
        _press(window, item)
        assert card.isPressed(), "전제: 눌린 표시가 켜졌다"
        assert _is(_edge(card, 3), token + "Pressed", 8)
        assert not _is(_edge(card, 3), token, 8)
        _release(window, item)

    def test_shift_range_is_added_on_release_with_the_modifier_of_the_press(
        self, window: VodDownloader
    ) -> None:
        """Shift 범위는 뗄 때 더해지고, Shift 여부는 누를 때의 값이어야 한다.

        선택 모드, 카드 셋, 첫째 클릭 -> 셋째를 Shift와 함께 누름 -> 고른 카드 = 첫째뿐
        -> Shift 없이 뗌 -> 고른 카드 = 셋 모두
        """
        items = _add(window, D.WAITING, D.WAITING, D.WAITING)
        _toggle_select_mode(window, True)
        _click(window, items[0])
        _press(window, items[2], SHIFT)
        assert _picked(window) == [items[0]]
        _release(window, items[2])
        assert sorted(_picked(window), key=items.index) == items

    def test_shift_held_only_at_release_does_not_make_a_range(self, window: VodDownloader) -> None:
        """누를 때 Shift가 없었으면 뗄 때 Shift가 눌려 있어도 그 카드 하나만 골라야 한다.

        선택 모드, 카드 셋, 첫째 클릭 -> 셋째를 수정자 없이 누름 -> Shift와 함께 뗌 -> 고른 카드 = 첫째 · 셋째
        """
        items = _add(window, D.WAITING, D.WAITING, D.WAITING)
        _toggle_select_mode(window, True)
        _click(window, items[0])
        _press(window, items[2])
        _release(window, items[2], SHIFT)
        assert sorted(_picked(window), key=items.index) == [items[0], items[2]]

    @pytest.mark.parametrize("modifier", [CTRL, SHIFT])
    def test_modifier_click_outside_select_mode_enters_on_release(
        self, window: VodDownloader, modifier: Qt.KeyboardModifier
    ) -> None:
        """선택 모드 밖의 Ctrl · Shift+클릭은 누르는 동안 눌린 표시만 켜고 뗄 때 선택 모드로 들어가며 골라야 한다.

        카드 둘, 첫째를 Ctrl(또는 Shift)과 함께 누름 -> 선택 모드 아님 · 눌린 표시 켜짐 -> 뗌
        -> 선택 모드 · 고른 카드 = 첫째
        """
        items = _add(window, D.WAITING, D.WAITING)
        card = _card(window, items[0])
        _press(window, items[0], modifier)
        assert not window.listView.isSelecting()
        assert card.isPressed()
        _release(window, items[0], modifier)
        assert window.listView.isSelecting()
        assert _picked(window) == [items[0]]
        assert not card.isPressed()

    def test_modifier_click_released_outside_does_not_enter(self, window: VodDownloader) -> None:
        """선택 모드 밖에서 Ctrl과 함께 누른 뒤 카드 밖에서 떼면 선택 모드로 들어가지 않아야 한다.

        카드 둘, 첫째를 Ctrl과 함께 누름 -> 둘째 위로 옮겨 뗌 -> 선택 모드 아님
        """
        items = _add(window, D.WAITING, D.WAITING)
        _press(window, items[0], CTRL)
        assert _card(window, items[0]).isPressed(), "전제: 눌린 표시가 켜졌다"
        _drag_to(window, items[1])
        _release(window, items[1], CTRL)
        assert not window.listView.isSelecting()

    def test_plain_click_outside_select_mode_shows_no_pressed_mark(
        self, window: VodDownloader
    ) -> None:
        """선택 모드 밖의 수정자 없는 누름은 눌린 표시를 켜지 않고 떼도 아무 일이 없어야 한다.

        카드 둘, 첫째를 수정자 없이 누름 -> 눌린 표시 꺼짐 -> 뗌 -> 선택 모드 아님
        """
        items = _add(window, D.WAITING, D.WAITING)
        card = _card(window, items[0])
        _press(window, items[0])
        assert not card.isPressed()
        assert card.contentFrame.property("pressed") is not True
        _release(window, items[0])
        assert not window.listView.isSelecting()

    def test_double_click_in_select_mode_toggles_twice(self, window: VodDownloader) -> None:
        """선택 모드의 더블 클릭은 고르기와 풀기가 한 번씩 일어나야 한다.

        선택 모드, 카드 하나를 더블 클릭 -> 고름이 바뀔 때마다 적은 고른 카드 목록 = [[그 카드], []]
        """
        items = _add(window, D.WAITING, D.WAITING)
        _toggle_select_mode(window, True)
        seen: list[list[ContentItem]] = []
        window.listView.selectionChanged.connect(lambda: seen.append(_picked(window)))
        QTest.mouseDClick(window.windowHandle(), LEFT, NO_MODIFIER, _spot(window, items[0]))
        _settle()
        assert seen == [[items[0]], []]

    def test_right_button_does_not_pick(self, window: VodDownloader) -> None:
        """선택 모드에서 오른쪽 버튼은 눌린 표시를 켜지 않고 떼도 고르지 않아야 한다.

        선택 모드, 카드를 오른쪽 버튼으로 누름 -> 눌린 표시 꺼짐 -> 뗌 -> 고른 카드 없음
        """
        items = _add(window, D.WAITING, D.WAITING)
        _toggle_select_mode(window, True)
        card = _card(window, items[0])
        _press(window, items[0], button=Qt.MouseButton.RightButton)
        assert not card.isPressed()
        _release(window, items[0], button=Qt.MouseButton.RightButton)
        assert _picked(window) == []
        assert window.listView.isSelecting(), "전제: 선택 모드는 그대로다"


# ---- Space도 뗄 때 고르기 (#561, 오너 2026-11-11) ----


def _space(
    widget: QWidget,
    kind: QEvent.Type,
    modifiers: Qt.KeyboardModifier = NO_MODIFIER,
    repeat: bool = False,
) -> None:
    """초점 위젯에 Space 누름(`KeyPress`) 또는 뗌(`KeyRelease`) 하나만 보낸다."""
    QApplication.sendEvent(widget, QKeyEvent(kind, Qt.Key.Key_Space, modifiers, " ", repeat))
    _settle()


PRESS = QEvent.Type.KeyPress
LIFT = QEvent.Type.KeyRelease


class TestSpaceOnRelease:
    """Space 누름 · 뗌을 따로 보내 잰다 — 키 이벤트는 초점 위젯에 바로 보낸다."""

    def _picking(self, win: VodDownloader, count: int) -> list[ContentItem]:
        items = _add(win, *[D.WAITING] * count)
        _toggle_select_mode(win, True)
        _card(win, items[0]).setFocus(Qt.FocusReason.OtherFocusReason)
        _settle()
        return items

    def test_pressing_space_only_shows_the_pressed_ground(self, window: VodDownloader) -> None:
        """선택 모드에서 초점 카드에 Space를 누르기만 하면 고르지 않고 눌린 바탕만 켜야 한다.

        선택 모드, 카드 둘, 첫째에 초점, Space 누름 -> 고른 카드 없음 · 첫째 눌린 바탕 켜짐
        """
        items = self._picking(window, 2)
        card = _card(window, items[0])
        _space(card, PRESS)
        assert _picked(window) == []
        assert card.isPressed()

    def test_releasing_space_picks_and_clears_the_pressed_ground(
        self, window: VodDownloader
    ) -> None:
        """Space를 누른 뒤 떼면 그때 초점 카드를 고르고 눌린 바탕을 꺼야 한다.

        선택 모드, 첫째에 초점, Space 누름 → 뗌 -> [첫째] · 눌린 바탕 꺼짐
        """
        items = self._picking(window, 2)
        card = _card(window, items[0])
        _space(card, PRESS)
        _space(card, LIFT)
        assert _picked(window) == [items[0]]
        assert not card.isPressed()

    def test_moving_focus_while_holding_space_picks_nothing(self, window: VodDownloader) -> None:
        """Space를 누른 채 ↓로 초점을 옮기면 눌린 바탕이 꺼지고, 그 뒤에 떼도 아무 카드도 고르지 않아야 한다.

        선택 모드, 카드 둘, 첫째에 초점, Space 누름 · ↓ -> 초점 = 둘째, 첫째 눌린 바탕 꺼짐,
        둘째에서 Space 뗌 -> 고른 카드 없음
        """
        items = self._picking(window, 2)
        first, second = _card(window, items[0]), _card(window, items[1])
        _space(first, PRESS)
        _key(first, Qt.Key.Key_Down)
        assert window.focusWidget() is second, "전제: 초점이 둘째로 갔다"
        assert not first.isPressed()
        _space(second, LIFT)
        assert _picked(window) == []
        assert not second.isPressed()

    def test_shift_space_range_is_added_on_release(self, window: VodDownloader) -> None:
        """Shift+Space 범위는 누를 때가 아니라 뗄 때 더해야 하고, 수정자는 누를 때의 값이어야 한다.

        선택 모드, 카드 셋, 첫째 Space(누름 · 뗌) · ↓ · ↓, Shift+Space 누름 -> [첫째] 그대로,
        수정자 없이 뗌 -> [첫째, 둘째, 셋째]
        """
        items = self._picking(window, 3)
        first = _card(window, items[0])
        _space(first, PRESS)
        _space(first, LIFT)
        _key(first, Qt.Key.Key_Down)
        _key(window.focusWidget(), Qt.Key.Key_Down)
        last = _card(window, items[2])
        assert window.focusWidget() is last, "전제: 초점이 셋째로 갔다"
        _space(last, PRESS, SHIFT)
        assert _picked(window) == [items[0]]
        _space(last, LIFT)
        assert _picked(window) == items

    def test_auto_repeat_while_holding_does_not_pick(self, window: VodDownloader) -> None:
        """Space를 누르고 있어 오는 키 반복(뗌 · 누름 짝)은 고르지 않고, 손을 뗄 때 한 번만 골라야 한다.

        선택 모드, 첫째에 초점, Space 누름 · 키 반복 뗌 · 누름 두 번 -> 한 번마다 고른 카드 없음 · 눌린 바탕
        켜짐, 뗌 -> [첫째]
        """
        items = self._picking(window, 1)
        card = _card(window, items[0])
        _space(card, PRESS)
        for _ in range(2):
            _space(card, LIFT, repeat=True)
            _space(card, PRESS, repeat=True)
            # 한 번마다 본다 — 두 번 뒤에만 보면 골랐다가 다시 푼 것도 같은 값이다
            assert _picked(window) == []
            assert card.isPressed()
        _space(card, LIFT)
        assert _picked(window) == [items[0]]

    def test_mouse_press_while_holding_space_picks_once(self, window: VodDownloader) -> None:
        """Space를 누른 채 같은 카드를 마우스로 누르고 떼도 Space를 뗄 때 한 번만 골라야 한다.

        선택 모드, 첫째에 초점, Space 누름 · 첫째를 마우스로 누름 · 뗌 -> 고른 카드 없음,
        Space 뗌 -> [첫째](고름이 바뀐 횟수 1)
        """
        items = self._picking(window, 1)
        card = _card(window, items[0])
        seen: list[list[ContentItem]] = []
        window.listView.selectionChanged.connect(lambda: seen.append(_picked(window)))
        _space(card, PRESS)
        _press(window, items[0])
        _release(window, items[0])
        assert _picked(window) == []
        _space(card, LIFT)
        assert seen == [[items[0]]]

    def test_space_while_holding_the_mouse_is_ignored(self, window: VodDownloader) -> None:
        """마우스로 누른 채 Space를 누르고 떼면 Space는 무시하고 마우스를 뗄 때 한 번만 골라야 한다.

        선택 모드, 첫째를 마우스로 누름 · Space 누름 · 뗌 -> 고른 카드 없음, 마우스 뗌 -> [첫째](바뀐 횟수 1)
        """
        items = self._picking(window, 1)
        card = _card(window, items[0])
        seen: list[list[ContentItem]] = []
        window.listView.selectionChanged.connect(lambda: seen.append(_picked(window)))
        _press(window, items[0])
        focused = window.focusWidget()
        _space(focused, PRESS)
        _space(focused, LIFT)
        assert _picked(window) == []
        assert card.isPressed(), "전제: 마우스 누름이 이어진다"
        _release(window, items[0])
        assert seen == [[items[0]]]


# ---- 창을 오간 뒤의 초점 종류 (#561, 141a 정할 것 3) ----


def _show(win: VodDownloader, qtbot: QtBot) -> None:
    win.show()
    qtbot.waitExposed(win)


def _activate(qtbot: QtBot, target: QWidget) -> None:
    """그 창이 활성이 될 때까지 다시 요청하며 기다린다 — 조건 대기(`_focus`와 같은 까닭)."""

    def active() -> bool:
        if not target.isActiveWindow():
            target.activateWindow()
            return False
        return True

    qtbot.waitUntil(active)


def _leave_and_return(qtbot: QtBot, win: VodDownloader, card: ContentItemWidget) -> None:
    """다른 최상위 창을 활성으로 만들었다가 이 창을 다시 활성으로 만든다 — 카드 초점이 돌아올 때까지."""
    other = QWidget()
    other.resize(200, 100)
    other.show()
    qtbot.waitExposed(other)
    _activate(qtbot, other)
    qtbot.waitUntil(lambda: not card.hasFocus())
    _activate(qtbot, win)
    qtbot.waitUntil(card.hasFocus)
    _settle()
    other.hide()


class TestFocusKindAfterWindowSwitch:
    def test_mouse_focus_stays_mouse_after_the_window_comes_back(
        self, window: VodDownloader, qtbot: QtBot
    ) -> None:
        """마우스로 초점을 받은 카드는 창이 비활성이 됐다가 다시 활성이 돼도 키보드 초점이 아니어야 한다.

        카드 둘, 첫째에 마우스 초점 -> focus ring 숨음 · 카드 버튼 숨음, 다른 창 활성 → 이 창 활성
        -> 첫째가 초점을 되찾음, 키보드 초점 아님 · focus ring 숨음 · 카드 버튼 숨음
        """
        items = _add(window, D.WAITING, D.WAITING)
        _show(window, qtbot)
        card = _card(window, items[0])
        _focus(qtbot, window, card, Qt.FocusReason.MouseFocusReason)
        assert not card.hasKeyFocus(), "전제: 마우스 초점이다"
        assert not card.hoverBox.isVisible(), "전제: 카드 버튼이 숨어 있다"

        _leave_and_return(qtbot, window, card)

        assert not card.hasKeyFocus()
        assert not _ring(window).isVisible()
        assert not card.hoverBox.isVisible()

    def test_key_focus_stays_key_after_the_window_comes_back(
        self, window: VodDownloader, qtbot: QtBot
    ) -> None:
        """키보드로 초점을 받은 카드는 창이 비활성이 됐다가 다시 활성이 되면 키보드 초점 · focus ring이 돌아와야 한다.

        카드 둘, 첫째에 Tab 초점 -> 다른 창 활성 → 이 창 활성 -> 첫째가 키보드 초점, focus ring이 첫째를 두름
        """
        items = _add(window, D.WAITING, D.WAITING)
        _show(window, qtbot)
        card = _card(window, items[0])
        _focus(qtbot, window, card, Qt.FocusReason.TabFocusReason)
        assert card.hasKeyFocus(), "전제: 키보드 초점이다"

        _leave_and_return(qtbot, window, card)

        assert card.hasKeyFocus()
        qtbot.waitUntil(lambda: _ring_wraps(window, card))


# ---- 창 좌우 바깥 여백 (#561, 오너 2026-11-11) ----


def _left(win: VodDownloader, widget: QWidget) -> int:
    assert widget.isVisible(), f"전제: {widget.objectName()}이 보인다"
    return widget.mapTo(win, QPoint(0, 0)).x()


def _right(win: VodDownloader, widget: QWidget) -> int:
    """위젯 오른쪽 끝 바깥 x — 창 좌표."""
    assert widget.isVisible(), f"전제: {widget.objectName()}이 보인다"
    return widget.mapTo(win, QPoint(widget.width(), 0)).x()


class TestOuterMargin:
    @pytest.mark.parametrize("cards", [3, 12])  # 12장이면 스크롤바가 보인다
    def test_bars_and_cards_share_the_left_and_right_lines(
        self, window: VodDownloader, qtbot: QtBot, cards: int
    ) -> None:
        """메뉴바 · 헤더바 두 줄 · 카드 · 하단바의 왼쪽 끝이 한 선, 오른쪽 끝이 한 선이고 두 여백이 같아야 한다.

        창 900, 카드 3장(스크롤바 없음) · 12장(스크롤바 보임) -> 왼쪽 끝(메뉴 첫 버튼 · URL 칸 · 칩 묶음 ·
        첫 카드 · 하단바 속도 글)이 모두 같음, 오른쪽 끝(VOD 추가 · 선택 · 첫 카드 · 중지)이 모두 같음,
        왼쪽 여백 == 창 폭 − 오른쪽 끝
        """
        items = _add(window, *[D.WAITING] * cards)
        _show(window, qtbot)
        _settle()
        shows = window.listView.verticalScrollBar().isVisible()
        assert shows is (cards == 12), "전제: 스크롤바는 12장일 때만 보인다"
        card = _card(window, items[0])
        lefts = {
            "메뉴": _left(window, window.menuDownloadButton),
            "URL": _left(window, window.urlInput),
            "칩 묶음": _left(window, window.filterChipBar),  # 헤더바 2줄 — 폭에 따라 칩 · 드롭다운
            "카드": _left(window, card),
            "하단바": _left(window, window.footerMonitor),
        }
        rights = {
            "VOD 추가": _right(window, window.fetchButton),
            "선택": _right(window, window.selectButton),
            "카드": _right(window, card),
            "중지": _right(window, window.stopButton),
        }
        assert len(set(lefts.values())) == 1, lefts
        assert len(set(rights.values())) == 1, rights
        assert lefts["카드"] == window.width() - rights["카드"]

    def test_card_and_scrollbar_leave_room_for_the_focus_ring(
        self, window: VodDownloader, qtbot: QtBot
    ) -> None:
        """스크롤바가 보일 때 카드 오른쪽 끝 ↔ 스크롤바 왼쪽 끝이 focus ring의 띄움 + 굵기 이상이어야 한다.

        창 900, 카드 12장 -> 스크롤바 왼쪽 끝 − 첫 카드 오른쪽 끝 >= 4
        """
        items = _add(window, *[D.WAITING] * 12)
        _show(window, qtbot)
        _settle()
        bar = window.listView.verticalScrollBar()
        assert bar.isVisible(), "전제: 스크롤바가 보인다"
        room = _left(window, bar) - _right(window, _card(window, items[0]))
        assert room >= RING_REACH, f"카드 ↔ 스크롤바 {room}px"
