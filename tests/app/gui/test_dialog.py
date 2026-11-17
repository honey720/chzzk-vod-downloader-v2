"""설정 창 — 로그 폴더 열기 3-OS 동작(#181) · 두 선택 칸의 메뉴 펼침 (#620).

os.startfile은 Windows 전용이라 macOS·Linux에서 클릭 시 무조건
AttributeError였다. QDesktopServices.openUrl로 바꿔 3-OS 공통 동작을
보장한다.
"""

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from pytestqt.qtbot import QtBot
from PySide6.QtCore import QEvent, QObject, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox

import app.theme as theme
import CVDv2 as main_module
from app.system import settings
from app.views.dialog import SettingDialog
from app.widgets.choice_combo import ChoiceCombo
from app.widgets.header_controls import ChoiceMenu
from tests.app.gui.card_helpers import hold_style


def test_open_logs_folder_uses_desktop_services(qapp) -> None:
    dialog = SettingDialog()
    with patch("app.views.dialog.QDesktopServices.openUrl", return_value=True) as m:
        dialog.openLogsFolder()

    assert m.call_count == 1
    (url,), _ = m.call_args
    assert url.isLocalFile()
    assert url.toLocalFile().endswith("logs")


def test_open_logs_folder_warns_on_failure(qapp) -> None:
    dialog = SettingDialog()
    with patch("app.views.dialog.QDesktopServices.openUrl", return_value=False):
        with patch("app.views.dialog.QMessageBox.warning") as warn:
            dialog.openLogsFolder()

    assert warn.called


ACTIVE_WAIT_MS = 3000  # 창 활성을 기다리는 한도(ms) — X11 · xvfb는 활성이 비동기로 온다
SAVED = {
    "afterDownload": "shutdown",
    "language": "ko_KR",
}  # 창을 열 때의 값 — 두 칸 모두 맨 위가 아니다


@pytest.fixture
def _app_style(qapp: QApplication, request: pytest.FixtureRequest) -> Iterator[None]:
    """앱 스타일(`build_style`) · 실제 QSS(다크). 끝에 Fusion 기본 · 빈 QSS로 되돌린다.

    `deferred_popup_hide`는 매개변수로 받는다(없으면 끔) — macOS 기본값(켬)에서도 재기 위해서다.
    """
    deferred = getattr(request, "param", False)
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style(deferred_popup_hide=deferred)))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))
    yield
    qapp.setStyleSheet("")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())


def _shown_dialog(qtbot: QtBot) -> SettingDialog:
    """저장 값 `SAVED`로 연 설정 창 — 보이고 활성이다."""
    settings.update_settings(SAVED)
    dialog = SettingDialog()
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)
    dialog.activateWindow()
    qtbot.waitUntil(dialog.isActiveWindow, timeout=ACTIVE_WAIT_MS)
    return dialog


def _combo(dialog: SettingDialog, name: str) -> ChoiceCombo:
    combo = getattr(dialog, name)
    assert isinstance(combo, ChoiceCombo), "전제: 설정 창의 칸이 ChoiceCombo다"
    assert combo.currentData() == SAVED[name], "전제: 저장 값으로 열렸다"
    return combo


def _open(qtbot: QtBot, combo: ChoiceCombo) -> ChoiceMenu:
    """포인터를 창 구석으로 옮긴 뒤(합성 이동 — 실제 커서는 그대로) 메뉴를 연다.

    `QMenu`는 열릴 때 포인터가 있던 자리에서 움직이지 않은 이동 · 놓기를 무시한다(test_choice_combo 참고).
    """
    QTest.mouseMove(combo.window().windowHandle(), QPoint(1, 1))
    QApplication.processEvents()
    combo.showPopup()
    menu = combo.menuWidget
    qtbot.waitUntil(menu.isVisible)
    return menu


def _close(qtbot: QtBot, menu: ChoiceMenu) -> None:
    QTest.keyClick(menu, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: not menu.isVisible())


def _row_point(menu: ChoiceMenu, row: int) -> QPoint:
    action = next(a for a in menu.choices() if a.data() == row)
    return menu.actionGeometry(action).center()


def _hover(menu: ChoiceMenu, row: int) -> None:
    """메뉴 창에 합성 포인터 이동을 보낸다 — 왼쪽 위를 먼저 지나 실제 이동을 만든다."""
    window = menu.windowHandle()
    QTest.mouseMove(window, QPoint(2, 2))
    QTest.mouseMove(window, _row_point(menu, row))
    QApplication.processEvents()


def _active_row(menu: ChoiceMenu) -> int | None:
    action = menu.activeAction()
    return None if action is None else action.data()


COMBOS = ("afterDownload", "language")  # 설정 창의 두 선택 칸


@pytest.mark.usefixtures("_app_style")
@pytest.mark.parametrize("name", COMBOS)
class TestSettingCombosUseTheChoiceMenu:
    """설정 창의 두 칸은 `ChoiceCombo` 메뉴로 펼친다 — #241 후속 · #246의 목표를 실제 설정 창에서 잰다 (#620)."""

    def test_menu_opens_marked_and_highlighted_on_the_saved_value(
        self, qtbot: QtBot, name: str
    ) -> None:
        """칸을 펼치면 저장 값 줄에 ✓와 강조가 있어야 한다.

        저장 값 shutdown(2) · ko_KR(1) -> 메뉴 보임, ✓ = 강조 = 저장 값 줄
        """
        dialog = _shown_dialog(qtbot)  # 붙잡는다 — addWidget은 창을 붙잡지 않는다
        combo = _combo(dialog, name)
        menu = _open(qtbot, combo)
        assert menu.markedKeys() == [combo.currentIndex()]
        assert _active_row(menu) == combo.currentIndex()

    def test_highlight_is_on_the_saved_value_when_the_menu_shows(
        self, qtbot: QtBot, name: str
    ) -> None:
        """호버한 줄이 있던 뒤 다시 펼치면 메뉴가 보이는 순간(Show) 이미 강조가 저장 값 줄에 있어야 한다.

        펼침 → 맨 위 줄 호버 → Esc → 다시 펼침 -> Show 이벤트 때 강조 = 저장 값 줄
        """
        dialog = _shown_dialog(qtbot)  # 붙잡는다 — addWidget은 창을 붙잡지 않는다
        combo = _combo(dialog, name)
        menu = _open(qtbot, combo)
        _hover(menu, 0)
        assert _active_row(menu) == 0, "전제: 호버가 강조를 옮겼다"
        _close(qtbot, menu)
        seen: list[int | None] = []
        probe = _ShowProbe(menu, seen)
        menu.installEventFilter(probe)
        _open(qtbot, combo)
        menu.removeEventFilter(probe)
        assert seen == [combo.currentIndex()]

    def test_hover_then_escape_keeps_the_saved_value(self, qtbot: QtBot, name: str) -> None:
        """펼친 채 다른 줄을 호버하고 Esc로 닫으면 값은 저장 값 그대로여야 한다.

        펼침 → 맨 위 줄 호버 → Esc -> currentData == 저장 값, 다시 펼친 강조 = 저장 값 줄
        """
        dialog = _shown_dialog(qtbot)  # 붙잡는다 — addWidget은 창을 붙잡지 않는다
        combo = _combo(dialog, name)
        menu = _open(qtbot, combo)
        _hover(menu, 0)
        _close(qtbot, menu)
        assert combo.currentData() == SAVED[name]
        _open(qtbot, combo)
        assert _active_row(menu) == combo.currentIndex()

    def test_clicking_a_row_sets_the_value_and_the_next_menu_starts_there(
        self, qtbot: QtBot, name: str
    ) -> None:
        """메뉴에서 맨 위 줄을 누르면 값이 그 줄이 되고, 다시 펼친 강조 · ✓가 그 줄이어야 한다.

        펼침 → 맨 위 줄 누르기 -> currentIndex 0, 다시 펼침 -> ✓ = 강조 = 0
        """
        dialog = _shown_dialog(qtbot)  # 붙잡는다 — addWidget은 창을 붙잡지 않는다
        combo = _combo(dialog, name)
        menu = _open(qtbot, combo)
        _hover(menu, 0)
        window, point = menu.windowHandle(), _row_point(menu, 0)
        QTest.mousePress(window, Qt.MouseButton.LeftButton, pos=point)
        QApplication.processEvents()
        QTest.mouseRelease(window, Qt.MouseButton.LeftButton, pos=point)
        qtbot.waitUntil(lambda: not menu.isVisible())
        assert combo.currentIndex() == 0
        _open(qtbot, combo)
        assert menu.markedKeys() == [0]
        assert _active_row(menu) == 0

    @pytest.mark.parametrize("_app_style", [False, True], ids=["now", "deferred"], indirect=True)
    def test_enter_delivered_the_macos_way_keeps_the_dialog_open(
        self, qtbot: QtBot, name: str
    ) -> None:
        """macOS 배달 순서로 Enter를 보내면 강조한 줄만 고르고 설정 창은 열려 있어야 한다(#246).

        지연 닫기 힌트 끔 · 켬 각각, 펼침 → 맨 위 줄로 강조 → 대화상자 창에 Shortcut · KeyPress · KeyRelease(Return)
        -> currentIndex 0, 창 보임, finished 없음
        """
        dialog = _shown_dialog(qtbot)
        combo = _combo(dialog, name)
        finished: list[int] = []
        dialog.finished.connect(finished.append)
        combo.setFocus()
        menu = _open(qtbot, combo)
        for _ in range(combo.currentIndex()):
            QTest.keyClick(menu, Qt.Key.Key_Up)
        assert _active_row(menu) == 0, "전제: 강조가 맨 위 줄이다"
        window = dialog.windowHandle()
        QTest.keyEvent(QTest.KeyAction.Shortcut, window, Qt.Key.Key_Return)
        QTest.keyPress(window, Qt.Key.Key_Return)
        QTest.keyRelease(window, Qt.Key.Key_Return)
        qtbot.waitUntil(lambda: not menu.isVisible())
        QApplication.processEvents()
        assert combo.currentIndex() == 0
        assert dialog.isVisible()
        assert finished == []

    def test_ok_writes_the_value_chosen_in_the_menu(self, qtbot: QtBot, name: str) -> None:
        """메뉴에서 고른 뒤 OK를 누르면 그 값이 저장돼야 한다(#519).

        펼침 → 맨 위 줄 Enter → OK -> load_setting(name) == 맨 위 줄 값
        """
        dialog = _shown_dialog(qtbot)
        combo = _combo(dialog, name)
        menu = _open(qtbot, combo)
        for _ in range(combo.currentIndex()):
            QTest.keyClick(menu, Qt.Key.Key_Up)
        QTest.keyClick(menu, Qt.Key.Key_Return)
        qtbot.waitUntil(lambda: not menu.isVisible())
        dialog.dialogButtonBox.button(QDialogButtonBox.StandardButton.Ok).click()
        assert settings.load_setting(name) == combo.itemData(0)
        assert combo.itemData(0) != SAVED[name], "전제: 다른 값을 골랐다"

    def test_cancel_writes_nothing_after_choosing_in_the_menu(
        self, qtbot: QtBot, name: str
    ) -> None:
        """메뉴에서 고른 뒤 Cancel을 누르면 저장 값이 그대로여야 한다(#519).

        펼침 → 맨 위 줄 Enter → Cancel -> load_setting(name) == 저장 값
        """
        dialog = _shown_dialog(qtbot)
        combo = _combo(dialog, name)
        menu = _open(qtbot, combo)
        for _ in range(combo.currentIndex()):
            QTest.keyClick(menu, Qt.Key.Key_Up)
        QTest.keyClick(menu, Qt.Key.Key_Return)
        qtbot.waitUntil(lambda: not menu.isVisible())
        assert combo.currentIndex() == 0, "전제: 창 안의 값은 바뀌었다"
        dialog.dialogButtonBox.button(QDialogButtonBox.StandardButton.Cancel).click()
        assert settings.load_setting(name) == SAVED[name]


class _ShowProbe(QObject):
    """메뉴의 Show 이벤트 때 강조한 줄을 적는다."""

    def __init__(self, menu: ChoiceMenu, seen: list[int | None]) -> None:
        super().__init__(menu)
        self._menu = menu
        self._seen = seen

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._menu and event.type() == QEvent.Type.Show:
            self._seen.append(_active_row(self._menu))
        return False
