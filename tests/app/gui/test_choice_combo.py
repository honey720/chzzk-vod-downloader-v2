"""값을 고르는 칸(`ChoiceCombo`) — 펼침 메뉴 자리 · 호버 · 키보드 · 시그널 차례 · 휠 · 창 떠남 (#620).

칸은 실제 대화상자에 두고 실제 QSS(다크)를 태운다. 메뉴는 `showPopup`(키 · 누르기가 부르는 길)으로 열고, 포인터
이동 · 누르기는 메뉴 창(`QWindow`)에 합성 이벤트로 보낸다 — 실제 커서는 옮기지 않는다. 고를 때 시그널 차례의
기준은 같은 테스트 안에서 Qt 콤보박스(`QComboBox`)의 목록에서 골라 잰다.
"""

from collections.abc import Iterator

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QAction, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QPushButton,
    QStyle,
    QStyleOptionComboBox,
    QVBoxLayout,
    QWidget,
)

import app.theme as theme
import CVDv2 as main_module
from app.widgets.choice_combo import ChoiceCombo
from app.widgets.header_controls import ChoiceMenu
from tests.app.gui.card_helpers import drop_new_top_levels, hold_style, snapshot_top_levels

ITEMS = ("none", "sleep", "shutdown")  # 설정 창 「다운로드 뒤 동작」과 같은 세 줄
START = 2  # 처음 고른 줄 — 맨 위가 아니어서 위 · 아래 둘 다 움직일 수 있다
ACTIVE_WAIT_MS = 3000  # 창 활성을 기다리는 한도(ms) — X11 · xvfb는 활성이 비동기로 온다
ACTIVE_STEP_MS = 10  # 창 활성을 다시 보는 간격(ms)


@pytest.fixture(autouse=True)
def _environment(qapp: QApplication) -> Iterator[None]:
    """실제 QSS(다크). 테스트가 띄운 창은 끝에 파괴한다."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))
    before = snapshot_top_levels()
    yield
    drop_new_top_levels(before)
    theme.set_color_scheme("dark")
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))


def _pump() -> None:
    for _ in range(3):
        QApplication.processEvents()


def _activate(window: QWidget) -> None:
    """창을 활성으로 만들고 활성이 될 때까지 기다린다."""
    window.activateWindow()
    for _ in range(ACTIVE_WAIT_MS // ACTIVE_STEP_MS):
        if window.isActiveWindow():
            break
        QTest.qWait(ACTIVE_STEP_MS)
    assert window.isActiveWindow(), "전제: 창이 활성이다"


def _host(qtbot, combo_class: type[QComboBox] = ChoiceCombo) -> tuple[QDialog, QComboBox]:
    """칸 하나와 그 위 버튼 하나가 든 대화상자 — 보이고 활성이며, 칸에 초점이 있다."""
    dialog = QDialog()
    qtbot.addWidget(dialog)
    layout = QVBoxLayout(dialog)
    dialog.otherButton = QPushButton("other", dialog)
    layout.addWidget(dialog.otherButton)
    combo = combo_class(dialog)
    for text in ITEMS:
        combo.addItem(text, text)
    combo.setCurrentIndex(START)
    layout.addWidget(combo)
    dialog.resize(300, 160)
    dialog.show()
    qtbot.waitExposed(dialog)
    _activate(dialog)
    combo.setFocus(Qt.FocusReason.TabFocusReason)
    _pump()
    assert combo.hasFocus(), "전제: 칸에 초점이 있다"
    return dialog, combo


def _open(qtbot, combo: ChoiceCombo) -> ChoiceMenu:
    """포인터를 창 왼쪽 위 구석으로 옮긴 뒤 메뉴를 연다.

    `QMenu`는 열릴 때 포인터가 있던 자리에서 움직이지 않은 이동 · 놓기를 무시한다 — 앞 테스트가 남긴 포인터
    자리가 이번 메뉴의 줄 위면 그 줄을 누르기가 잡히지 않았다(이 파일 실측). 합성 이동이라 실제 커서는 그대로다.
    """
    QTest.mouseMove(combo.window().windowHandle(), QPoint(1, 1))
    _pump()
    combo.showPopup()
    menu = combo.menuWidget
    qtbot.waitUntil(menu.isVisible)
    _pump()
    return menu


def _action(menu: ChoiceMenu, row: int) -> QAction:
    return next(a for a in menu.choices() if a.data() == row)


def _menu_point(menu: ChoiceMenu, row: int) -> QPoint:
    return menu.actionGeometry(_action(menu, row)).center()


def _hover(menu: ChoiceMenu, row: int) -> None:
    """메뉴 창에 합성 포인터 이동을 보낸다 — 왼쪽 위를 먼저 지나 실제 이동을 만든다."""
    window = menu.windowHandle()
    QTest.mouseMove(window, QPoint(2, 2))
    QTest.mouseMove(window, _menu_point(menu, row))
    _pump()


def _click(menu: ChoiceMenu, row: int) -> None:
    """메뉴 창의 그 줄로 포인터를 옮긴 뒤 합성 누르기 · 놓기를 보낸다."""
    _hover(menu, row)
    window, point = menu.windowHandle(), _menu_point(menu, row)
    QTest.mousePress(window, Qt.MouseButton.LeftButton, pos=point)
    _pump()
    QTest.mouseRelease(window, Qt.MouseButton.LeftButton, pos=point)


def _record(combo: QComboBox) -> list[str]:
    """고를 때 나는 시그널을 차례대로 적는 목록."""
    seen: list[str] = []
    combo.currentIndexChanged.connect(lambda i: seen.append(f"currentIndexChanged({i})"))
    combo.currentTextChanged.connect(lambda t: seen.append(f"currentTextChanged({t})"))
    combo.activated.connect(lambda i: seen.append(f"activated({i})"))
    combo.textActivated.connect(lambda t: seen.append(f"textActivated({t})"))
    return seen


def _qt_list_pick(qtbot, row: int) -> list[str]:
    """Qt 콤보박스 목록에서 그 줄을 눌러 고를 때 나는 시그널 차례(기준)."""
    _dialog, combo = _host(qtbot, QComboBox)
    seen = _record(combo)
    combo.showPopup()
    view = combo.view()
    qtbot.waitUntil(view.isVisible)
    rect = view.visualRect(view.model().index(row, 0))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    qtbot.waitUntil(lambda: not view.isVisible())
    _pump()
    return seen


# ---- 펼치기 ----


class TestOpening:
    """펼치면 Qt 목록이 아니라 메뉴가 칸 아래에 뜬다."""

    def test_menu_opens_below_the_combo_left_aligned(self, qtbot) -> None:
        """펼친 메뉴는 위끝이 칸 아래끝 이상이고 왼쪽 끝이 칸 왼쪽 끝과 같아야 한다.

        칸 → showPopup
        -> 메뉴 위끝 >= 칸 아래끝, 메뉴 왼쪽 끝 == 칸 왼쪽 끝, Qt 목록은 숨김
        """
        _dialog, combo = _host(qtbot)
        menu = _open(qtbot, combo)
        bottom_left = combo.mapToGlobal(QPoint(0, combo.height()))
        assert menu.geometry().top() >= bottom_left.y()
        assert menu.geometry().left() == bottom_left.x()
        assert not combo.view().isVisible()

    def test_menu_lists_the_items_and_marks_the_current_one(self, qtbot) -> None:
        """메뉴는 칸의 항목을 차례대로 보이고 지금 값에만 ✓를 그려야 한다.

        항목 none · sleep · shutdown, 지금 값 2
        -> 메뉴 이름 (none, sleep, shutdown), ✓ 키 [2], 강조 줄 = 2
        """
        _dialog, combo = _host(qtbot)
        menu = _open(qtbot, combo)
        assert [ChoiceMenu.nameOf(a) for a in menu.choices()] == list(ITEMS)
        assert menu.markedKeys() == [START]
        assert menu.activeAction() is _action(menu, START)

    def test_menu_is_at_least_as_wide_as_the_combo(self, qtbot) -> None:
        """메뉴 폭은 칸 폭 이상이어야 한다.

        칸 폭 = 대화상자 폭 − 여백 → 메뉴 폭 >= 칸 폭
        """
        _dialog, combo = _host(qtbot)
        menu = _open(qtbot, combo)
        assert menu.width() >= combo.width()

    @pytest.mark.parametrize(
        ("key", "modifier"),
        [
            (Qt.Key.Key_Down, Qt.KeyboardModifier.AltModifier),
            (Qt.Key.Key_F4, Qt.KeyboardModifier.NoModifier),
            (Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier),
        ],
        ids=["alt_down", "f4", "space"],
    )
    def test_keyboard_opens_the_menu(self, qtbot, key: Qt.Key, modifier) -> None:
        """초점이 있는 칸에서 Alt+↓ · F4 · Space는 메뉴를 펼치고 값을 바꾸지 않아야 한다.

        지금 값 2 → 키 → 메뉴 보임, 값 2
        """
        _dialog, combo = _host(qtbot)
        QTest.keyClick(combo, key, modifier)
        qtbot.waitUntil(combo.menuWidget.isVisible)
        assert combo.currentIndex() == START

    def test_mouse_press_opens_the_menu(self, qtbot) -> None:
        """칸을 누르면 메뉴가 펼쳐져야 한다.

        칸 가운데 왼쪽 누르기 -> 메뉴 보임
        """
        _dialog, combo = _host(qtbot)
        QTest.mouseClick(combo, Qt.MouseButton.LeftButton, pos=combo.rect().center())
        qtbot.waitUntil(combo.menuWidget.isVisible)

    def test_open_state_lights_the_combo_border(self, qtbot) -> None:
        """메뉴가 열린 동안 칸의 스타일 옵션은 펼친 상태(State_On)여야 한다.

        닫힘 -> State_On 없음, 메뉴 열림 -> State_On 있음
        """
        _dialog, combo = _host(qtbot)
        option = QStyleOptionComboBox()
        combo.initStyleOption(option)
        assert not option.state & QStyle.StateFlag.State_On
        _open(qtbot, combo)
        option = QStyleOptionComboBox()
        combo.initStyleOption(option)
        assert option.state & QStyle.StateFlag.State_On


# ---- 값 ----


class TestValue:
    """호버 · Esc는 값을 바꾸지 않고, 고르기 · 닫힌 채 화살표는 바꾼다."""

    def test_hover_moves_only_the_highlight(self, qtbot) -> None:
        """메뉴에서 다른 줄 위로 포인터만 지나가면 강조만 옮기고 값은 그대로여야 한다.

        지금 값 2 → 줄 0 위로 포인터 이동(누르지 않음) → 강조 줄 0, 값 2, 시그널 없음
        """
        _dialog, combo = _host(qtbot)
        seen = _record(combo)
        menu = _open(qtbot, combo)
        _hover(menu, 0)
        assert menu.activeAction() is _action(menu, 0), "전제: 호버가 강조를 옮겼다"
        assert combo.currentIndex() == START
        assert seen == []

    def test_escape_keeps_the_value(self, qtbot) -> None:
        """펼친 메뉴에서 ↑로 강조를 옮긴 뒤 Esc로 닫으면 값은 그대로여야 한다.

        지금 값 2 → ↑ → Esc -> 메뉴 닫힘, 값 2, 시그널 없음
        """
        _dialog, combo = _host(qtbot)
        seen = _record(combo)
        menu = _open(qtbot, combo)
        QTest.keyClick(menu, Qt.Key.Key_Up)
        assert menu.activeAction() is _action(menu, 1), "전제: ↑가 강조를 옮겼다"
        QTest.keyClick(menu, Qt.Key.Key_Escape)
        qtbot.waitUntil(lambda: not menu.isVisible())
        _pump()
        assert combo.currentIndex() == START
        assert seen == []

    def test_escape_after_hover_keeps_the_value(self, qtbot) -> None:
        """다른 줄에 호버한 뒤 Esc로 닫고 다시 열면 값과 강조가 지금 값이어야 한다.

        지금 값 2 → 줄 0 호버 → Esc → 다시 펼침 -> 값 2, 강조 줄 2, ✓ [2]
        """
        _dialog, combo = _host(qtbot)
        menu = _open(qtbot, combo)
        _hover(menu, 0)
        QTest.keyClick(menu, Qt.Key.Key_Escape)
        qtbot.waitUntil(lambda: not menu.isVisible())
        menu = _open(qtbot, combo)
        assert combo.currentIndex() == START
        assert menu.activeAction() is _action(menu, START)
        assert menu.markedKeys() == [START]

    @pytest.mark.parametrize(
        ("key", "expected"), [(Qt.Key.Key_Up, 1), (Qt.Key.Key_Down, 2)], ids=["up", "down"]
    )
    def test_arrow_keys_change_the_value_while_closed(self, qtbot, key: Qt.Key, expected) -> None:
        """닫힌 칸에서 ↑는 한 줄 위로 값을 바꾸고 마지막 줄에서 ↓는 그대로여야 한다.

        지금 값 2(마지막 줄) → ↑ -> 1, ↓ -> 2. 메뉴는 열리지 않는다
        """
        _dialog, combo = _host(qtbot)
        QTest.keyClick(combo, key)
        _pump()
        assert combo.currentIndex() == expected
        assert not combo.menuWidget.isVisible()

    def test_typing_a_letter_moves_the_value_while_closed(self, qtbot) -> None:
        """닫힌 칸에서 글자를 치면 그 글자로 시작하는 줄로 값이 옮겨 가야 한다.

        지금 값 2(shutdown) → n -> 0(none)
        """
        _dialog, combo = _host(qtbot)
        QTest.keyClicks(combo, "n")
        _pump()
        assert combo.currentIndex() == 0


# ---- 고르기 시그널 ----


class TestChoosingSignals:
    """메뉴에서 고를 때 시그널 차례 · 횟수가 Qt 콤보박스 목록에서 고를 때와 같다."""

    def test_clicking_another_row_matches_the_qt_list(self, qtbot) -> None:
        """메뉴에서 다른 줄을 누르면 Qt 목록에서 같은 줄을 누를 때와 같은 시그널이 같은 차례로 나야 한다.

        지금 값 2 → 줄 0 누르기
        -> currentIndexChanged(0) · currentTextChanged(none) · activated(0) · textActivated(none)
           (Qt 콤보박스를 같은 테스트에서 재서 비교)
        """
        expected = _qt_list_pick(qtbot, 0)
        assert expected[0] == "currentIndexChanged(0)", "전제: Qt 기준이 값 바꿈부터 시작한다"
        _dialog, combo = _host(qtbot)
        seen = _record(combo)
        menu = _open(qtbot, combo)
        _click(menu, 0)
        qtbot.waitUntil(lambda: not menu.isVisible())
        _pump()
        assert seen == expected
        assert combo.currentIndex() == 0

    def test_clicking_the_current_row_matches_the_qt_list(self, qtbot) -> None:
        """메뉴에서 지금 값 줄을 누르면 Qt 목록과 같게 activated · textActivated만 나야 한다.

        지금 값 2 → 줄 2 누르기 -> activated(2) · textActivated(shutdown)
        """
        expected = _qt_list_pick(qtbot, START)
        assert expected == ["activated(2)", "textActivated(shutdown)"], "전제: Qt 기준"
        _dialog, combo = _host(qtbot)
        seen = _record(combo)
        menu = _open(qtbot, combo)
        _click(menu, START)
        qtbot.waitUntil(lambda: not menu.isVisible())
        _pump()
        assert seen == expected

    def test_enter_on_the_highlighted_row_chooses_it(self, qtbot) -> None:
        """펼친 메뉴에서 ↑ 뒤 Enter는 그 줄을 고르고 시그널을 한 번씩 내야 한다.

        지금 값 2 → ↑ → Enter
        -> currentIndexChanged(1) · currentTextChanged(sleep) · activated(1) · textActivated(sleep)
        """
        _dialog, combo = _host(qtbot)
        seen = _record(combo)
        menu = _open(qtbot, combo)
        QTest.keyClick(menu, Qt.Key.Key_Up)
        QTest.keyClick(menu, Qt.Key.Key_Return)
        qtbot.waitUntil(lambda: not menu.isVisible())
        _pump()
        assert seen == [
            "currentIndexChanged(1)",
            "currentTextChanged(sleep)",
            "activated(1)",
            "textActivated(sleep)",
        ]

    def test_enter_delivered_to_the_dialog_window_does_not_close_it(self, qtbot) -> None:
        """macOS 배달 순서(ShortcutOverride 뒤 같은 키 KeyPress를 대화상자 창에)로 Enter를 보내면
        줄만 고르고 대화상자는 열려 있어야 한다(#246).

        지금 값 2 → 펼침 → ↑ → 대화상자 창에 Shortcut · KeyPress · KeyRelease(Return)
        -> 값 1, 대화상자 보임, finished 없음
        """
        dialog, combo = _host(qtbot)
        finished: list[int] = []
        dialog.finished.connect(finished.append)
        menu = _open(qtbot, combo)
        QTest.keyClick(menu, Qt.Key.Key_Up)
        window = dialog.windowHandle()
        QTest.keyEvent(QTest.KeyAction.Shortcut, window, Qt.Key.Key_Return)
        QTest.keyPress(window, Qt.Key.Key_Return)
        QTest.keyRelease(window, Qt.Key.Key_Return)
        qtbot.waitUntil(lambda: not menu.isVisible())
        _pump()
        assert combo.currentIndex() == 1
        assert dialog.isVisible()
        assert finished == []


# ---- 휠 ----


def _wheel(widget: QWidget, steps: int) -> None:
    """위젯 가운데에 휠 이벤트를 보낸다 — 한 칸 120, 음수는 아래로."""
    center = QPointF(widget.rect().center())
    event = QWheelEvent(
        center,
        QPointF(widget.mapToGlobal(center.toPoint())),
        QPoint(0, 0),
        QPoint(0, 120 * steps),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(widget, event)


class TestWheel:
    """휠은 초점이 있을 때만 값을 바꾼다."""

    def test_wheel_without_focus_keeps_the_value(self, qtbot) -> None:
        """초점이 다른 위젯에 있으면 칸 위의 휠이 값을 바꾸지 않고 초점도 주지 않아야 한다.

        초점 = 다른 버튼, 지금 값 2 → 휠 위로 한 칸 -> 값 2, 칸 초점 없음
        """
        dialog, combo = _host(qtbot)
        dialog.otherButton.setFocus()
        _pump()
        assert not combo.hasFocus(), "전제: 칸에 초점이 없다"
        _wheel(combo, 1)
        _pump()
        assert combo.currentIndex() == START
        assert not combo.hasFocus()

    def test_wheel_with_focus_changes_the_value(self, qtbot) -> None:
        """초점이 있는 칸 위의 휠은 값을 바꿔야 한다.

        초점 = 칸, 지금 값 2 → 휠 위로 한 칸 -> 1
        """
        _dialog, combo = _host(qtbot)
        _wheel(combo, 1)
        _pump()
        assert combo.currentIndex() == 1


# ---- 창 떠남 ----


class TestWindowLeft:
    """칸이 든 창을 떠나면 열린 메뉴가 닫힌다."""

    def test_menu_closes_when_another_window_is_activated(self, qtbot) -> None:
        """메뉴가 열린 채 다른 창이 활성이 되면 메뉴가 닫히고 값은 그대로여야 한다.

        펼침 → 다른 최상위 창 활성 -> 메뉴 숨김, 값 2
        """
        _dialog, combo = _host(qtbot)
        menu = _open(qtbot, combo)
        other = QWidget()
        qtbot.addWidget(other)
        other.resize(120, 80)
        other.show()
        qtbot.waitExposed(other)
        other.activateWindow()
        qtbot.waitUntil(lambda: not menu.isVisible())
        assert combo.currentIndex() == START

    def test_menu_closes_when_the_window_is_minimized(self, qtbot) -> None:
        """메뉴가 열린 채 칸이 든 창이 최소화되면 메뉴가 닫혀야 한다.

        펼침 → 대화상자 showMinimized -> 메뉴 숨김
        """
        dialog, combo = _host(qtbot)
        menu = _open(qtbot, combo)
        dialog.showMinimized()
        qtbot.waitUntil(lambda: not menu.isVisible())
