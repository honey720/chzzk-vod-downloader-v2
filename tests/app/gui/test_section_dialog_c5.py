"""구간 창 C5 — 손잡이 끌기 · Alt+↑/↓ 순서 바꾸기 · 버튼 상자 · 창 제목 · 최소폭 (#586).

실제 배선을 탄다: 메인 창 → 구간 창 → 손잡이 위젯의 마우스 이벤트 · 칸의 키 이벤트 → 뷰모델 → 카드.
끌기는 실제 포인터를 옮기지 않는다 — 손잡이에 마우스 이벤트를 직접 보낸다(`section_reorder`).
기대값은 손으로 적은 값이다.
"""

# 픽스처(basis)는 test_section_editor에서 들여와 매개변수 이름으로 받는다 — 같은 이름의 재정의는 의도다
# ruff: noqa: F811

import re
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt, QTranslator
from PySide6.QtGui import QColor, QImage, QPainter, QPalette, QPen, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QStyle, QWidget

import app.theme as theme
import CVDv2 as main_module
from app.views.section_dialog import DRAG_EDGE, SectionEditDialog
from app.widgets import tip_bubble
from app.widgets.icons import action_pixmap
from core.models.plan import TimeRange
from tests.app.gui.card_helpers import bubble_text_for, enter, qt_tooltip_shown, shown
from tests.app.gui.section_reorder import (
    LEFT,
    NO_BUTTON,
    alt_move,
    drag_handle,
    gap_point,
    press_at,
    send_mouse,
)
from tests.app.gui.test_section_editor import (  # noqa: F401 — 픽스처는 이름으로 쓴다
    _apply_production_qss,
    _destroy_windows,
    _make_item,
    _no_network,
    _pump,
    basis,
    error_shown,
    open_editor,
    open_window,
    press_ok,
    set_rows,
)

A = ("00:10:00:00", "00:20:00:00")  # 10분 ~ 20분
B = ("00:30:00:00", "00:31:00:00")  # 30분 ~ 31분
C = ("00:00:05:00", "00:00:06:00")  # 5초 ~ 6초
BAD = ("00:40:00:00", "00:35:00:00")  # 시작이 끝보다 뒤 — 오류 행
ORDER_ERROR = "Start must be before end"
#: 스타일의 버튼 배치 힌트마다 확인(AcceptRole)이 취소(RejectRole)보다 앞인가 — Qt
#: qplatformdialoghelper.cpp의 배치 표에서 두 역할의 차례만 옮겨 적는다(test_qt_standard_buttons와 같은 표)
LAYOUT = QDialogButtonBox.ButtonLayout
OK_FIRST = {
    LAYOUT.WinLayout: True,
    LAYOUT.MacLayout: False,
    LAYOUT.KdeLayout: True,
    LAYOUT.GnomeLayout: False,
    LAYOUT.AndroidLayout: False,
}


_ALIVE: list = []  # 테스트 동안 메인 창을 붙잡아 둔다 — 부모 없는 창이라 참조가 끊기면 GC가 구간 창째 지운다


@pytest.fixture(autouse=True)
def _keep_windows() -> Iterator[None]:
    """`_editor`가 연 메인 창을 테스트가 끝날 때까지 붙잡고, 끝나면 놓는다(파괴는 `_destroy_windows`)."""
    yield
    _ALIVE.clear()


def _editor(qtbot, tmp_path, basis, rows: list[tuple[str, str]]) -> SectionEditDialog:  # noqa: F811
    """카드 하나의 구간 창을 열어 행을 rows로 채운다 — 창을 활성으로 둔다(초점 단언 전제)."""
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    _ALIVE.append(win)
    dialog = open_editor(qtbot, win, item)
    dialog.activateWindow()
    QTest.qWaitForWindowActive(dialog)
    set_rows(dialog, rows)
    _pump()
    return dialog


def _values(dialog: SectionEditDialog) -> list[tuple[str, str]]:
    return [tuple(row) for row in dialog.viewModel().rows]


def _numbers(dialog: SectionEditDialog) -> list[str]:
    return [shown(row.numberLabel) for row in dialog._rows]


# ================================================================ 손잡이 끌기


@pytest.mark.parametrize(
    ("source", "gap", "expected"),
    [
        (2, 1, [A, C, B]),  # 위로 한 칸
        (0, 2, [B, A, C]),  # 아래로 한 칸
        (2, 0, [C, A, B]),  # 맨 위로
        (0, 3, [B, C, A]),  # 맨 끝으로
    ],
    ids=["up", "down", "to-top", "to-end"],
)
def test_dragging_a_handle_moves_the_row_to_the_gap(
    qtbot,
    tmp_path,
    basis,
    source: int,
    gap: int,
    expected: list,  # noqa: F811
) -> None:
    """손잡이를 끌어 틈에 놓으면 그 행이 그 틈으로 옮겨지고 번호가 1부터 다시 매겨져야 한다.

    행 A · B · C에서 source 행을 gap 틈에 놓음
    -> 값 == expected, 번호 == ["1", "2", "3"], 끄는 행 흐림 · 선 · 저절로 스크롤이 거둬짐
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B, C])

    drag_handle(dialog, source, gap)

    assert _values(dialog) == expected
    assert _numbers(dialog) == ["1", "2", "3"]
    assert not dialog.isDragging() and not dialog.dropLine.isVisible()
    assert all(row.graphicsEffect() is None for row in dialog._rows)


@pytest.mark.parametrize("gap", [1, 2], ids=["own-top", "own-bottom"])
def test_dropping_a_row_next_to_itself_leaves_the_order(
    qtbot,
    tmp_path,
    basis,
    gap: int,  # noqa: F811
) -> None:
    """끄는 행의 바로 위 · 아래 틈에 놓으면 아무 행도 옮겨지지 않아야 한다.

    행 A · B · C에서 B(1번 행)를 gap 틈(1 = B의 위, 2 = B의 아래)에 놓음 -> 값 == [A, B, C]
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B, C])
    rows_before = list(dialog._rows)

    drag_handle(dialog, 1, gap)

    assert _values(dialog) == [A, B, C]
    assert dialog._rows == rows_before, "행을 다시 만들지 않는다"


def test_dropping_outside_the_window_leaves_the_order(qtbot, tmp_path, basis) -> None:  # noqa: F811
    """끄는 행을 창 밖에서 놓으면 그대로여야 한다.

    행 A · B · C에서 C의 손잡이를 창 오른쪽 아래 바깥(200px)에서 놓음 -> 값 == [A, B, C], 끌기 끝남
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B, C])
    outside = dialog.frameGeometry().bottomRight() + QPoint(200, 200)

    drag_handle(dialog, 2, to=outside)

    assert _values(dialog) == [A, B, C]
    assert not dialog.isDragging() and not dialog.dropLine.isVisible()


def test_the_drop_line_marks_the_gap_and_hides_next_to_the_dragged_row(
    qtbot,
    tmp_path,
    basis,  # noqa: F811
) -> None:
    """끄는 동안 들어갈 틈에 선이 보이고, 끄는 행의 바로 위 · 아래 틈에서는 선이 없어야 한다.

    행 A · B · C에서 B를 누르고 틈 0 → 1 → 2 → 3으로 움직임
    -> 틈 0: 선이 A의 위쪽 끝에 걸침, 틈 1 · 2: 선 없음, 틈 3: 선이 C의 아래쪽 끝에 걸침
    -> 끄는 동안 B만 흐림 효과가 붙음, Esc로 버린 뒤 효과 없음
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B, C])
    handle = dialog._rows[1].handle
    start = press_at(handle)
    send_mouse(handle, QEvent.Type.MouseMove, start + QPoint(0, 30), NO_BUTTON)
    seen = {}
    for gap in range(4):
        send_mouse(handle, QEvent.Type.MouseMove, gap_point(dialog, gap), NO_BUTTON)
        line = dialog.dropLine
        seen[gap] = (line.isVisible(), line.geometry())
    effects = [row.graphicsEffect() is not None for row in dialog._rows]
    QTest.keyClick(dialog._rows[0].startEdit.clockEdit, Qt.Key.Key_Escape)

    first, last = dialog._rows[0], dialog._rows[2]
    assert seen[0][0] and seen[0][1].top() <= first.y() <= seen[0][1].bottom() + 1
    assert not seen[1][0] and not seen[2][0]
    bottom = last.y() + last.height()
    assert seen[3][0] and seen[3][1].top() <= bottom <= seen[3][1].bottom() + 1
    assert effects == [False, True, False]
    assert all(row.graphicsEffect() is None for row in dialog._rows)


def test_escape_while_dragging_drops_the_drag_and_keeps_the_window(
    qtbot,
    tmp_path,
    basis,  # noqa: F811
) -> None:
    """끄는 중 Esc는 끌기만 버리고 창은 닫지 않아야 하며, 그 뒤 놓아도 옮겨지지 않아야 한다.

    행 A · B · C에서 C를 눌러 틈 0까지 끈 채 Esc, 그 자리에서 놓음
    -> 창이 보인다, 값 == [A, B, C], 끌기 끝남
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B, C])
    handle = dialog._rows[2].handle
    start = press_at(handle)
    send_mouse(handle, QEvent.Type.MouseMove, start + QPoint(0, -30), NO_BUTTON)
    target = gap_point(dialog, 0)
    send_mouse(handle, QEvent.Type.MouseMove, target, NO_BUTTON)
    assert dialog.isDragging(), "전제: 끄는 중"

    QTest.keyClick(dialog._rows[0].startEdit.clockEdit, Qt.Key.Key_Escape)
    send_mouse(handle, QEvent.Type.MouseButtonRelease, target, LEFT, NO_BUTTON)
    _pump()

    assert dialog.isVisible()
    assert _values(dialog) == [A, B, C]
    assert not dialog.isDragging() and not dialog.dropLine.isVisible()


@pytest.mark.parametrize("part", ["number", "length", "clock"])
def test_dragging_anything_but_the_handle_does_not_move_the_row(
    qtbot,
    tmp_path,
    basis,
    part: str,  # noqa: F811
) -> None:
    """번호 · 길이 · 시각 칸을 눌러 끌어서는 행이 옮겨지지 않아야 한다.

    행 A · B · C에서 C의 part 위젯을 누르고 A의 틈까지 움직여 놓음 -> 값 == [A, B, C], 끌기 없음
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B, C])
    row = dialog._rows[2]
    widget = {"number": row.numberLabel, "length": row.infoLabel, "clock": row.startEdit.clockEdit}[
        part
    ]
    start = press_at(widget)
    send_mouse(widget, QEvent.Type.MouseMove, start + QPoint(0, -30), NO_BUTTON)
    target = gap_point(dialog, 0)
    send_mouse(widget, QEvent.Type.MouseMove, target, NO_BUTTON)
    dragging = dialog.isDragging()
    send_mouse(widget, QEvent.Type.MouseButtonRelease, target, LEFT, NO_BUTTON)
    _pump()

    assert not dragging
    assert _values(dialog) == [A, B, C]


def test_dragging_near_the_list_edges_scrolls_the_list(qtbot, tmp_path, basis) -> None:  # noqa: F811
    """끄는 중 포인터가 목록 아래 · 위 끝에 닿으면 그쪽으로 저절로 스크롤해야 한다.

    빈 행 10개 · 창 높이 300(목록이 넘친다). 맨 위 행을 눌러 목록 아래 끝(DRAG_EDGE 안)에 둠
    -> 스크롤 값이 0에서 늘어난다. 이어 목록 위 끝에 둠 -> 스크롤 값이 줄어든다
    """
    dialog = _editor(qtbot, tmp_path, basis, [("", "")] * 10)
    dialog.resize(dialog.width(), 300)
    _pump()
    bar = dialog.scrollArea.verticalScrollBar()
    assert bar.maximum() > 0 and bar.value() == 0, "전제: 목록이 넘치고 맨 위다"
    viewport = dialog.scrollArea.viewport()
    handle = dialog._rows[0].handle
    start = press_at(handle)
    send_mouse(handle, QEvent.Type.MouseMove, start + QPoint(0, 30), NO_BUTTON)
    near_bottom = viewport.mapToGlobal(
        QPoint(viewport.width() // 2, viewport.height() - DRAG_EDGE // 2)
    )
    send_mouse(handle, QEvent.Type.MouseMove, near_bottom, NO_BUTTON)
    qtbot.waitUntil(lambda: bar.value() > 0, timeout=3000)
    down = bar.value()
    near_top = viewport.mapToGlobal(QPoint(viewport.width() // 2, DRAG_EDGE // 2))
    send_mouse(handle, QEvent.Type.MouseMove, near_top, NO_BUTTON)
    qtbot.waitUntil(lambda: bar.value() < down, timeout=3000)
    QTest.keyClick(dialog._rows[1].startEdit.clockEdit, Qt.Key.Key_Escape)

    assert not dialog.isDragging()


def test_values_numbers_and_message_lines_follow_the_dropped_row(
    qtbot,
    tmp_path,
    basis,  # noqa: F811
) -> None:
    """놓은 뒤 값 · 번호 · 행 아래 안내 줄(오류 · 무시 안내)이 각자의 행을 따라가야 한다.

    행 A · 빈 행 · BAD(시작이 끝보다 뒤)
    -> 처음: 번호 ["1", "", "2"], BAD에 오류
    BAD(3번째)를 맨 위로 끎 → 빈 행(이제 3번째)을 맨 위로 끎
    -> 값 [빈, BAD, A], 번호 ["", "1", "2"], 오류는 BAD 행에만, 무시 안내는 빈 행에만, 확인 꺼짐
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, ("", ""), BAD])
    note = dialog.viewModel().ignoredText()
    assert [error_shown(r) for r in dialog._rows] == ["", "", ORDER_ERROR], "전제: 오류"
    assert _numbers(dialog) == ["1", "", "2"]

    drag_handle(dialog, 2, 0)
    drag_handle(dialog, 2, 0)

    assert _values(dialog) == [("", ""), BAD, A]
    assert _numbers(dialog) == ["", "1", "2"]
    assert [error_shown(r) for r in dialog._rows] == ["", ORDER_ERROR, ""]
    notes = [shown(r.noteLabel) if r.noteLabel.isVisible() else "" for r in dialog._rows]
    assert notes == [note, "", ""]
    assert not dialog.okButton.isEnabled()


def test_the_dropped_order_reaches_the_card_on_ok(qtbot, tmp_path, basis) -> None:  # noqa: F811
    """끌어 바꾼 순서가 확인 뒤 카드의 구간 차례여야 한다.

    행 A · B · C에서 C를 맨 위로 끌고 확인 -> selections == (C, A, B)
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    set_rows(dialog, [A, B, C])
    _pump()

    drag_handle(dialog, 2, 0)
    press_ok(dialog)

    assert item.selections == (
        TimeRange(5.0, 6.0),
        TimeRange(600.0, 1200.0),
        TimeRange(1800.0, 1860.0),
    )


def test_a_field_with_focus_keeps_it_on_its_own_row_after_another_row_is_dropped(
    qtbot,
    tmp_path,
    basis,  # noqa: F811
) -> None:
    """끄는 동안 다른 행의 칸에 있던 초점은 놓은 뒤에도 그 칸이 든 행(옮겨 간 자리)의 같은 칸에 있어야 한다.

    행 A · B · C. A의 끝 프레임 칸에 초점을 두고 C를 맨 위로 끎 -> 초점 == 2번째 행(A)의 끝 프레임 칸
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B, C])
    dialog._rows[0].endEdit.frameEdit.setFocus()
    _pump()

    drag_handle(dialog, 2, 0)

    assert _values(dialog)[1] == A
    assert QApplication.focusWidget() is dialog._rows[1].endEdit.frameEdit


def test_a_single_row_hides_its_handle_but_keeps_its_place(qtbot, tmp_path, basis) -> None:  # noqa: F811
    """행이 하나뿐이면 손잡이가 숨고, 숨어도 칸의 자리는 행이 둘일 때와 같아야 한다.

    행 하나 -> 손잡이 숨김, 시작 칸의 x == 「구간 추가」로 행 둘이 된 뒤 첫 행 시작 칸의 x, 이제 손잡이 보임
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    alone = dialog._rows[0]
    hidden, x_alone = alone.handle.isVisible(), alone.startEdit.x()

    dialog.addButton.click()
    _pump()

    assert not hidden
    assert dialog._rows[0].startEdit.x() == x_alone
    assert all(row.handle.isVisible() for row in dialog._rows)


# ================================================================ Alt+↑/↓


@pytest.mark.parametrize(
    ("column", "part"),
    [("start", "clock"), ("start", "frame"), ("end", "clock"), ("end", "frame")],
)
def test_alt_up_moves_the_row_and_keeps_focus_on_the_same_field(
    qtbot,
    tmp_path,
    basis,
    column: str,
    part: str,  # noqa: F811
) -> None:
    """시각 칸의 Alt+↑는 그 행을 한 칸 위로 옮기고, 초점을 옮겨 간 행의 같은 칸에 둬야 한다.

    행 A · B · C의 C(3번째) column 시각 part 칸에서 Alt+↑ -> 값 [A, C, B], 초점 == 2번째 행의 같은 칸
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B, C])
    pick = lambda row: getattr(getattr(row, f"{column}Edit"), f"{part}Edit")  # noqa: E731
    field = pick(dialog._rows[2])
    field.setFocus()
    _pump()

    alt_move(field, Qt.Key.Key_Up)

    assert _values(dialog) == [A, C, B]
    assert QApplication.focusWidget() is pick(dialog._rows[1])


def test_alt_down_moves_the_row_down_and_keeps_focus(qtbot, tmp_path, basis) -> None:  # noqa: F811
    """시각 칸의 Alt+↓는 그 행을 한 칸 아래로 옮기고 초점을 따라 옮겨야 한다.

    행 A · B · C의 A 시작 시분초 칸에서 Alt+↓ -> 값 [B, A, C], 초점 == 2번째 행 시작 시분초 칸
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B, C])
    field = dialog._rows[0].startEdit.clockEdit
    field.setFocus()
    _pump()

    alt_move(field, Qt.Key.Key_Down)

    assert _values(dialog) == [B, A, C]
    assert QApplication.focusWidget() is dialog._rows[1].startEdit.clockEdit


@pytest.mark.parametrize(
    ("row", "key"), [(0, Qt.Key.Key_Up), (2, Qt.Key.Key_Down)], ids=["top", "bottom"]
)
def test_alt_arrow_at_the_end_of_the_list_does_nothing(
    qtbot,
    tmp_path,
    basis,
    row: int,
    key: Qt.Key,  # noqa: F811
) -> None:
    """맨 위 행의 Alt+↑ · 맨 아래 행의 Alt+↓는 아무 일도 하지 않아야 한다 — 초점도 그 칸 그대로다.

    행 A · B · C -> 값 [A, B, C], 행 위젯을 다시 만들지 않음, 초점 == 누른 칸
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B, C])
    rows_before = list(dialog._rows)
    field = dialog._rows[row].endEdit.clockEdit
    field.setFocus()
    _pump()

    alt_move(field, key)

    assert _values(dialog) == [A, B, C]
    assert dialog._rows == rows_before
    assert QApplication.focusWidget() is field


@pytest.mark.parametrize(
    "extra",
    [Qt.KeyboardModifier.ControlModifier, Qt.KeyboardModifier.ShiftModifier],
    ids=["ctrl", "shift"],
)
def test_alt_arrow_with_another_modifier_does_not_move_the_row(
    qtbot,
    tmp_path,
    basis,
    extra: Qt.KeyboardModifier,  # noqa: F811
) -> None:
    """Alt와 다른 수정 키가 함께 눌린 ↑는 행을 옮기지 않아야 한다(칸에 맡긴다).

    행 A · B · C의 C 시작 시분초 칸에서 Alt+extra+↑ -> 값 [A, B, C]
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B, C])
    field = dialog._rows[2].startEdit.clockEdit
    field.setFocus()
    _pump()

    alt_move(field, Qt.Key.Key_Up, extra)

    assert _values(dialog) == [A, B, C]


# ================================================================ 창 · 버튼


def test_the_window_title_is_sections(qtbot, tmp_path, basis) -> None:  # noqa: F811
    """구간 창의 제목은 "Sections"(한국어 「구간」)여야 한다.

    열린 창 -> windowTitle() == "Sections"
    """
    dialog = _editor(qtbot, tmp_path, basis, [A])

    assert dialog.windowTitle() == "Sections"


def test_ok_and_cancel_follow_the_style_order_and_ok_is_filled(qtbot, tmp_path, basis) -> None:  # noqa: F811
    """확인 · 취소는 한 버튼 상자에 스타일의 배치 차례로 놓이고, 확인은 주 동작 채움이어야 한다.

    값이 유효한 창 -> 두 버튼의 부모 == 버튼 상자, 확인이 앞인지 == 스타일 배치 표(OK_FIRST),
    확인의 role == "primary", 확인 안쪽 왼쪽 끝 바탕 == 토큰 accent, 취소 바탕 != accent
    """
    dialog = _editor(qtbot, tmp_path, basis, [A])
    ok, cancel = dialog.okButton, dialog.cancelButton
    layout = LAYOUT(QApplication.style().styleHint(QStyle.StyleHint.SH_DialogButtonLayout))
    accent = QColor(theme.current_tokens()["accent"])

    def ground(button: QWidget) -> QColor:
        image = button.grab().toImage()
        x = round(4 * image.devicePixelRatio())  # 왼쪽 안쪽 4px — 좌표는 정수 화소
        return image.pixelColor(x, image.height() // 2)

    assert ok.parent() is dialog.buttonBox and cancel.parent() is dialog.buttonBox
    assert (ok.x() < cancel.x()) == OK_FIRST[layout]
    assert ok.isEnabled() and ok.property("role") == "primary"
    assert ground(ok) == accent
    assert ground(cancel) != accent


def test_enter_in_a_field_moves_on_and_does_not_close(qtbot, tmp_path, basis) -> None:  # noqa: F811
    """시각 칸에서 친 Enter는 창을 닫지 않고 다음 시각의 시분초 칸으로 가야 한다.

    행 A · B. A의 시작 프레임 칸에서 Enter -> 창 열림, 초점 == A의 끝 시분초 칸
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B])
    field = dialog._rows[0].startEdit.frameEdit
    field.setFocus()
    _pump()

    QTest.keyClick(field, Qt.Key.Key_Return)
    _pump()

    assert dialog.isVisible()
    assert QApplication.focusWidget() is dialog._rows[0].endEdit.clockEdit


def test_rows_fit_inside_the_list_at_the_narrowest_window(qtbot, tmp_path, basis) -> None:  # noqa: F811
    """창을 가장 좁게 줄여도 행의 손잡이 ~ ✕가 겹치지 않고 목록 안에 다 들어가야 한다.

    행 A · B. 창 폭을 1로 청함(최소폭으로 선다)
    -> 행마다 손잡이 · 번호 · 시작 · ~ · 끝 · 길이 · ✕가 왼쪽부터 차례로 놓이고 서로 겹치지 않음,
       손잡이의 왼쪽 >= 0, ✕의 오른쪽 끝 <= 목록 뷰포트의 폭, ✕의 폭 >= 그 도형 한 변,
       길이 글("00:10:00:00" · "00:01:00:00")의 폭 >= 그 글자 폭(잘리지 않음)
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B])
    dialog.resize(1, 400)
    _pump()
    viewport = dialog.scrollArea.viewport()

    for row in dialog._rows:
        parts = [
            row.handle,
            row.numberLabel,
            row.startEdit,
            row.rangeLabel,
            row.endEdit,
            row.infoLabel,
            row.deleteButton,
        ]
        boxes = [part.geometry() for part in parts]
        assert all(part.isVisible() for part in parts)
        for left, right in zip(boxes, boxes[1:]):
            assert left.right() < right.left()
        assert row.mapTo(viewport, boxes[0].topLeft()).x() >= 0
        corner = row.mapTo(viewport, boxes[-1].bottomRight())
        assert corner.x() < viewport.width()
        button = row.deleteButton
        assert button.width() >= button.glyphSize()
        length = row.infoLabel
        assert length.text() in ("00:10:00:00", "00:01:00:00")
        assert length.width() >= length.fontMetrics().horizontalAdvance(length.text())


# ================================================================ 옮긴 행이 보이게 스크롤


#: 서로 다른 구간 8개 — 1분 ~ 1분 30초, 2분 ~ 2분 30초, …, 8분 ~ 8분 30초
EIGHT = [(f"00:0{n}:00:00", f"00:0{n}:30:00") for n in range(1, 9)]
LOW = 300  # 창 높이(px) — 행 8개가 목록에 다 들어가지 않는 높이


def _low_editor(qtbot, tmp_path, basis) -> SectionEditDialog:  # noqa: F811
    """행 8개(EIGHT)의 구간 창을 낮은 창(LOW)으로 연다 — 목록이 넘쳐 스크롤된다."""
    dialog = _editor(qtbot, tmp_path, basis, EIGHT)
    dialog.resize(dialog.width(), LOW)
    _pump()
    assert dialog.scrollArea.verticalScrollBar().maximum() > 0, "전제: 목록이 넘친다"
    return dialog


def _fully_visible(dialog: SectionEditDialog, widget: QWidget) -> bool:
    """widget이 목록 뷰포트의 보이는 범위 안에 다 들어 있는지 — 위젯 사각형을 뷰포트 좌표로 옮겨 견준다."""
    viewport = dialog.scrollArea.viewport()
    top_left = widget.mapTo(viewport, QPoint(0, 0))
    box = widget.rect().translated(top_left)
    return viewport.rect().contains(box)


@pytest.mark.parametrize(
    ("key", "start", "steps"),
    [(Qt.Key.Key_Down, 0, 7), (Qt.Key.Key_Up, 7, 7)],
    ids=["down-to-bottom", "up-to-top"],
)
def test_alt_moves_keep_the_focused_field_inside_the_visible_list(
    qtbot,
    tmp_path,
    basis,  # noqa: F811
    key: Qt.Key,
    start: int,
    steps: int,
) -> None:
    """Alt+↑/↓로 행을 끝까지 옮기면 매번 초점 칸과 옮겨 간 행이 목록의 보이는 범위 안에 있어야 한다.

    행 8개 · 창 높이 300. down: 맨 위 행을 보이게 둔 채 1번 행 시작 칸에서 Alt+↓ 7번,
    up: 맨 아래 행을 보이게 둔 채 8번 행 시작 칸에서 Alt+↑ 7번
    -> 매번 초점 == 옮겨 간 행의 시작 시분초 칸, 그 칸 · 그 행이 다 보임. 끝에서 그 구간이 맨 아래 · 맨 위
    """
    dialog = _low_editor(qtbot, tmp_path, basis)
    moving = EIGHT[start]
    dialog._rows[start].startEdit.clockEdit.setFocus()
    _pump()
    dialog.scrollArea.ensureWidgetVisible(dialog._rows[start], 0, 0)
    _pump()
    assert _fully_visible(dialog, dialog._rows[start]), "전제: 옮길 행이 보인다"
    step = 1 if key == Qt.Key.Key_Down else -1

    for done in range(1, steps + 1):
        alt_move(QApplication.focusWidget(), key)
        at = start + step * done
        field = dialog._rows[at].startEdit.clockEdit
        assert QApplication.focusWidget() is field, f"{done}번째: 초점이 옮겨 간 행에 없다"
        assert _fully_visible(dialog, field), f"{done}번째: 초점 칸이 안 보인다"
        assert _fully_visible(dialog, dialog._rows[at]), f"{done}번째: 옮겨 간 행이 안 보인다"

    assert _values(dialog)[start + step * steps] == moving


def test_a_dropped_row_is_scrolled_into_view(qtbot, tmp_path, basis) -> None:  # noqa: F811
    """손잡이로 끌어 보이는 범위의 아래쪽 밖에 놓으면 놓은 행이 보이게 스크롤되어야 한다.

    행 8개 · 창 높이 300 · 맨 위까지 스크롤. 아래 끝이 잘린 첫 행 r을 찾아 1번 행을 r + 1 틈에 놓음
    (그 자리는 목록 아래 · 창 안) -> 옮겨 간 행(r) == 그 구간, 그 행이 다 보임, 스크롤 값 > 0
    """
    dialog = _low_editor(qtbot, tmp_path, basis)
    bar = dialog.scrollArea.verticalScrollBar()
    bar.setValue(0)
    _pump()
    cut = next(i for i, row in enumerate(dialog._rows) if not _fully_visible(dialog, row))
    point = gap_point(dialog, cut + 1)
    assert dialog.frameGeometry().contains(point), "전제: 놓을 자리가 창 안이다"

    drag_handle(dialog, 0, to=point)

    assert _values(dialog)[cut] == EIGHT[0]
    assert _fully_visible(dialog, dialog._rows[cut])
    assert bar.value() > 0


@pytest.mark.parametrize("how", ["alt", "drag"])
def test_moving_a_row_that_stays_visible_keeps_the_scroll_value(
    qtbot,
    tmp_path,
    basis,  # noqa: F811
    how: str,
) -> None:
    """옮기기 전 · 뒤 모두 다 보이는 행을 옮기면 스크롤 값이 그대로여야 한다.

    행 8개 · 창 높이 300 · 스크롤 값 = 최대의 절반. 다 보이는 이웃 두 행 i · i + 1에서 i를 한 칸 아래로
    (alt: i 시작 칸에서 Alt+↓, drag: i 손잡이를 i + 2 틈에 놓음) -> 값이 바뀜, 스크롤 값 == 옮기기 전 값
    """
    dialog = _low_editor(qtbot, tmp_path, basis)
    bar = dialog.scrollArea.verticalScrollBar()
    bar.setValue(bar.maximum() // 2)
    _pump()
    before = bar.value()
    rows = dialog._rows
    first = next(
        i
        for i in range(len(rows) - 1)
        if _fully_visible(dialog, rows[i]) and _fully_visible(dialog, rows[i + 1])
    )

    if how == "alt":
        rows[first].startEdit.clockEdit.setFocus()
        _pump()
        alt_move(rows[first].startEdit.clockEdit, Qt.Key.Key_Down)
    else:
        drag_handle(dialog, first, first + 2)

    assert _values(dialog)[first + 1] == EIGHT[first]
    assert bar.value() == before


# ================================================================ 첫 초점


def _open_fresh(qtbot, tmp_path, basis, *, wait: bool = True) -> SectionEditDialog:  # noqa: F811
    """구간이 하나(A) 든 카드의 구간 창을 열고 활성으로 둔다 — 행을 고치지 않는다.

    wait가 참이면 조회가 끝나기를 기다린다.
    """
    item = _make_item(str(tmp_path))
    item.selections = (TimeRange(600.0, 1200.0),)  # A — 10분 ~ 20분
    win = open_window(tmp_path, item)
    _ALIVE.append(win)
    win.onCardSectionEdit(item)
    _pump()
    dialog = win._sectionDialog
    assert dialog is not None, "전제: 구간 창이 열렸다"
    if wait:
        qtbot.waitUntil(lambda: dialog.viewModel().state != "loading", timeout=3000)
    dialog.activateWindow()
    QTest.qWaitForWindowActive(dialog)
    _pump()
    return dialog


def _press_enter_on_focus(dialog: SectionEditDialog) -> None:
    """초점이 있는 위젯(없으면 창)에 Enter를 보낸다."""
    QTest.keyClick(QApplication.focusWidget() or dialog, Qt.Key.Key_Return)
    _pump()


def test_opening_puts_focus_on_the_first_rows_start_field(qtbot, tmp_path, basis) -> None:  # noqa: F811
    """구간 창을 열면 첫 행의 시작 시분초 칸에 초점이 있고, 숫자를 치면 그 칸 값을 새로 써야 한다.

    구간 A(10분 ~ 20분)가 든 카드로 엶 -> 초점 == 1번 행 시작 시분초 칸.
    이어 "5"를 침 -> 그 칸 == "00:00:05"(Tab으로 들어간 칸처럼 앞 값 00:10:00을 이어 쓰지 않는다)
    """
    dialog = _open_fresh(qtbot, tmp_path, basis)
    field = dialog._rows[0].startEdit.clockEdit

    assert QApplication.focusWidget() is field
    QTest.keyClick(field, Qt.Key.Key_5)
    _pump()
    assert field.text() == "00:00:05"


def test_enter_right_after_opening_does_not_close(qtbot, tmp_path, basis) -> None:  # noqa: F811
    """구간 창을 열자마자 Enter를 쳐도 창이 닫히지 않아야 한다.

    구간 A가 든 카드로 엶 -> 초점에 Enter -> 창 보임, 결과 코드 0(닫히지 않음), 카드 구간 그대로
    """
    dialog = _open_fresh(qtbot, tmp_path, basis)
    item = dialog.viewModel().item

    _press_enter_on_focus(dialog)

    assert dialog.isVisible()
    assert dialog.result() == 0
    assert item.selections == (TimeRange(600.0, 1200.0),)


def test_focus_moves_to_the_first_row_once_the_lookup_ends(qtbot, tmp_path, basis) -> None:  # noqa: F811
    """조회 중으로 열린 창은 초점이 버튼에 없고 Enter로 닫히지 않으며, 조회가 끝나면 첫 행 시작 칸에 와야 한다.

    조회를 붙잡고 엶 -> 초점 == 창 자신(버튼 아님), Enter -> 창 보임.
    조회를 풂 -> 초점 == 1번 행 시작 시분초 칸, Enter -> 창 보임
    """
    basis.gate.clear()
    try:
        dialog = _open_fresh(qtbot, tmp_path, basis, wait=False)
        assert dialog.viewModel().state == "loading", "전제: 조회 중이다"
        assert QApplication.focusWidget() is dialog
        _press_enter_on_focus(dialog)
        assert dialog.isVisible()
    finally:
        basis.gate.set()

    qtbot.waitUntil(lambda: dialog.viewModel().state == "ready", timeout=3000)
    qtbot.waitUntil(
        lambda: QApplication.focusWidget() is dialog._rows[0].startEdit.clockEdit, timeout=3000
    )
    _press_enter_on_focus(dialog)
    assert dialog.isVisible()


def test_a_failed_lookup_keeps_focus_off_the_buttons(qtbot, tmp_path, basis) -> None:  # noqa: F811
    """조회가 실패한 창은 초점이 창 자신에 있고 Enter로 닫히지 않으며 Esc로는 닫혀야 한다.

    조회 실패로 엶 -> 초점 == 창 자신, Enter -> 창 보임, Esc -> 창 숨음
    """
    basis.fail = True
    dialog = _open_fresh(qtbot, tmp_path, basis)
    assert dialog.viewModel().state == "failed", "전제: 조회가 실패했다"

    assert QApplication.focusWidget() is dialog
    _press_enter_on_focus(dialog)
    assert dialog.isVisible()
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    _pump()
    assert not dialog.isVisible()


def test_opening_from_the_card_edit_dialog_puts_focus_on_the_first_row(
    qtbot,
    tmp_path,
    basis,  # noqa: F811
) -> None:
    """카드 편집 창의 「구간 편집…」으로 연 구간 창도 첫 행 시작 칸에 초점이 있고 Enter로 닫히지 않아야 한다.

    구간 A가 든 카드의 편집 창 → 「구간 편집…」 -> 초점 == 1번 행 시작 시분초 칸, Enter -> 구간 창 보임
    """
    from tests.app.gui.test_card_edit_dialog import _open as open_card_edit

    item = _make_item(str(tmp_path))
    item.selections = (TimeRange(600.0, 1200.0),)
    win = open_window(tmp_path, item)
    _ALIVE.append(win)
    edit = open_card_edit(win, item)
    edit.sectionButton.click()
    _pump()
    sections = edit.sectionDialog
    assert sections is not None, "전제: 구간 창이 열렸다"
    qtbot.waitUntil(lambda: sections.viewModel().state != "loading", timeout=3000)
    sections.activateWindow()
    QTest.qWaitForWindowActive(sections)
    _pump()

    assert QApplication.focusWidget() is sections._rows[0].startEdit.clockEdit
    _press_enter_on_focus(sections)
    assert sections.isVisible()


# ================================================================ ✕ 상자 · 번호 글


def _pixel(widget: QWidget, x: int, y: int) -> QColor:
    """widget.grab()의 논리 좌표 (x, y) 화소 색 — 좌표는 정수 화소로 바꾼다."""
    image = widget.grab().toImage()
    ratio = image.devicePixelRatio()
    return image.pixelColor(round(x * ratio), round(y * ratio))


def test_the_delete_button_is_a_box_with_the_app_surface_tokens(qtbot, tmp_path, basis) -> None:  # noqa: F811
    """구간 행 ✕는 면 · 테두리가 있는 상자이고 호버하면 면이 호버 색이어야 한다.

    행 A · B, 다크 -> ✕ 왼쪽 안쪽 3px · 세로 가운데 화소 == surfaceAlt, 왼쪽 끝 0px 화소 == border.
    창(QWindow)에 합성 포인터 이동으로 ✕ 가운데에 올림 -> 같은 화소 == surfaceHover, 왼쪽 끝 == borderStrong.
    role 속성 없음(흐린 글자 버튼 아님), 글 크기 == fontSizeMeta
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B])
    tokens = theme.current_tokens()
    button = dialog._rows[0].deleteButton
    middle = button.height() // 2

    assert button.property("role") is None
    assert f"{button.font().pixelSize()}px" == theme.FONTS["fontSizeMeta"]
    assert _pixel(button, 3, middle) == QColor(tokens["surfaceAlt"])
    assert _pixel(button, 0, middle) == QColor(tokens["border"])
    # 합성 이벤트다 — 실제 포인터는 옮기지 않는다(QWindow에 보낸다, safety.md)
    # 창 안의 다른 자리를 거쳐 옮긴다 — ✕ 가운데로 한 번만 옮기면 ✕가 포인터 아래로 잡히지 않았다(실측)
    for spot in (QPoint(1, 1), button.mapTo(dialog, button.rect().center())):
        QTest.mouseMove(dialog.windowHandle(), spot)
        _pump()
    qtbot.waitUntil(button.underMouse, timeout=3000)
    assert _pixel(button, 3, middle) == QColor(tokens["surfaceHover"])
    assert _pixel(button, 0, middle) == QColor(tokens["borderStrong"])


def test_the_card_delete_button_keeps_its_icon_look(qtbot, tmp_path, basis) -> None:  # noqa: F811
    """카드의 ✕는 구간 행 ✕의 상자 모양을 받지 않아야 한다 — 평소 면 · 테두리 없음.

    대기 카드에 호버 -> 카드 ✕ role == "icon", 왼쪽 안쪽 3px 화소 != surfaceAlt, 왼쪽 끝 화소 != border
    """
    from tests.app.gui.test_card_edit_dialog import _hover

    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    _ALIVE.append(win)
    card = win.listView.widgetFor(item)
    _hover(card)
    button = card.deleteButton
    assert button.isVisible(), "전제: 호버한 카드에 ✕가 보인다"
    tokens = theme.current_tokens()
    middle = button.height() // 2

    assert button.property("role") == "icon"
    assert _pixel(button, 3, middle) != QColor(tokens["surfaceAlt"])
    assert _pixel(button, 0, middle) != QColor(tokens["border"])


def test_row_numbers_use_the_body_text_color_and_size(qtbot, tmp_path, basis) -> None:  # noqa: F811
    """구간 번호는 본문 색 · 보통 크기이고, 길이 글은 흐린 색 · 작은 글 그대로여야 한다.

    행 A · B, 다크 -> 번호: 글꼴 크기 == fontSizeBase, 글 색(WindowText) == text, 그림의 가장 밝은 화소의
    밝기 > textMuted 밝기(흐린 색으로는 나올 수 없는 밝기). 길이: 글꼴 크기 == fontSizeMeta, 글 색 == textMuted,
    가장 밝은 화소의 밝기 <= textMuted 밝기
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B])
    tokens = theme.current_tokens()
    number = dialog._rows[0].numberLabel
    length = dialog._rows[0].infoLabel
    muted = QColor(tokens["textMuted"]).lightness()

    def brightest(widget: QWidget) -> int:
        image = widget.grab().toImage()
        return max(
            image.pixelColor(x, y).lightness()
            for x in range(image.width())
            for y in range(image.height())
        )

    def ink(widget: QWidget) -> QColor:
        return widget.palette().color(QPalette.ColorRole.WindowText)

    assert shown(number) == "1", "전제: 번호가 보인다"
    assert f"{number.font().pixelSize()}px" == theme.FONTS["fontSizeBase"]
    assert ink(number) == QColor(tokens["text"])
    assert brightest(number) > muted
    assert f"{length.font().pixelSize()}px" == theme.FONTS["fontSizeMeta"]
    assert ink(length) == QColor(tokens["textMuted"])
    assert brightest(length) <= muted


# ================================================================ ✕는 그린 대각선 둘 (inbox 159i)


def _reference_cross(color: QColor, size: int, ratio: float) -> QPixmap:
    """카드 ✕와 같은 대각선 둘을 테스트가 따로 그린 픽스맵 — 한 변 size, 선 굵기 0.16, 둥근 끝, 22% ~ 78%.

    제품의 그리기 함수(``action_pixmap``)를 부르지 않는다 — 제품이 모양을 바꾸면 여기와 달라진다.
    """
    pixmap = QPixmap(round(size * ratio), round(size * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(color, size * 0.16)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    s = float(size)
    painter.drawLine(QPointF(s * 0.22, s * 0.22), QPointF(s * 0.78, s * 0.78))
    painter.drawLine(QPointF(s * 0.78, s * 0.22), QPointF(s * 0.22, s * 0.78))
    painter.end()
    return pixmap


def _glyph_origin(button: QWidget) -> QPoint:
    """버튼 가운데에 놓인 도형 정사각의 왼쪽 위(논리 좌표)."""
    size = theme.METRICS["actionGlyph"]
    return QPoint((button.width() - size) // 2, (button.height() - size) // 2)


def _inked(color: QColor, ink: QColor, ground: QColor) -> bool:
    """화소가 바탕보다 도형 색에 가까운지(RGB 거리)."""

    def distance(a: QColor, b: QColor) -> int:
        return abs(a.red() - b.red()) + abs(a.green() - b.green()) + abs(a.blue() - b.blue())

    return distance(color, ink) < distance(color, ground)


def _worst_channel_gap(first: QImage, second: QImage, rect: QRect, alpha: bool = False) -> int:
    """두 그림의 rect(화소 좌표) 안에서 채널마다의 차 가운데 가장 큰 값."""
    worst = 0
    for x in range(rect.left(), rect.left() + rect.width()):
        for y in range(rect.top(), rect.top() + rect.height()):
            a, b = first.pixelColor(x, y), second.pixelColor(x, y)
            gaps = [abs(a.red() - b.red()), abs(a.green() - b.green()), abs(a.blue() - b.blue())]
            if alpha:
                gaps.append(abs(a.alpha() - b.alpha()))
            worst = max(worst, *gaps)
    return worst


def test_the_section_x_is_two_painted_diagonals(qtbot: QtBot, tmp_path: Path, basis: type) -> None:
    """구간 행 ✕는 글자가 아니라 대각선 둘이어야 한다 — 가운데 · 네 모서리 근처는 칠하고 네 변 가운데는 비운다.

    행 A · B, 다크, 포인터 없음 -> ✕ 글 == "", 도형 정사각(actionGlyph 한 변, 버튼 가운데)에서
    (0.5, 0.5) · (0.3, 0.3) · (0.7, 0.3) · (0.3, 0.7) · (0.7, 0.7) 화소는 text 색에 가깝고,
    (0.5, 0.2) · (0.2, 0.5) · (0.8, 0.5) · (0.5, 0.8) 화소는 바탕(surfaceAlt)에 가깝다
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B])
    button = dialog._rows[0].deleteButton
    tokens = theme.current_tokens()
    ink, ground = QColor(tokens["text"]), QColor(tokens["surfaceAlt"])
    origin = _glyph_origin(button)
    size = theme.METRICS["actionGlyph"]

    def at(fx: float, fy: float) -> QColor:
        return _pixel(button, int(origin.x() + fx * size), int(origin.y() + fy * size))

    assert button.text() == ""
    assert not button.underMouse(), "전제: 포인터가 ✕ 위에 없다"
    for spot in ((0.5, 0.5), (0.3, 0.3), (0.7, 0.3), (0.3, 0.7), (0.7, 0.7)):
        assert _inked(at(*spot), ink, ground), f"{spot}가 칠해지지 않았다"
    for spot in ((0.5, 0.2), (0.2, 0.5), (0.8, 0.5), (0.5, 0.8)):
        assert not _inked(at(*spot), ink, ground), f"{spot}가 칠해졌다"


def test_the_section_x_matches_the_card_x_drawing_regardless_of_the_font(
    qtbot: QtBot, tmp_path: Path, basis: type
) -> None:
    """구간 행 ✕의 도형 자리 화소는 바탕 위에 카드 ✕ 모양을 그린 것과 같아야 한다 — 글꼴과 무관하다.

    행 A · B, 다크 -> ✕ 그림의 도형 정사각 화소 == surfaceAlt로 채운 그림에 테스트가 그린 대각선 둘
    (text 색, 한 변 actionGlyph)을 같은 자리에 얹은 것(채널마다 차 <= 2)
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B])
    button = dialog._rows[0].deleteButton
    tokens = theme.current_tokens()
    image = button.grab().toImage()
    ratio = image.devicePixelRatio()
    size = theme.METRICS["actionGlyph"]
    origin = _glyph_origin(button)
    expected = QImage(image.size(), QImage.Format.Format_ARGB32_Premultiplied)
    expected.setDevicePixelRatio(ratio)
    expected.fill(QColor(tokens["surfaceAlt"]))
    painter = QPainter(expected)
    painter.drawPixmap(origin, _reference_cross(QColor(tokens["text"]), size, ratio))
    painter.end()
    side = round(size * ratio)
    square = QRect(round(origin.x() * ratio), round(origin.y() * ratio), side, side)

    assert not button.underMouse(), "전제: 포인터가 ✕ 위에 없다"
    assert _worst_channel_gap(image, expected, square) <= 2


def test_a_disabled_section_x_is_drawn_in_the_disabled_text_color(
    qtbot: QtBot, tmp_path: Path, basis: type
) -> None:
    """꺼진 ✕(하나뿐인 빈 행)의 대각선은 text가 아니라 textDisabled 색에 가까워야 한다.

    빈 행 하나 -> ✕ 꺼짐, 도형 가운데 화소가 text보다 textDisabled에 가깝다
    """
    dialog = _editor(qtbot, tmp_path, basis, [("", "")])
    button = dialog._rows[0].deleteButton
    tokens = theme.current_tokens()
    origin = _glyph_origin(button)
    size = theme.METRICS["actionGlyph"]
    middle = _pixel(button, origin.x() + size // 2, origin.y() + size // 2)

    assert not button.isEnabled(), "전제: 하나뿐인 빈 행의 ✕는 꺼져 있다"
    assert _inked(middle, QColor(tokens["textDisabled"]), QColor(tokens["text"]))


def test_the_card_x_still_uses_the_same_cross(qtbot: QtBot, tmp_path: Path, basis: type) -> None:
    """카드 ✕의 도형은 테스트가 따로 그린 대각선 둘과 같아야 한다 — 카드 ✕ 모양은 바뀌지 않는다.

    대기 카드 -> 카드 ✕ 도형 이름 "delete", 한 변 actionGlyph, 그 픽스맵 화소 == 테스트가 그린 대각선
    둘(같은 색 · 한 변 · 배율, 채널 · 알파마다 차 <= 2)
    """
    from tests.app.gui.test_card_edit_dialog import _hover

    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    _ALIVE.append(win)
    card = win.listView.widgetFor(item)
    _hover(card)
    button = card.deleteButton
    color = theme.current_tokens()[button.colorToken()]
    ratio = button.devicePixelRatioF()
    size = button.glyphSize()
    drawn = action_pixmap(button.iconName(), color, size, ratio).toImage()
    reference = _reference_cross(QColor(color), size, ratio).toImage()

    assert button.iconName() == "delete"
    assert size == theme.METRICS["actionGlyph"]
    assert drawn.size() == reference.size()
    assert _worst_channel_gap(drawn, reference, drawn.rect(), alpha=True) <= 2


# ================================================================ 길이 · 합계는 프레임 타임코드 (inbox 159i)


def _lengths(dialog: SectionEditDialog) -> list[str]:
    return [row.infoLabel.text() for row in dialog._rows]


def _length_tips(dialog: SectionEditDialog) -> list[str]:
    return [row.infoLabel.toolTip() for row in dialog._rows]


def test_a_27_frame_length_at_60fps_ends_in_27(qtbot: QtBot, tmp_path: Path, basis: type) -> None:
    """60fps에서 27프레임짜리 구간의 길이는 「:27」로 끝나고 툴팁은 같은 시간의 밀리초여야 한다.

    60fps · 3600초, 행 00:00:10:00 ~ 00:00:10:27
    -> 길이 == "00:00:00:27", 길이 툴팁 == "00:00:00.450"(27 ÷ 60초), 합계 == "Total 00:00:00:27",
       합계 툴팁 == "00:00:00.450"
    """
    dialog = _editor(qtbot, tmp_path, basis, [("00:00:10:00", "00:00:10:27")])

    assert _lengths(dialog) == ["00:00:00:27"]
    assert _length_tips(dialog) == ["00:00:00.450"]
    assert dialog.sumLabel.text() == "Total 00:00:00:27"
    assert dialog.sumLabel.toolTip() == "00:00:00.450"


def test_lengths_at_30fps_use_30_frames_per_second(
    qtbot: QtBot, tmp_path: Path, basis: type
) -> None:
    """30fps 영상의 길이는 프레임 칸이 30마다 초로 넘어가야 한다.

    30fps · 3600초, 행 00:00:10:00 ~ 00:00:11:15(45프레임), 행 00:01:00:20 ~ 00:01:02:05(45프레임)
    -> 길이 == ["00:00:01:15", "00:00:01:15"], 툴팁 == ["00:00:01.500", "00:00:01.500"],
       합계 == "Total 00:00:03:00"(90프레임), 합계 툴팁 == "00:00:03.000"
    """
    basis.fps = Fraction(30)
    dialog = _editor(
        qtbot, tmp_path, basis, [("00:00:10:00", "00:00:11:15"), ("00:01:00:20", "00:01:02:05")]
    )

    assert _lengths(dialog) == ["00:00:01:15", "00:00:01:15"]
    assert _length_tips(dialog) == ["00:00:01.500", "00:00:01.500"]
    assert dialog.sumLabel.text() == "Total 00:00:03:00"
    assert dialog.sumLabel.toolTip() == "00:00:03.000"


def test_lengths_at_59_94fps_follow_the_input_timecode(
    qtbot: QtBot, tmp_path: Path, basis: type
) -> None:
    """59.94fps 영상의 길이는 입력 칸의 타임코드 차와 같아야 한다 — 프레임 칸은 60개(00 ~ 59)다.

    59.94fps(2997/50) · 3600초, 행 00:00:00:30 ~ 00:00:01:59(프레임 30 ~ 119, 89프레임)
    -> 길이 == "00:00:01:29", 툴팁 == "00:00:01.485"(89 ÷ 59.94초), 합계 == "Total 00:00:01:29"
    """
    basis.fps = Fraction(2997, 50)
    dialog = _editor(qtbot, tmp_path, basis, [("00:00:00:30", "00:00:01:59")])

    assert _lengths(dialog) == ["00:00:01:29"]
    assert _length_tips(dialog) == ["00:00:01.485"]
    assert dialog.sumLabel.text() == "Total 00:00:01:29"


def test_the_sum_carries_frames_into_seconds(qtbot: QtBot, tmp_path: Path, basis: type) -> None:
    """합계는 행 길이의 프레임을 더해 60프레임마다 초로 넘겨야 한다.

    60fps · 3600초, 행 00:00:00:40 ~ 00:00:01:00(20프레임), 행 00:00:02:00 ~ 00:00:02:50(50프레임)
    -> 길이 == ["00:00:00:20", "00:00:00:50"], 합계 == "Total 00:00:01:10"(70프레임),
       합계 툴팁 == "00:00:01.167"
    """
    dialog = _editor(
        qtbot, tmp_path, basis, [("00:00:00:40", "00:00:01:00"), ("00:00:02:00", "00:00:02:50")]
    )

    assert _lengths(dialog) == ["00:00:00:20", "00:00:00:50"]
    assert dialog.sumLabel.text() == "Total 00:00:01:10"
    assert dialog.sumLabel.toolTip() == "00:00:01.167"


OVER_ROWS = [("", "00:40:00:00"), ("00:20:00:00", "00:50:00:00")]  # 40분 + 30분 = 70분 > 60분
FRAME_FORM = r"\d\d:\d\d:\d\d:\d\d"  # 프레임 타임코드 HH:MM:SS:FF


def _header_end(dialog: SectionEditDialog) -> str:
    """머리줄에 보이는 영상 끝 타임코드 — 「… video ends at <끝>」의 끝."""
    assert dialog.headerLabel.isVisible()
    return dialog.headerLabel.text().rsplit("video ends at ", 1)[1]


def _notice_numbers(dialog: SectionEditDialog) -> tuple[str, str]:
    """합계 초과 알림의 (구간 합, 영상 길이) — 알림 전체 글에서 읽는다."""
    assert dialog.totalLabel.isVisible()
    match = re.fullmatch(
        r"Sections add up to more than the video · Sections (\S+) · Video (\S+)",
        dialog.totalLabel.text(),
    )
    assert match is not None, dialog.totalLabel.text()
    return match.group(1), match.group(2)


@pytest.mark.parametrize(
    ("fps", "end"),
    [
        (Fraction(30), "00:59:59:29"),  # 107999프레임 — 108000프레임째(3600초)는 길이를 넘는다
        # 215783프레임(3599.983초) — 3599초가 시작되는 215724프레임에서 59프레임 뒤
        (Fraction(2997, 50), "00:59:59:59"),
    ],
    ids=["30fps", "59.94fps"],
)
def test_the_over_total_notice_is_in_frames_with_the_header_end_as_the_video(
    qtbot: QtBot, tmp_path: Path, basis: type, fps: Fraction, end: str
) -> None:
    """합계 초과 알림의 두 값은 프레임 타임코드이고 영상 길이는 머리줄의 영상 끝과 같아야 한다.

    fps · 3599.995초, 행 처음 ~ 00:40:00:00, 행 00:20:00:00 ~ 00:50:00:00(합 70분)
    -> 구간 합 == "01:10:00:00", 영상 길이 == end(HH:MM:SS:FF 꼴) == 머리줄 「video ends at」 뒤 글
    """
    basis.fps = fps
    # 프레임 경계가 아닌 길이 — 반올림하면 01:00:00:00이고, 길이를 넘지 않는 마지막 프레임은 그 앞이다
    basis.duration = 3599.995
    dialog = _editor(qtbot, tmp_path, basis, OVER_ROWS)

    total, video = _notice_numbers(dialog)

    assert total == "01:10:00:00"
    assert re.fullmatch(FRAME_FORM, video), video
    assert video == end
    assert video == _header_end(dialog)


def test_length_tooltips_follow_moves_and_value_changes(
    qtbot: QtBot, tmp_path: Path, basis: type
) -> None:
    """길이 · 합계 툴팁은 행을 옮기거나 값을 바꾸면 그 행의 값으로 따라 바뀌어야 한다.

    60fps · 3600초, 행 A(10분) · B(1분)
    -> 툴팁 ["00:10:00.000", "00:01:00.000"], 합계 툴팁 "00:11:00.000"
    -> 첫 행 시작 칸에서 Alt+↓: 툴팁 ["00:01:00.000", "00:10:00.000"], 길이 ["00:01:00:00", "00:10:00:00"]
    -> 첫 행(B)의 끝을 00:30:30:30으로: 툴팁 ["00:00:30.500", "00:10:00.000"], 합계 툴팁 "00:10:30.500"
    """
    from tests.app.gui.test_section_editor import leave, type_into

    dialog = _editor(qtbot, tmp_path, basis, [A, B])
    before = (_length_tips(dialog), dialog.sumLabel.toolTip())
    alt_move(dialog._rows[0].startEdit.clockEdit, Qt.Key.Key_Down)
    _pump()
    moved = (_length_tips(dialog), _lengths(dialog))
    type_into(dialog._rows[0].endEdit, "00:30:30:30")
    leave(dialog._rows[0].endEdit)
    _pump()

    assert before == (["00:10:00.000", "00:01:00.000"], "00:11:00.000")
    assert moved == (["00:01:00.000", "00:10:00.000"], ["00:01:00:00", "00:10:00:00"])
    assert _length_tips(dialog) == ["00:00:30.500", "00:10:00.000"]
    assert dialog.sumLabel.toolTip() == "00:10:30.500"


def test_an_error_row_has_no_length_and_no_length_tooltip(
    qtbot: QtBot, tmp_path: Path, basis: type
) -> None:
    """오류 행은 길이 글도 길이 툴팁도 없어야 하고, 합계와 그 툴팁에서 빠져야 한다.

    60fps · 3600초, 행 A(10분) · 오류 행(시작 > 끝)
    -> 길이 ["00:10:00:00", ""], 툴팁 ["00:10:00.000", ""], 합계 "Total 00:10:00:00", 합계 툴팁 "00:10:00.000"
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, BAD])

    assert error_shown(dialog._rows[1]) == ORDER_ERROR, "전제: 둘째 행이 오류다"
    assert _lengths(dialog) == ["00:10:00:00", ""]
    assert _length_tips(dialog) == ["00:10:00.000", ""]
    assert dialog.sumLabel.text() == "Total 00:10:00:00"
    assert dialog.sumLabel.toolTip() == "00:10:00.000"


@pytest.mark.parametrize("failed", (False, True), ids=("loading", "failed"))
def test_no_length_or_sum_is_shown_before_the_frame_rate_is_known(
    qtbot: QtBot, tmp_path: Path, basis: type, failed: bool
) -> None:
    """프레임률을 모르는 동안(조회 중 · 조회 실패)에는 행 · 길이 · 합계와 그 툴팁이 없어야 한다.

    조회를 붙잡아 둔 창(loading) · 조회가 실패한 창(failed) -> 행 0개, 합계 숨김 · 글 "" · 툴팁 ""
    """
    from tests.app.gui.test_section_editor import ask_section_editor

    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    _ALIVE.append(win)
    if failed:
        basis.fail = True
    else:
        basis.gate.clear()
    try:
        ask_section_editor(win, item)
        dialog = win._sectionDialog
        if failed:
            qtbot.waitUntil(lambda: dialog.viewModel().state == "failed", timeout=3000)
        _pump()
        assert dialog.viewModel().state == ("failed" if failed else "loading")
        assert dialog._rows == []
        assert not dialog.sumLabel.isVisible()
        assert (dialog.sumLabel.text(), dialog.sumLabel.toolTip()) == ("", "")
    finally:
        basis.gate.set()


class TestLengthTipBubbles:
    """길이 · 합계 툴팁은 앱의 말풍선으로 뜨고 Qt 기본 툴팁은 뜨지 않는다 (inbox 159i)."""

    @pytest.fixture(autouse=True)
    def _hide_bubble(self) -> Iterator[None]:
        tip_bubble.hide_tip()
        yield
        tip_bubble.hide_tip()

    def test_length_and_sum_show_milliseconds_in_the_bubble(
        self, qtbot: QtBot, tmp_path: Path, basis: type
    ) -> None:
        """길이 글 · 합계 글에 들어가면 밀리초 툴팁이 말풍선으로 떠야 한다.

        60fps · 3600초, 행 A · B -> 첫 행 길이에 들어감: 말풍선 "00:10:00.000", 합계에 들어감: 말풍선
        "00:11:00.000", 둘 다 Qt 툴팁 안 뜸
        """
        dialog = _editor(qtbot, tmp_path, basis, [A, B])
        seen = []
        for widget in (dialog._rows[0].infoLabel, dialog.sumLabel):
            enter(widget)
            seen.append((bubble_text_for(widget), qt_tooltip_shown(widget)))

        assert seen == [("00:10:00.000", False), ("00:11:00.000", False)]


# ================================================================ 안내는 말줄임 없이 다 보인다 (inbox 159i)

HINT_EN = (
    "Digits fill from the right · '.' goes to the frame field · "
    "empty = start/end of the video · up to 20 sections"
)
HINT_KO = "숫자는 오른쪽부터 채움 · '.'는 프레임 칸 · 비우면 영상 처음/끝 · 최대 20개"


def _hint_lines_needed(label: QWidget, text: str) -> int:
    """그 글을 label 폭에 낱말 단위로 줄바꿈하면 드는 줄 수 — 테스트가 글꼴 정보로 따로 센다."""
    metrics = label.fontMetrics()
    box = metrics.boundingRect(
        QRect(0, 0, label.contentsRect().width(), 10_000), Qt.TextFlag.TextWordWrap, text
    )
    return max(1, round(box.height() / metrics.lineSpacing()))


def _assert_hint_fully_shown(dialog: SectionEditDialog, text: str) -> int:
    """안내가 보이고 글이 전문이며 줄바꿈에 드는 줄이 다 들어가는 높이인지 단언한다 — 든 줄 수를 돌려준다."""
    hint = dialog.hintLabel
    lines = _hint_lines_needed(hint, text)
    metrics = hint.fontMetrics()
    assert hint.isVisible()
    assert hint.text() == text
    # 줄 상자의 높이 — 한 줄이면 글 높이, 그 뒤 줄마다 줄 간격이 더해진다
    needed = metrics.height() + (lines - 1) * metrics.lineSpacing()
    assert hint.contentsRect().height() >= needed, f"{lines}줄이 들 높이가 아니다"
    assert hint.geometry().top() >= dialog.scrollArea.geometry().bottom()
    assert hint.geometry().bottom() <= dialog.buttonBox.geometry().top()
    return lines


@pytest.mark.parametrize("language", ("en", "ko"))
def test_the_hint_is_fully_shown_at_the_first_and_the_narrowest_width(
    qtbot: QtBot, tmp_path: Path, basis: type, qapp: QApplication, language: str
) -> None:
    """안내 글은 창 처음 폭(560)과 최소폭에서 말줄임 없이 다 보여야 한다 — 한 줄에 안 들면 두 줄로 나눈다.

    60fps · 3600초, 행 하나, 영어 · 한국어 × 폭 560을 청함(최소폭이 더 넓으면 최소폭) · 최소폭(폭 1을 청함)
    -> 안내 글 == 전문, 안내 높이 >= 낱말 줄바꿈에 드는 줄 수의 높이, 목록 아래 · 버튼 상자 위
    -> 영어 최소폭은 두 줄 이상이 든다(전제 — 줄바꿈을 실제로 잰다)
    """
    translator = QTranslator()
    if language == "ko":
        assert translator.load(main_module.resource_path("translations/ko_KR.qm"))
        qapp.installTranslator(translator)
    try:
        dialog = _editor(qtbot, tmp_path, basis, [A])
        text = HINT_KO if language == "ko" else HINT_EN
        dialog.resize(560, 420)
        _pump()
        assert dialog.width() == max(560, dialog.minimumWidth()), "전제: 처음 폭"
        _assert_hint_fully_shown(dialog, text)
        dialog.resize(1, 420)
        _pump()
        assert dialog.width() == dialog.minimumWidth(), "전제: 최소폭"
        lines = _assert_hint_fully_shown(dialog, text)
        if language == "en":
            assert lines >= 2, "전제: 최소폭의 영어 안내는 한 줄에 들지 않는다"
    finally:
        qapp.removeTranslator(translator)


def test_the_hint_wraps_without_moving_the_buttons_or_the_minimum_size(
    qtbot: QtBot, tmp_path: Path, basis: type
) -> None:
    """안내가 두 줄이 되어도 창 최소 높이와 버튼 상자 자리는 그대로이고, 늘어난 줄은 목록이 내줘야 한다.

    60fps · 3600초, 행 하나, 영어. 폭 2000 · 높이 420 -> 안내 한 줄. 폭 1 · 높이 420 -> 안내 두 줄 이상
    -> 두 경우 버튼 상자 위 끝이 같고, 창 최소 높이가 같고, 목록 높이 차 == 안내 높이 차
    """
    dialog = _editor(qtbot, tmp_path, basis, [A])
    hint = dialog.hintLabel
    seen = {}
    for width in (2000, 1):
        dialog.resize(width, 420)
        _pump()
        seen[width] = (
            _hint_lines_needed(hint, HINT_EN),
            dialog.buttonBox.geometry().top(),
            dialog.minimumSizeHint().height(),
            dialog.scrollArea.height(),
            hint.height(),
        )
    wide, narrow = seen[2000], seen[1]

    assert (wide[0], narrow[0] >= 2) == (1, True), "전제: 넓으면 한 줄, 최소폭이면 두 줄 이상"
    assert narrow[1] == wide[1]
    assert narrow[2] == wide[2]
    assert wide[3] - narrow[3] == narrow[4] - wide[4]
