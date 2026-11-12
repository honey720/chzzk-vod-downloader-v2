"""구간 창 C5 — 손잡이 끌기 · Alt+↑/↓ 순서 바꾸기 · 버튼 상자 · 창 제목 · 최소폭 (#586).

실제 배선을 탄다: 메인 창 → 구간 창 → 손잡이 위젯의 마우스 이벤트 · 칸의 키 이벤트 → 뷰모델 → 카드.
끌기는 실제 포인터를 옮기지 않는다 — 손잡이에 마우스 이벤트를 직접 보낸다(`section_reorder`).
기대값은 손으로 적은 값이다.
"""

# 픽스처(basis)는 test_section_editor에서 들여와 매개변수 이름으로 받는다 — 같은 이름의 재정의는 의도다
# ruff: noqa: F811

from collections.abc import Iterator

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QStyle, QWidget

import app.theme as theme
from app.views.section_dialog import DRAG_EDGE, SectionEditDialog
from core.models.plan import TimeRange
from tests.app.gui.card_helpers import shown
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
        return image.pixelColor(4 * image.devicePixelRatio(), image.height() // 2)

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
       손잡이의 왼쪽 >= 0, ✕의 오른쪽 끝 <= 목록 뷰포트의 폭, ✕의 폭 >= 그 글자 폭,
       길이 글("00:10:00.000" · "00:01:00.000")의 폭 >= 그 글자 폭(잘리지 않음)
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
        assert button.width() >= button.fontMetrics().horizontalAdvance(button.text())
        length = row.infoLabel
        assert length.text() in ("00:10:00.000", "00:01:00.000")
        assert length.width() >= length.fontMetrics().horizontalAdvance(length.text())
