"""Tab 키 초점 때 버튼 안에 Fusion 초점 사각형이 따로 그려지지 않는다.

Fusion은 실제 Tab 키로 초점이 옮겨졌을 때만(창의 WA_KeyboardFocusChange) 버튼 글 둘레에 자기 초점 사각형을
그린다. 앱의 키보드 초점 표시는 버튼마다 따로 있으므로 이 사각형은 기본 버튼 규칙의 `outline: none`으로 끈다.
`setFocus`로는 이 속성이 서지 않아 사각형이 그려지지 않는다 — 그래서 모든 경우를 실제 Tab 키 이벤트로 옮긴다.
"""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot
from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QDialogButtonBox,
    QFrame,
    QMessageBox,
    QPushButton,
    QWidget,
)

import app.theme as theme
import app.widgets.view as view_module
from app.views.mainWindow import VodDownloader
from tests.app.gui.card_helpers import tab_reaches_all_controls
from tests.app.gui.test_dialog_buttons import (  # noqa: F401 — 픽스처는 이름으로 쓴다
    _activate,
    _edit,
    _section,
    _setting,
    scheme,
)
from tests.app.gui.test_section_editor import (  # noqa: F401 — 픽스처는 이름으로 쓴다
    _destroy_windows,
    _make_item,
    _no_network,
    _pump,
    basis,
    open_window,
)

SLACK = 24  # 색이 같다고 보는 한도 — 세 채널 차의 합
WAIT_MS = 3000  # 초점 · 활성을 기다리는 한도(ms) — X11 · xvfb는 활성이 비동기로 온다
WIDE = 900  # 메인 창을 최소폭보다 넓히는 값(px) — 바 버튼이 모두 보이는 폭
TAB_PRESSES = 30  # target에 닿을 때까지 누르는 Tab 한도 — 창 하나의 Tab 순서보다 넉넉하다
SELECT_BAR_EDGE = 3  # 선택 줄 버튼의 가장자리 자리(px) — 바깥 여백 2px + 테두리 1px(style.qss)

# 경우 이름 -> (버튼을 찾는 함수, 가장자리 두께 px)
MAIN_CASES: dict[str, tuple[Callable[[VodDownloader], QWidget], int]] = {
    "settingsGear": (lambda win: win.settingsButton, 1),
    "selectOff": (lambda win: win.selectButton, 1),
    "download": (lambda win: win.downloadButton, 1),
    "selectBarCancel": (lambda win: win.selectBar.cancelButton, SELECT_BAR_EDGE),
}
LIFTED_EDGE = 2  # 고른 탭의 가장자리 자리(px) — 강조색 1px + 간격 1px(paint_lifted_ring)
# 경우 이름 -> (창 이름, 버튼을 찾는 함수, 가장자리 두께 px)
DIALOG_CASES: dict[str, tuple[str, Callable[[QWidget], QWidget], int]] = {
    "settingTab": ("setting", lambda d: _checked_tab(d), LIFTED_EDGE),
    "settingCancel": ("setting", lambda d: _box_button(d, "Cancel"), 1),
    "settingSave": ("setting", lambda d: _box_button(d, "Save"), 1),
    "editDefaultName": ("edit", lambda d: d.defaultNameButton, 1),
    "editSections": ("edit", lambda d: d.sectionButton, 1),
    "editBrowse": ("edit", lambda d: d.browseButton, 1),
    "editDefaultPath": ("edit", lambda d: d.defaultPathButton, 1),
    "editSave": ("edit", lambda d: d.saveButton, 1),
    "editCancel": ("edit", lambda d: d.cancelButton, 1),
    "sectionRowDelete": ("section", lambda d: _shown_row_delete(d), 1),
    "sectionAdd": ("section", lambda d: d.addButton, 1),
    "sectionOk": ("section", lambda d: d.okButton, 1),
    "sectionCancel": ("section", lambda d: d.cancelButton, 1),
}
CONFIRM_CASES = ("stop", "cancel")  # 확인 창의 「Stop and delete」 · Cancel


@pytest.fixture(autouse=True)
def _tab_rule() -> Iterator[None]:
    """Tab이 모든 컨트롤에 가는 규칙을 세운다 — 선택 줄 ✕ · 구간 행 ✕(TabFocus)에 Tab이 닿는다는 전제 (#653)."""
    with tab_reaches_all_controls():
        yield


def _box_button(dialog: QWidget, name: str) -> QWidget:
    return dialog.dialogButtonBox.button(getattr(QDialogButtonBox.StandardButton, name))


def _shown_row_delete(dialog: QWidget) -> QWidget:
    rows = dialog.findChildren(QPushButton, "sectionDeleteButton")
    return next(b for b in rows if b.isVisible())


def _checked_tab(dialog: QWidget) -> QWidget:
    strip = dialog.findChild(QFrame, "settingsTabs")
    return next(b for b in strip.findChildren(QPushButton) if b.isChecked())


def _rect(window: QWidget, widget: QWidget) -> QRect:
    return QRect(widget.mapTo(window, QPoint(0, 0)), widget.size())


def _pixel(image: QImage, x: int, y: int) -> QColor:
    ratio = image.devicePixelRatio()
    return image.pixelColor(int(x * ratio), int(y * ratio))


def _distance(a: QColor, b: QColor) -> int:
    return abs(a.red() - b.red()) + abs(a.green() - b.green()) + abs(a.blue() - b.blue())


def _inner_points(rect: QRect, edge: int) -> list[tuple[int, int]]:
    """가장자리 `edge` px와 모서리 반경 자리를 뺀 안쪽 화소(창 좌표) — 가로 띠와 세로 띠를 합친 십자꼴."""
    tokens = theme.current_tokens()
    radius = max(int(tokens[name].removesuffix("px")) for name in ("radius", "controlRadius"))
    corner = edge + radius
    left, top, right, bottom = rect.left(), rect.top(), rect.right(), rect.bottom()
    points = {
        (x, y)
        for x in range(left + corner, right - corner + 1)
        for y in range(top + edge, bottom - edge + 1)
    }
    points |= {
        (x, y)
        for x in range(left + edge, right - edge + 1)
        for y in range(top + corner, bottom - corner + 1)
    }
    return sorted(points)


def _changed(
    first: QImage, second: QImage, points: list[tuple[int, int]], origin: QPoint
) -> list[tuple[int, int]]:
    return [
        (x - origin.x(), y - origin.y())
        for x, y in points
        if _distance(_pixel(first, x, y), _pixel(second, x, y)) > SLACK
    ]


def _park(window: QWidget) -> None:
    """포인터를 창 왼쪽 위로 옮긴다(합성 이동 — 실제 커서는 그대로)."""
    handle = window.windowHandle()
    QTest.mouseMove(handle, QPoint(3, 3))
    QTest.mouseMove(handle, QPoint(1, 1))
    _pump()


def _step_back(qtbot: QtBot, window: QWidget, target: QWidget) -> QWidget:
    """`target`에 초점을 둔 뒤 실제 Shift+Tab 키로 앞 위젯에 초점을 옮기고 그 위젯을 돌려준다."""

    def focused() -> bool:
        if not window.isActiveWindow():
            window.activateWindow()
            return False
        target.setFocus(Qt.FocusReason.TabFocusReason)
        return target.hasFocus()

    qtbot.waitUntil(focused, timeout=WAIT_MS)
    QTest.keyClick(target, Qt.Key.Key_Backtab)
    qtbot.waitUntil(lambda: not target.hasFocus(), timeout=WAIT_MS)
    _pump()
    before = QApplication.focusWidget()
    assert before is not None and before.window() is window, "전제: 같은 창의 앞 위젯"
    return before


def _tab_in(qtbot: QtBot, window: QWidget, before: QWidget, target: QWidget) -> None:
    """앞 위젯에서 실제 Tab 키로 `target`에 초점을 옮긴다 — 창에 WA_KeyboardFocusChange가 선다.

    macOS는 Shift+Tab과 Tab의 순서가 어긋나 한 번에 돌아오지 않는 버튼이 있다 — `target`에 닿을 때까지 Tab을 더
    누른다(`TAB_PRESSES`번까지).
    """
    for _ in range(TAB_PRESSES):
        QTest.keyClick(QApplication.focusWidget() or before, Qt.Key.Key_Tab)
        _pump()
        if target.hasFocus():
            break
    qtbot.waitUntil(target.hasFocus, timeout=WAIT_MS)
    _pump()
    assert window.testAttribute(Qt.WidgetAttribute.WA_KeyboardFocusChange), "전제: 키보드 초점 이동"


def _inside_after_tab(
    qtbot: QtBot, window: QWidget, target: QWidget, edge: int
) -> list[tuple[int, int]]:
    """Tab 키 초점 전 · 후 그림에서 `target` 안쪽 화소 중 색이 바뀐 자리(버튼 좌표)."""
    _activate(qtbot, window)
    _park(window)
    assert target.isVisible() and target.isEnabled() and not target.underMouse(), "전제"
    before = _step_back(qtbot, window, target)
    rect = _rect(window, target)
    points = _inner_points(rect, edge)
    assert points, "전제: 잴 안쪽 화소가 있다"
    unfocused = window.grab().toImage()
    _tab_in(qtbot, window, before, target)
    focused = window.grab().toImage()
    return _changed(unfocused, focused, points, rect.topLeft())


@pytest.fixture
def main_window(tmp_path: Path, basis: type) -> Iterator[VodDownloader]:  # noqa: F811 — 픽스처
    """대기 카드 하나를 둔 메인 창 — 바 버튼이 모두 보이는 폭. 끝나면 숨긴다."""
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    win.resize(win.minimumWidth() + WIDE, 700)
    _pump()
    yield win
    win.hide()


class TestMainWindowButtons:
    """메인 창 바 · 선택 줄 버튼은 Tab 키 초점에도 안쪽 그림이 그대로다."""

    @pytest.mark.parametrize("case", list(MAIN_CASES))
    def test_tab_key_focus_leaves_the_inside_unchanged(
        self,
        qtbot: QtBot,
        main_window: VodDownloader,
        scheme: str,  # noqa: F811 — 픽스처
        case: str,
    ) -> None:
        """메인 창 버튼에 Tab 키로 초점이 오면 가장자리 안쪽 화소가 초점 전과 같아야 한다.

        대기 카드 하나, 창 폭 = 최소폭 + 900, 포인터는 창 왼쪽 위, 앞 위젯에서 Tab(선택 줄은 선택 모드 · 나타나는 fade가 끝난 뒤)
        -> 가장자리 · 모서리 반경 자리를 뺀 안쪽 화소 중 초점 전과 색이 다른 것 0개
        """
        find, edge = MAIN_CASES[case]
        if case == "selectBarCancel":
            main_window.selectButton.click()
            bar = main_window.selectBar
            qtbot.waitUntil(
                lambda: bar.isVisible() and bar.graphicsEffect() is None, timeout=WAIT_MS
            )
            _pump()
        changed = _inside_after_tab(qtbot, main_window, find(main_window), edge)
        assert not changed, f"{scheme} {case}: 안쪽 {len(changed)}화소 바뀜, 처음 {changed[:5]}"

    def test_tab_key_focus_on_a_menu_tab_draws_only_the_app_box(
        self,
        qtbot: QtBot,
        main_window: VodDownloader,
        scheme: str,  # noqa: F811 — 픽스처
    ) -> None:
        """메뉴 탭에 Tab 키로 초점이 오면 그림이 setFocus(Tab 까닭) 초점 그림과 같아야 한다.

        메뉴 탭 「VOD Download」, 포인터는 창 왼쪽 위 -> 앞 위젯에서 setFocus(Tab 까닭)로 온 그림(앱 초점 상자만,
        WA_KeyboardFocusChange 없음)과 실제 Tab 키로 온 그림의 탭 안 화소 차이 0개
        """
        win = main_window
        tab = win.menuDownloadButton
        _activate(qtbot, win)
        _park(win)
        before = _step_back(qtbot, win, tab)
        win.setAttribute(Qt.WidgetAttribute.WA_KeyboardFocusChange, False)
        tab.setFocus(Qt.FocusReason.TabFocusReason)
        qtbot.waitUntil(tab.hasFocus, timeout=WAIT_MS)
        _pump()
        assert tab.property("keyFocus") is True, "전제: 앱 초점 상자가 켜졌다"
        assert not win.testAttribute(Qt.WidgetAttribute.WA_KeyboardFocusChange), "전제"
        rect = _rect(win, tab)
        app_box = win.grab().toImage()
        before.setFocus(Qt.FocusReason.TabFocusReason)
        _pump()
        _tab_in(qtbot, win, before, tab)
        keyed = win.grab().toImage()
        points = _inner_points(rect, 0)
        changed = _changed(app_box, keyed, points, rect.topLeft())
        assert not changed, f"{scheme}: 탭 안 {len(changed)}화소 다름, 처음 {changed[:5]}"


class TestDialogButtons:
    """설정 · 편집 · 구간 창 버튼은 Tab 키 초점에도 안쪽 그림이 그대로다."""

    @pytest.mark.parametrize("case", list(DIALOG_CASES))
    def test_tab_key_focus_leaves_the_inside_unchanged(
        self,
        qtbot: QtBot,
        tmp_path: Path,
        basis: type,  # noqa: F811 — 픽스처
        scheme: str,  # noqa: F811 — 픽스처
        case: str,
    ) -> None:
        """대화상자 버튼에 Tab 키로 초점이 오면 가장자리 안쪽 화소가 초점 전과 같아야 한다.

        메인 창 · 대기 카드 하나, 설정 창(쿠키 칸 바꿈) · 편집 창 · 구간 창(행 둘), 포인터는 창 왼쪽 위,
        앞 위젯에서 Tab -> 가장자리 · 모서리 반경 자리를 뺀 안쪽 화소 중 초점 전과 색이 다른 것 0개
        """
        name, find, edge = DIALOG_CASES[case]
        item = _make_item(str(tmp_path))
        win = open_window(tmp_path, item)
        held: list[QWidget] = [win]  # 붙잡는다 — addWidget은 메인 창을 붙잡지 않는다
        if name == "setting":
            dialog = _setting(qtbot)
        elif name == "edit":
            dialog = _edit(qtbot, win, item)
        else:
            dialog = _section(qtbot, win, item)
        held.append(dialog)
        changed = _inside_after_tab(qtbot, dialog, find(dialog), edge)
        assert not changed, f"{scheme} {case}: 안쪽 {len(changed)}화소 바뀜, 처음 {changed[:5]}"
        dialog.hide()
        win.hide()


class TestConfirmBoxButtons:
    """확인 창 버튼은 Tab 키 초점에도 안쪽 그림이 그대로다."""

    @pytest.mark.parametrize("case", CONFIRM_CASES)
    def test_tab_key_focus_leaves_the_inside_unchanged(
        self,
        qtbot: QtBot,
        scheme: str,  # noqa: F811 — 픽스처
        case: str,
    ) -> None:
        """확인 창 버튼에 Tab 키로 초점이 오면 가장자리 1px 안쪽 화소가 초점 전과 같아야 한다.

        「멈추고 지우기」 확인 창, 포인터는 창 왼쪽 위, 앞 위젯에서 Tab -> 가장자리 1px · 모서리 반경 자리를 뺀
        안쪽 화소 중 초점 전과 색이 다른 것 0개
        """
        seen: list[list[tuple[int, int]]] = []
        failures: list[BaseException] = []
        finished: list[bool] = []  # 테스트가 끝나면 찾기를 멈춘다 — 뒤 테스트의 창을 닫지 않게

        def run() -> None:
            if finished:
                return
            box = QApplication.activeModalWidget()
            if not isinstance(box, QMessageBox):
                QTimer.singleShot(10, run)
                return
            try:
                stop = next(
                    b
                    for b in box.buttons()
                    if box.buttonRole(b) == QMessageBox.ButtonRole.DestructiveRole
                )
                cancel = box.button(QMessageBox.StandardButton.Cancel)
                target = stop if case == "stop" else cancel
                seen.append(_inside_after_tab(qtbot, box, target, 1))
            except BaseException as error:  # 실패를 exec 밖으로 옮긴다
                failures.append(error)
            QTest.keyClick(box, Qt.Key.Key_Escape)

        QTimer.singleShot(0, run)
        try:
            view_module.confirm_stop_and_delete(None)
        finally:
            finished.append(True)
        if failures:
            raise failures[0]
        assert seen and not seen[0], f"{scheme} {case}: 안쪽 바뀐 화소 {seen[:1]}"
