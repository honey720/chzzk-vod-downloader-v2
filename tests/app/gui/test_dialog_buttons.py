"""대화상자 버튼 — Windows 11 표준 모양 · 주 동작 · 키보드 focus ring (#622).

설정 · 편집 · 구간 · 확인 창 안의 버튼을 실제 전역 QSS로 그려 화소로 잰다. 보조 버튼은 면 fieldBg · 테두리
border, 호버 · 눌림은 면만 한 단계씩, 주 동작은 강조색 채움이다. 강조색 테두리는 키보드로 받은 초점에만 있다.
"""

from collections.abc import Callable, Iterator

import pytest
from pytestqt.qtbot import QtBot
from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QMessageBox,
    QPushButton,
    QWidget,
)

import app.theme as theme
import app.widgets.view as view_module
import CVDv2 as main_module
from app.views.dialog import SettingDialog
from app.views.mainWindow import VodDownloader
from app.views.section_dialog import SectionDeleteButton
from tests.app.gui.card_helpers import hold_style
from tests.app.gui.test_section_editor import (  # noqa: F401 — 픽스처는 이름으로 쓴다
    _destroy_windows,
    _make_item,
    _no_network,
    _pump,
    basis,
    open_window,
)

SLACK = 24  # 색이 같다고 보는 한도 — 세 채널 차의 합(테두리 안티앨리어싱)
ACTIVE_WAIT_MS = 3000  # 창 활성 · 호버를 기다리는 한도(ms) — X11 · xvfb는 활성이 비동기로 온다
FACE_INSET = 3  # 면을 읽는 자리 — 왼쪽 테두리에서 안쪽으로(px), 글자에 닿지 않는다
# 테마마다 기대하는 호버 · 눌림 면 — 평소 면(fieldBg)에서 한 단계 · 두 단계(#622 오너 결정, 188 보고 3절 B)
STEPS = {"dark": ("surfaceHover", "surfacePressed"), "light": ("surfaceAlt", "surfaceHover")}
WINDOWS = ("setting", "edit", "section")  # 버튼 상자가 있는 창 — 확인 창은 따로 잰다


@pytest.fixture(params=["dark", "light"])
def scheme(qapp: QApplication, request: pytest.FixtureRequest) -> Iterator[str]:
    """앱 스타일 · 실제 QSS를 그 테마로 건다. 끝에 다크 · 빈 QSS로 되돌린다."""
    name = request.param
    theme.set_color_scheme(name)
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))
    yield name
    qapp.setStyleSheet("")
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())


def _is(colour: QColor, token: str) -> bool:
    want = QColor(theme.current_tokens()[token])
    gap = (
        abs(colour.red() - want.red())
        + abs(colour.green() - want.green())
        + abs(colour.blue() - want.blue())
    )
    return gap <= SLACK


def _rect(window: QWidget, widget: QWidget) -> QRect:
    return QRect(widget.mapTo(window, QPoint(0, 0)), widget.size())


def _pixel(image: QImage, x: int, y: int) -> QColor:
    ratio = image.devicePixelRatio()
    return image.pixelColor(int(x * ratio), int(y * ratio))


def _edge_and_face(button: QWidget) -> tuple[QColor, QColor]:
    """버튼 왼쪽 변 가운데의 가장자리 화소와 `FACE_INSET` 안쪽 면 화소(창을 그려서 읽는다)."""
    window = button.window()
    _pump()
    image = window.grab().toImage()
    rect = _rect(window, button)
    y = rect.center().y()
    return _pixel(image, rect.left(), y), _pixel(image, rect.left() + FACE_INSET, y)


def _activate(qtbot: QtBot, window: QWidget) -> None:
    def active() -> bool:
        if not window.isActiveWindow():
            window.activateWindow()
            return False
        return True

    qtbot.waitUntil(active, timeout=ACTIVE_WAIT_MS)
    _pump()


def _key_focus(qtbot: QtBot, button: QWidget) -> None:
    """버튼에 키보드(Tab) 까닭으로 초점을 준다."""
    _activate(qtbot, button.window())
    button.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(button.hasFocus, timeout=ACTIVE_WAIT_MS)
    _pump()
    assert button.property("keyFocus") is True, "전제: 키보드 초점으로 적혔다"


def _park(window: QWidget) -> None:
    """포인터를 창 왼쪽 위로 옮긴다(합성 이동 — 실제 커서는 그대로). 두 자리를 지나 실제 이동을 만든다."""
    handle = window.windowHandle()
    QTest.mouseMove(handle, QPoint(3, 3))
    QTest.mouseMove(handle, QPoint(1, 1))
    _pump()


def _hover(qtbot: QtBot, button: QWidget) -> None:
    window = button.window()
    _activate(qtbot, window)
    _park(window)
    QTest.mouseMove(window.windowHandle(), _rect(window, button).center())
    qtbot.waitUntil(button.underMouse, timeout=ACTIVE_WAIT_MS)
    _pump()


def _press_and_hold(qtbot: QtBot, button: QWidget) -> Callable[[], None]:
    """버튼 위에서 왼쪽 단추를 누른 채 둔다 — 놓는 함수를 돌려준다(버튼 밖에서 놓아 누름이 일어나지 않는다)."""
    window = button.window()
    _hover(qtbot, button)
    QTest.mousePress(
        window.windowHandle(), Qt.MouseButton.LeftButton, pos=_rect(window, button).center()
    )
    _pump()
    assert button.isDown(), "전제: 눌린 채다"

    def release() -> None:
        QTest.mouseRelease(window.windowHandle(), Qt.MouseButton.LeftButton, pos=QPoint(1, 1))
        _pump()

    return release


def _setting(qtbot: QtBot) -> SettingDialog:
    dialog = SettingDialog()
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)
    _activate(qtbot, dialog)
    return dialog


def _edit(qtbot: QtBot, win: VodDownloader, item) -> QDialog:
    win.onCardEdit(item)
    _pump()
    dialog = win._editDialog
    qtbot.waitExposed(dialog)
    _activate(qtbot, dialog)
    return dialog


def _section(qtbot: QtBot, win: VodDownloader, item) -> QDialog:
    edit = _edit(qtbot, win, item)
    edit.openSections()
    _pump()
    dialog = edit.sectionDialog
    qtbot.waitExposed(dialog)
    qtbot.waitUntil(lambda: dialog.viewModel().state != "loading", timeout=ACTIVE_WAIT_MS)
    dialog.addButton.click()  # 행 둘 — 행 ✕가 켜진다
    _pump()
    _activate(qtbot, dialog)
    return dialog


@pytest.fixture
def opened(
    qtbot: QtBot,
    tmp_path,
    basis: type,  # noqa: F811 — 픽스처
    scheme: str,
) -> Iterator[Callable[[str], tuple[QDialog, QPushButton, QPushButton]]]:
    """창 이름 → (창, 보조 버튼 Cancel, 주 동작 버튼). 메인 창 · 대기 카드 하나를 붙잡아 둔다."""
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    held: list[QWidget] = [win]  # 붙잡는다 — addWidget은 창을 붙잡지 않는다

    def open_(name: str) -> tuple[QDialog, QPushButton, QPushButton]:
        if name == "setting":
            dialog = _setting(qtbot)
            box = dialog.dialogButtonBox
            pair = (
                box.button(QDialogButtonBox.StandardButton.Cancel),
                box.button(QDialogButtonBox.StandardButton.Ok),
            )
        elif name == "edit":
            dialog = _edit(qtbot, win, item)
            pair = (dialog.cancelButton, dialog.saveButton)
        else:
            dialog = _section(qtbot, win, item)
            pair = (dialog.cancelButton, dialog.okButton)
        held.append(dialog)
        return (dialog, *pair)

    yield open_
    held.clear()


class TestTokens:
    """대화상자 버튼 호버 · 눌림 토큰은 평소 면에서 한 단계 · 두 단계다(#622 B)."""

    @pytest.mark.parametrize("name", ["dark", "light"])
    def test_hover_and_pressed_tokens_are_the_chosen_steps(self, name: str) -> None:
        """두 테마의 dialogButtonHover · dialogButtonPressed가 고른 단계 토큰과 같아야 한다.

        다크 -> surfaceHover · surfacePressed, 라이트 -> surfaceAlt · surfaceHover
        """
        table = {"dark": theme.DARK, "light": theme.LIGHT}[name]
        hover, pressed = STEPS[name]
        assert table["dialogButtonHover"] == table[hover]
        assert table["dialogButtonPressed"] == table[pressed]


@pytest.mark.parametrize("name", WINDOWS)
class TestSecondaryButton:
    """보조 버튼(Cancel)은 면 fieldBg · 테두리 border, 호버 · 눌림은 면만 한 단계씩이다."""

    def test_idle_face_is_the_field_face_with_a_border(
        self, opened, scheme: str, name: str
    ) -> None:
        """평소 Cancel의 면은 fieldBg, 가장자리는 border여야 한다.

        창을 열고 포인터는 창 왼쪽 위, 초점은 다른 곳 -> 왼쪽 변 가운데 가장자리 ≈ border, 3px 안 ≈ fieldBg
        """
        dialog, cancel, _primary = opened(name)
        _park(dialog)
        assert not cancel.hasFocus() and not cancel.underMouse(), "전제: 초점 · 포인터 없음"
        edge, face = _edge_and_face(cancel)
        assert _is(face, "fieldBg"), f"{scheme} {name}: 면 {face.name()}"
        assert _is(edge, "border"), f"{scheme} {name}: 가장자리 {edge.name()}"

    def test_hover_steps_the_face_and_keeps_the_border(
        self, opened, qtbot: QtBot, scheme: str, name: str
    ) -> None:
        """포인터가 오른 Cancel의 면은 dialogButtonHover, 가장자리는 border여야 한다.

        Cancel 위로 합성 이동 -> 면 ≈ dialogButtonHover, 가장자리 ≈ border
        """
        _dialog, cancel, _primary = opened(name)
        _hover(qtbot, cancel)
        edge, face = _edge_and_face(cancel)
        assert _is(face, "dialogButtonHover"), f"{scheme} {name}: 면 {face.name()}"
        assert _is(edge, "border"), f"{scheme} {name}: 가장자리 {edge.name()}"

    def test_mouse_press_steps_the_face_twice_without_a_focus_ring(
        self, opened, qtbot: QtBot, scheme: str, name: str
    ) -> None:
        """마우스로 누른 Cancel의 면은 dialogButtonPressed, 가장자리는 강조색이 아닌 border여야 한다.

        Cancel 위에서 왼쪽 단추 누름(놓지 않음) -> 초점 있음 · keyFocus False, 면 ≈ dialogButtonPressed,
        가장자리 ≈ border
        """
        _dialog, cancel, _primary = opened(name)
        release = _press_and_hold(qtbot, cancel)
        try:
            assert cancel.hasFocus(), "전제: 누르면 초점을 받는다"
            assert cancel.property("keyFocus") is False
            edge, face = _edge_and_face(cancel)
        finally:
            release()
        assert _is(face, "dialogButtonPressed"), f"{scheme} {name}: 면 {face.name()}"
        assert _is(edge, "border"), f"{scheme} {name}: 가장자리 {edge.name()}"

    def test_keyboard_focus_draws_an_accent_edge(
        self, opened, qtbot: QtBot, scheme: str, name: str
    ) -> None:
        """키보드 초점을 받은 Cancel의 가장자리는 accent, 면은 fieldBg 그대로여야 하고 크기는 그대로여야 한다.

        Cancel에 Tab 까닭 초점 -> 가장자리 ≈ accent, 면 ≈ fieldBg, 초점 전후 크기 같음
        """
        dialog, cancel, _primary = opened(name)
        _park(dialog)
        size = cancel.size()
        _key_focus(qtbot, cancel)
        edge, face = _edge_and_face(cancel)
        assert _is(edge, "accent"), f"{scheme} {name}: 가장자리 {edge.name()}"
        assert _is(face, "fieldBg"), f"{scheme} {name}: 면 {face.name()}"
        assert cancel.size() == size


@pytest.mark.parametrize("name", WINDOWS)
class TestPrimaryButton:
    """주 동작(설정 OK · 편집 Save · 구간 OK)은 강조색 채움, 키보드 초점은 focusOnAccent 테두리다."""

    def test_primary_is_the_accent_fill(self, opened, scheme: str, name: str) -> None:
        """주 동작 버튼은 role primary이고 면 · 가장자리가 accent여야 한다.

        창을 열고 포인터는 창 왼쪽 위 -> role == "primary", 면 ≈ accent, 가장자리 ≈ accent
        """
        dialog, _cancel, primary = opened(name)
        _park(dialog)
        assert primary.property("role") == "primary"
        edge, face = _edge_and_face(primary)
        assert _is(face, "accent"), f"{scheme} {name}: 면 {face.name()}"
        assert _is(edge, "accent"), f"{scheme} {name}: 가장자리 {edge.name()}"

    def test_keyboard_focus_draws_the_on_accent_edge(
        self, opened, qtbot: QtBot, scheme: str, name: str
    ) -> None:
        """키보드 초점을 받은 주 동작 버튼의 가장자리는 focusOnAccent, 면은 accent여야 한다.

        주 동작 버튼에 Tab 까닭 초점 -> 가장자리 ≈ focusOnAccent, 면 ≈ accent
        """
        dialog, _cancel, primary = opened(name)
        _park(dialog)
        _key_focus(qtbot, primary)
        edge, face = _edge_and_face(primary)
        assert _is(edge, "focusOnAccent"), f"{scheme} {name}: 가장자리 {edge.name()}"
        assert _is(face, "accent"), f"{scheme} {name}: 면 {face.name()}"

    def test_mouse_focus_keeps_the_accent_edge(
        self, opened, qtbot: QtBot, scheme: str, name: str
    ) -> None:
        """마우스로 누른 주 동작 버튼의 가장자리는 focusOnAccent가 아니어야 한다.

        주 동작 버튼 위에서 왼쪽 단추 누름(놓지 않음) -> keyFocus False, 가장자리 ≈ accentPressed
        """
        _dialog, _cancel, primary = opened(name)
        release = _press_and_hold(qtbot, primary)
        try:
            assert primary.property("keyFocus") is False
            edge, _face = _edge_and_face(primary)
        finally:
            release()
        assert _is(edge, "accentPressed"), f"{scheme} {name}: 가장자리 {edge.name()}"


class TestEveryButtonIsWatched:
    """네 창의 모든 버튼에 키보드 초점 표시가 걸려 있다 — 빠진 버튼은 Tab 초점에 focus ring이 없다."""

    @pytest.mark.parametrize("name", WINDOWS)
    def test_every_push_button_carries_the_key_focus_mark(
        self, opened, scheme: str, name: str
    ) -> None:
        """창 안의 QPushButton마다 keyFocus 속성이 있어야 한다(구간 창은 행을 더해 다시 만든 뒤).

        창을 연다(구간 창은 + 구간 추가로 행 둘) -> 모든 QPushButton의 keyFocus가 None이 아님
        """
        dialog, _cancel, _primary = opened(name)
        buttons = dialog.findChildren(QPushButton)
        assert buttons, "전제: 버튼이 있다"
        missing = [b.objectName() or b.text() for b in buttons if b.property("keyFocus") is None]
        assert missing == []


class TestSectionRowDelete:
    """구간 창 행 ✕는 상자(면 fieldBg · 테두리 border)이고 키보드 초점에 강조색 테두리다."""

    def test_row_delete_is_a_field_box(self, opened, scheme: str) -> None:
        """행 ✕의 면은 fieldBg, 가장자리는 border여야 한다.

        구간 창에 행 둘 -> 첫 행 ✕ 면 ≈ fieldBg, 가장자리 ≈ border
        """
        dialog, _cancel, _primary = opened("section")
        _park(dialog)
        delete = [b for b in dialog.findChildren(SectionDeleteButton) if b.isVisible()][0]
        edge, face = _edge_and_face(delete)
        assert _is(face, "fieldBg"), f"{scheme}: 면 {face.name()}"
        assert _is(edge, "border"), f"{scheme}: 가장자리 {edge.name()}"

    def test_row_delete_keyboard_focus_draws_an_accent_edge(
        self, opened, qtbot: QtBot, scheme: str
    ) -> None:
        """키보드 초점을 받은 행 ✕의 가장자리는 accent여야 한다.

        구간 창에 행 둘, 첫 행 ✕에 Tab 까닭 초점 -> 가장자리 ≈ accent
        """
        dialog, _cancel, _primary = opened("section")
        _park(dialog)
        delete = [b for b in dialog.findChildren(SectionDeleteButton) if b.isVisible()][0]
        _key_focus(qtbot, delete)
        edge, _face = _edge_and_face(delete)
        assert _is(edge, "accent"), f"{scheme}: 가장자리 {edge.name()}"


class TestSubtleButton:
    """편집 창 「기본값」(테두리 없는 부차 버튼)도 키보드 초점에 강조색 테두리가 있고 크기는 그대로다."""

    def test_keyboard_focus_draws_an_accent_edge_and_keeps_the_size(
        self, opened, qtbot: QtBot, scheme: str
    ) -> None:
        """키보드 초점을 받은 「기본값」의 가장자리는 accent여야 하고 크기는 그대로여야 한다.

        편집 창의 「기본값」에 Tab 까닭 초점 -> 가장자리 ≈ accent, 초점 전후 크기 같음
        """
        dialog, _cancel, _primary = opened("edit")
        _park(dialog)
        button = dialog.defaultNameButton
        size = button.size()
        edge_before, _face = _edge_and_face(button)
        assert not _is(edge_before, "accent"), "전제: 초점 전 테두리 없음"
        _key_focus(qtbot, button)
        edge, _face = _edge_and_face(button)
        assert _is(edge, "accent"), f"{scheme}: 가장자리 {edge.name()}"
        assert button.size() == size


class TestConfirmBox:
    """확인 창(「Stop and delete」 · Cancel)은 두 버튼 모두 보조 버튼이다 — 기본 · Esc는 Cancel 그대로."""

    def _ask(self, check: Callable[[QMessageBox], None]) -> None:
        """실제 `confirm_stop_and_delete`를 띄우고, 뜬 동안 `check(box)`를 부른 뒤 Esc로 닫는다."""
        failures: list[BaseException] = []

        def run() -> None:
            box = QApplication.activeModalWidget()
            if not isinstance(box, QMessageBox):
                QTimer.singleShot(10, run)
                return
            try:
                check(box)
            except BaseException as error:  # 실패를 exec 밖으로 옮긴다
                failures.append(error)
            QTest.keyClick(box, Qt.Key.Key_Escape)

        QTimer.singleShot(0, run)
        assert view_module.confirm_stop_and_delete(None) is False
        if failures:
            raise failures[0]

    @staticmethod
    def _buttons(box: QMessageBox) -> tuple[QPushButton, QPushButton]:
        stop = next(
            b for b in box.buttons() if box.buttonRole(b) == QMessageBox.ButtonRole.DestructiveRole
        )
        return stop, box.button(QMessageBox.StandardButton.Cancel)

    def test_both_buttons_are_secondary(self, qtbot: QtBot, scheme: str) -> None:
        """두 버튼 모두 role이 없고 면 fieldBg · 가장자리 border여야 하며 기본 버튼은 Cancel이어야 한다.

        확인 창, 포인터는 창 왼쪽 위 -> 두 버튼 role None, 면 ≈ fieldBg, 가장자리 ≈ border, 기본 = Esc = Cancel
        """
        seen: dict[str, object] = {}

        def check(box: QMessageBox) -> None:
            _activate(qtbot, box)
            _park(box)
            stop, cancel = self._buttons(box)
            seen["default"] = box.defaultButton() is cancel
            seen["escape"] = box.escapeButton() is cancel
            for label, button in (("stop", stop), ("cancel", cancel)):
                seen[f"{label} role"] = button.property("role")
                edge, face = _edge_and_face(button)
                seen[f"{label} face"] = _is(face, "fieldBg") or face.name()
                seen[f"{label} edge"] = _is(edge, "border") or edge.name()

        self._ask(check)
        assert seen == {
            "default": True,
            "escape": True,
            "stop role": None,
            "stop face": True,
            "stop edge": True,
            "cancel role": None,
            "cancel face": True,
            "cancel edge": True,
        }, scheme

    def test_opening_focus_on_cancel_draws_no_ring(self, qtbot: QtBot, scheme: str) -> None:
        """막 연 확인 창에서 초점을 쥔 Cancel은 키보드 초점이 아니고 가장자리가 border여야 한다.

        확인 창을 연 그대로(키 누름 없음) -> Cancel 초점 있음, keyFocus False, 가장자리 ≈ border
        """
        seen: dict[str, object] = {}

        def check(box: QMessageBox) -> None:
            _activate(qtbot, box)
            _stop, cancel = self._buttons(box)
            seen["focus"] = cancel.hasFocus()
            seen["keyFocus"] = cancel.property("keyFocus")
            edge, _face = _edge_and_face(cancel)
            seen["edge"] = _is(edge, "border") or edge.name()

        self._ask(check)
        assert seen == {"focus": True, "keyFocus": False, "edge": True}, scheme

    def test_keyboard_focus_on_stop_draws_an_accent_edge(self, qtbot: QtBot, scheme: str) -> None:
        """키보드 초점을 받은 「Stop and delete」의 가장자리는 accent, 면은 fieldBg여야 한다.

        확인 창, 「Stop and delete」에 Tab 까닭 초점 -> 가장자리 ≈ accent, 면 ≈ fieldBg
        """
        seen: dict[str, object] = {}

        def check(box: QMessageBox) -> None:
            _park(box)
            stop, _cancel = self._buttons(box)
            _key_focus(qtbot, stop)
            edge, face = _edge_and_face(stop)
            seen["edge"] = _is(edge, "accent") or edge.name()
            seen["face"] = _is(face, "fieldBg") or face.name()

        self._ask(check)
        assert seen == {"edge": True, "face": True}, scheme
