"""구간 창 행 호버 — 포인터가 있는 행의 바탕 · 다시 맞추기 · 끄는 동안 끔 (#586).

실제 포인터를 옮기지 않는다. 들어감 · 나감은 창(QWindow)에 보내는 합성 포인터 이동이나 행에 보내는
들어감 이벤트로 만들고, 행을 다시 만든 뒤의 「포인터 밑 위젯」은 `app.widgets.view.widget_under_pointer`
대역으로 정한다(#575 방식). 기대 색은 theme 토큰이다.
"""

# 픽스처(basis)는 test_section_editor에서 들여와 매개변수 이름으로 받는다 — 같은 이름의 재정의는 의도다
# ruff: noqa: F811

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QEnterEvent, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

import app.theme as theme
import app.widgets.view as view_module
import CVDv2 as main_module
from app.views.section_dialog import SectionEditDialog, SectionRow
from tests.app.gui.section_reorder import (
    NO_BUTTON,
    alt_move,
    drag_handle,
    press_at,
    send_mouse,
)
from tests.app.gui.test_section_dialog_c5 import (  # noqa: F401 — 픽스처는 이름으로 쓴다
    A,
    B,
    BAD,
    C,
    _editor,
    _keep_windows,
    _pixel,
)
from tests.app.gui.test_section_editor import (  # noqa: F401 — 픽스처는 이름으로 쓴다
    _apply_production_qss,
    _destroy_windows,
    _no_network,
    _pump,
    basis,
)

QSS_PATH = main_module.resource_path(theme.QSS_RELATIVE_PATH)


@pytest.fixture(params=["dark", "light"])
def scheme(request: pytest.FixtureRequest, qapp: QApplication) -> Iterator[str]:
    """색 스킴을 그 값으로 세우고, 끝나면 다크로 되돌린다(_apply_production_qss가 다크로 연다)."""
    theme.apply_color_scheme(qapp, request.param, QSS_PATH)
    yield request.param
    theme.apply_color_scheme(qapp, "dark", QSS_PATH)


def _ground(dialog: SectionEditDialog, row: SectionRow) -> QColor:
    """행 바탕 화소 — 행 위 빈 자리(손잡이 폭 가운데 · 위 여백 세로 가운데)를 창 그림에서 읽는다.

    호버 띠 안이고 자식이 없는 자리다(띠 위 끝 = 행 위 끝).
    """
    pad = theme.METRICS["sectionRowHoverPad"]
    spot = row.mapTo(dialog, QPoint(row.handle.width() // 2, pad // 2))
    return _pixel(dialog, spot.x(), spot.y())


_SENDING_ENTER = [0]  # `_enter`가 들어감을 보내는 중이면 1 이상 — `_rows_follow_tests_only`가 본다


def _enter(widget: QWidget) -> None:
    """widget에 들어감 이벤트를 보낸다 — 포인터는 옮기지 않는다."""
    center = QPointF(widget.rect().center())
    _SENDING_ENTER[0] += 1
    try:
        QApplication.sendEvent(widget, QEnterEvent(center, center, widget.mapToGlobal(center)))
    finally:
        _SENDING_ENTER[0] -= 1
    _pump()


def _rows_follow_tests_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """행 호버가 실제 포인터 자리를 따르지 않게 한다 — `_enter`가 보낸 들어감만 행에 닿는다.

    실제 화면에서는 실제 포인터가 창 밖에 있으면 창 시스템이 창에 나감을 보낼 수 있고, 그 이벤트는
    `_enter` 뒤 몇 턴 늦게 오기도 한다 — Qt가 그것을 행 · 창의 나감으로 넘겨 `_enter`로 켠 호버가
    꺼졌다. Qt가 넘긴 나감은 `spontaneous()`가 거짓이라 이벤트로는 가를 수 없어, `_enter`가 보낸 것만
    행의 들어감 · 나감 처리에 닿게 한다. 목록의 호버 다시 맞추기는 포인터 밑에 행이 없다고 본다
    (#587 `hover_from_tests_only`와 같은 방식). 실제 포인터는 옮기지 않는다.

    `_enter`로 호버를 켜는 테스트에서 부른다. 창에 합성 포인터 이동(`_move` · `_move_in`)을 보내는
    테스트에는 쓰지 않는다 — 그 이동이 만드는 들어감도 막힌다.
    """
    monkeypatch.setattr(view_module, "widget_under_pointer", lambda: None)
    original_enter = SectionRow.enterEvent
    original_leave = SectionRow.leaveEvent

    def enter_from_tests(row: SectionRow, event: QEnterEvent) -> None:
        if _SENDING_ENTER[0]:
            original_enter(row, event)

    def leave_from_tests(row: SectionRow, event: QEvent) -> None:
        if _SENDING_ENTER[0]:
            original_leave(row, event)

    monkeypatch.setattr(SectionRow, "enterEvent", enter_from_tests)
    monkeypatch.setattr(SectionRow, "leaveEvent", leave_from_tests)


def _move(dialog: SectionEditDialog, widget: QWidget, at: QPoint | None = None) -> None:
    """창(QWindow)에 합성 포인터 이동을 보내 widget의 at(기본 가운데)에 둔다 — 실제 포인터는 옮기지 않는다."""
    local = at if at is not None else widget.rect().center()
    QTest.mouseMove(dialog.windowHandle(), widget.mapTo(dialog, local))
    _pump()


def _move_in(dialog: SectionEditDialog, widget: QWidget, at: QPoint | None = None) -> None:
    """창 왼쪽 위 (1, 1)을 거쳐 widget의 at에 합성 포인터를 둔다.

    창에 처음 보내는 이동은 포인터 밑 위젯을 끝까지 잡지 못했다(실측 — 행은 잡히고 손잡이는 안 잡힘).
    """
    _move(dialog, dialog, QPoint(1, 1))
    _move(dialog, widget, at)


def _slot_spot(row: SectionRow) -> QPoint:
    """행 안내 줄 왼쪽 들여 쓴 자리 — 자식이 없는 행 바탕이다."""
    return QPoint(row.handle.width() // 2, 2)


def _point_at(monkeypatch: pytest.MonkeyPatch, pick: Callable[[], QWidget | None]) -> None:
    """포인터 밑 위젯 대역 — 부를 때마다 pick()을 돌려준다(행을 다시 만든 뒤의 새 위젯도 가리킨다)."""
    monkeypatch.setattr(view_module, "widget_under_pointer", pick)


def _hovered(dialog: SectionEditDialog) -> list[int]:
    return [index for index, row in enumerate(dialog._rows) if row.isHovered()]


def _pointer_state(dialog: SectionEditDialog) -> str:
    """합성 포인터 이동이 행에 닿지 않았을 때 볼 상태 — 팝업 · 눌린 버튼 · 포인터 밑 위젯 · 창."""
    under = [
        w.objectName() or type(w).__name__ for w in dialog.findChildren(QWidget) if w.underMouse()
    ]
    window = dialog.windowHandle()
    return (
        f"popup={QApplication.activePopupWidget()!r} buttons={QApplication.mouseButtons()!r} "
        f"modal={QApplication.activeModalWidget()!r} active={QApplication.activeWindow()!r} "
        f"exposed={window.isExposed() if window else None} dialog={dialog.geometry()!r} "
        f"under={under} hovered={_hovered(dialog)}"
    )


def _wait_hovered(qtbot: QtBot, dialog: SectionEditDialog, row: SectionRow) -> None:
    """row가 호버 상태가 될 때까지 기다린다 — 시간을 넘기면 포인터 상태를 실패 글에 싣는다."""
    try:
        qtbot.waitUntil(row.isHovered, timeout=3000)
    except qtbot.TimeoutError as error:
        raise AssertionError(f"행 호버가 켜지지 않았다: {_pointer_state(dialog)}") from error


def test_a_hovered_row_paints_the_row_hover_ground_and_others_keep_the_window_ground(
    qtbot, tmp_path, basis, scheme: str
) -> None:  # noqa: F811
    """포인터가 들어간 행은 띠가 rowHoverBg이고 다른 행은 창 바탕 그대로여야 한다.

    행 A · B · C, 다크 · 라이트, 창에 합성 포인터 이동으로 2번 행 안내 줄 왼쪽에 둠
    -> 2번 행 바탕 화소 == rowHoverBg, 1 · 3번 행 == windowBg, 칠한 행 == [1]
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B, C])
    tokens = theme.current_tokens()
    rows = dialog._rows
    assert _ground(dialog, rows[1]) == QColor(tokens["windowBg"]), "전제: 호버 전 창 바탕"
    _move_in(dialog, rows[1].messageSlot, _slot_spot(rows[1]))
    _wait_hovered(qtbot, dialog, rows[1])

    assert _hovered(dialog) == [1]
    assert _ground(dialog, rows[1]) == QColor(tokens["rowHoverBg"])
    assert _ground(dialog, rows[0]) == QColor(tokens["windowBg"])
    assert _ground(dialog, rows[2]) == QColor(tokens["windowBg"])


def test_the_hover_stays_while_the_pointer_is_over_a_child_of_the_row(
    qtbot, tmp_path, basis
) -> None:  # noqa: F811
    """포인터가 행 안의 시각 칸 · ✕ · 손잡이로 옮겨 가도 그 행의 호버가 남아야 한다.

    행 A · B, 합성 포인터 이동: 2번 행 바탕 -> 시작 시분초 칸 -> ✕ -> 손잡이
    -> 각 자리에서 칠한 행 == [1], 2번 행 바탕 화소 == rowHoverBg
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B])
    surface = QColor(theme.current_tokens()["rowHoverBg"])
    row = dialog._rows[1]
    _move_in(dialog, row.messageSlot, _slot_spot(row))
    _wait_hovered(qtbot, dialog, row)

    for child in (row.startEdit.clockEdit, row.deleteButton, row.handle):
        _move(dialog, child)
        qtbot.waitUntil(child.underMouse, timeout=3000)  # 이동이 그 자식까지 갔다
        assert _hovered(dialog) == [1], child.objectName()
        assert _ground(dialog, row) == surface, child.objectName()


def test_the_hover_goes_off_when_the_pointer_leaves_the_rows(qtbot, tmp_path, basis) -> None:  # noqa: F811
    """포인터가 행 밖(창 제목 글)으로 나가면 행 호버가 꺼져야 한다.

    행 A · B, 2번 행에 합성 포인터 이동 -> 칠한 행 == [1] -> 제목 글로 이동 -> 칠한 행 == [],
    2번 행 바탕 화소 == windowBg
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B])
    row = dialog._rows[1]
    _move_in(dialog, row.messageSlot, _slot_spot(row))
    _wait_hovered(qtbot, dialog, row)

    _move(dialog, dialog.titleLabel)

    qtbot.waitUntil(lambda: not row.isHovered(), timeout=3000)
    assert _hovered(dialog) == []
    assert _ground(dialog, row) == QColor(theme.current_tokens()["windowBg"])


def test_the_hover_goes_off_when_the_window_becomes_inactive(qtbot, tmp_path, basis) -> None:  # noqa: F811
    """구간 창이 비활성이 되면 행 호버가 꺼지고, 비활성인 동안 들어감이 와도 켜지지 않아야 한다.

    행 A · B, 2번 행에 들어감 -> 칠한 행 == [1] -> 다른 최상위 창을 활성으로 -> 칠한 행 == [],
    1번 행에 들어감 -> 칠한 행 == []
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B])
    _enter(dialog._rows[1])
    assert _hovered(dialog) == [1], "전제: 활성 창에서 들어간 행을 칠한다"
    other = QWidget()
    other.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, False)
    other.show()
    other.activateWindow()
    QTest.qWaitForWindowActive(other)
    try:
        qtbot.waitUntil(lambda: not dialog.isActiveWindow(), timeout=3000)
        _pump()
        assert _hovered(dialog) == []
        _enter(dialog._rows[0])
        assert _hovered(dialog) == []
    finally:
        other.hide()
        other.deleteLater()


def test_no_other_row_is_hovered_while_a_row_is_dragged(
    qtbot, tmp_path, basis, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: F811
    """손잡이로 끄는 동안에는 어느 행도 호버로 칠하지 않아야 한다 — 들어감 · 굴림 다시 맞추기 모두.

    행 A · B · C, 2번 행에 들어감(칠함) -> 1번 행 손잡이를 끌기 거리 넘게 움직임 -> 칠한 행 == [];
    3번 행에 들어감 -> []; 포인터 밑 대역 = 3번 행 길이 글, 목록을 굴림 -> [];
    3번 행 바탕 화소 == windowBg
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B, C])
    rows = dialog._rows
    _enter(rows[1])
    assert _hovered(dialog) == [1], "전제: 끌기 전에는 칠한다"
    handle = rows[0].handle
    start = press_at(handle)
    _pump()
    step = QApplication.startDragDistance() + 1
    send_mouse(handle, QEvent.Type.MouseMove, start + QPoint(0, step), NO_BUTTON)
    _pump()
    assert dialog.isDragging(), "전제: 끄는 중이다"

    assert _hovered(dialog) == []
    _enter(rows[2])
    assert _hovered(dialog) == []
    _point_at(monkeypatch, lambda: rows[2].infoLabel)
    dialog.scrollArea.verticalScrollBar().valueChanged.emit(1)
    _pump()
    assert _hovered(dialog) == []
    assert _ground(dialog, rows[2]) == QColor(theme.current_tokens()["windowBg"])
    dialog._cancelDrag()


def test_after_a_drop_the_row_under_the_pointer_is_hovered(
    qtbot, tmp_path, basis, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: F811
    """손잡이로 끌어 놓은 뒤에는 새로 만든 행 가운데 포인터 밑 행을 칠해야 한다.

    행 A · B · C, 포인터 밑 대역 = 지금 2번 행의 길이 글, 1번 행을 맨 끝 틈(3)으로 끌어 놓음
    -> 순서 B · C · A, 칠한 행 == [1], 2번 행 바탕 화소 == rowHoverBg
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B, C])
    _point_at(monkeypatch, lambda: dialog._rows[1].infoLabel)

    drag_handle(dialog, 0, 3)
    _pump()

    assert [tuple(r) for r in dialog.viewModel().rows] == [B, C, A]
    assert _hovered(dialog) == [1]
    assert _ground(dialog, dialog._rows[1]) == QColor(theme.current_tokens()["rowHoverBg"])


def test_after_an_alt_move_the_row_under_the_pointer_is_hovered(
    qtbot, tmp_path, basis, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: F811
    """Alt+↑로 행을 옮긴 뒤에는 새로 만든 행 가운데 포인터 밑 행 하나만 칠해야 한다.

    행 A · B · C, 2번 행에 들어감(칠함), 포인터 밑 대역 = 지금 3번 행의 시작 시분초 칸,
    2번 행 시작 칸에서 Alt+↑ -> 순서 B · A · C, 칠한 행 == [2]
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B, C])
    _enter(dialog._rows[1])
    assert _hovered(dialog) == [1], "전제: 옮기기 전 2번 행을 칠한다"
    _point_at(monkeypatch, lambda: dialog._rows[2].startEdit.clockEdit)
    field = dialog._rows[1].startEdit.clockEdit
    field.setFocus()

    alt_move(field, Qt.Key.Key_Up)
    _pump()

    assert [tuple(r) for r in dialog.viewModel().rows] == [B, A, C]
    assert _hovered(dialog) == [2]


def test_after_a_row_is_deleted_the_row_under_the_pointer_is_hovered(
    qtbot, tmp_path, basis, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: F811
    """✕로 행을 지운 뒤에는 새로 만든 행 가운데 포인터 밑 행을 칠해야 한다.

    행 A · B · C, 포인터 밑 대역 = 지금 1번 행의 ✕, 1번 행 ✕ 누름 -> 순서 B · C, 칠한 행 == [0],
    1번 행 바탕 화소 == rowHoverBg
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B, C])
    _point_at(monkeypatch, lambda: dialog._rows[0].deleteButton)

    QTest.mouseClick(dialog._rows[0].deleteButton, Qt.MouseButton.LeftButton)
    _pump()

    assert [tuple(r) for r in dialog.viewModel().rows] == [B, C]
    assert _hovered(dialog) == [0]
    assert _ground(dialog, dialog._rows[0]) == QColor(theme.current_tokens()["rowHoverBg"])


def test_hovering_keeps_the_row_height_and_the_window_minimum_width(
    qtbot, tmp_path, basis, scheme: str
) -> None:  # noqa: F811
    """호버 전후 행 높이와 창의 최소폭이 같아야 한다.

    행 A · B, 다크 · 라이트, 2번 행에 들어감 -> 2번 행 높이 · 창 minimumSizeHint 폭 == 호버 전 값
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B])
    row = dialog._rows[1]
    height, width = row.height(), dialog.minimumSizeHint().width()

    _enter(row)

    assert row.isHovered(), "전제: 칠했다"
    assert row.height() == height
    assert dialog.minimumSizeHint().width() == width


def test_the_handle_dots_turn_body_text_color_under_the_pointer(qtbot, tmp_path, basis) -> None:  # noqa: F811
    """손잡이 점은 평소 흐린 글 색이고 포인터가 위에 있으면 본문 글 색이어야 한다.

    행 A · B, 다크 -> 1번 행 손잡이 그림의 가장 밝은 화소 밝기 <= textMuted 밝기,
    창에 합성 포인터 이동으로 손잡이에 둠 -> 가장 밝은 화소 밝기 > textMuted 밝기
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B])
    handle = dialog._rows[0].handle
    muted = QColor(theme.current_tokens()["textMuted"]).lightness()

    def brightest() -> int:
        image = handle.grab().toImage()
        return max(
            image.pixelColor(x, y).lightness()
            for x in range(image.width())
            for y in range(image.height())
        )

    assert brightest() <= muted
    _move_in(dialog, handle)
    qtbot.waitUntil(handle.underMouse, timeout=3000)
    assert brightest() > muted


# ---- 호버 띠의 자리 · 색(159e) ----

PAD = 4  # 띠 ↔ 입력칸 줄 위아래 여백(px) — 오너 결정 값(159e). 토큰을 읽지 않고 그대로 적는다
CORNER_SKIP = 8  # 글자를 찾지 않는 라벨 오른쪽 끝 폭(px) — 띠 모서리(menuHoverRadius 4)의 두 배


def _field_line(row: SectionRow) -> tuple[int, int]:
    """입력칸 줄의 위 끝 · 아래 끝(행 좌표, 둘 다 포함) — 줄에 놓인 위젯의 자리를 모아 잰다."""
    parts = (
        row.handle,
        row.numberLabel,
        row.startEdit,
        row.rangeLabel,
        row.endEdit,
        row.infoLabel,
        row.deleteButton,
    )
    return min(p.geometry().top() for p in parts), max(p.geometry().bottom() for p in parts)


def _band_rows(dialog: SectionEditDialog, row: SectionRow, image) -> list[int]:
    """손잡이 가운데 세로줄에서 rowHoverBg인 화소의 y(창 좌표) — 행 위아래 8px까지 본다."""
    color = QColor(theme.current_tokens()["rowHoverBg"])
    x = row.mapTo(dialog, QPoint(row.handle.geometry().center().x(), 0)).x()
    top = row.mapTo(dialog, QPoint(0, 0)).y()
    return [
        y
        for y in range(top - 8, top + row.height() + 8)
        if 0 <= y < image.height() and image.pixelColor(x, y) == color
    ]


def _ink_rows(dialog: SectionEditDialog, label: QWidget, image) -> list[int]:
    """label 자리에서 띠 색도 창 바탕도 아닌 화소(글자)가 있는 y(창 좌표).

    오른쪽 끝 CORNER_SKIP px는 보지 않는다 — 라벨 오른쪽 끝이 행 오른쪽 끝이라 띠의 둥근 모서리 화소가 든다.
    """
    tokens = theme.current_tokens()
    grounds = (QColor(tokens["rowHoverBg"]), QColor(tokens["windowBg"]))
    origin = label.mapTo(dialog, QPoint(0, 0))
    return [
        y
        for y in range(origin.y(), origin.y() + label.height())
        if any(
            image.pixelColor(x, y) not in grounds
            for x in range(origin.x(), origin.x() + label.width() - CORNER_SKIP)
        )
    ]


def _grab(dialog: SectionEditDialog) -> QImage:
    image = dialog.grab().toImage()
    assert image.devicePixelRatio() == 1, "전제: 배율 1 — 화소 좌표 = 논리 좌표"
    return image


def test_a_band_without_a_message_has_equal_pads_around_the_field_line(
    qtbot, tmp_path, basis, scheme: str
) -> None:  # noqa: F811
    """안내 글 없는 행의 띠는 입력칸 줄 위아래로 4px씩이고 행의 나머지 자리는 창 바탕이어야 한다.

    행 A · B · C, 다크 · 라이트, 2번 행에 들어감, 손잡이 가운데 세로줄
    -> 띠 위 끝 == 입력칸 줄 위 끝 − 4, 띠 아래 끝 == 입력칸 줄 아래 끝 + 4,
       띠 밑 ~ 행 아래 끝 == windowBg
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B, C])
    row = dialog._rows[1]
    _enter(row)
    assert _hovered(dialog) == [1] and not row.shownMessage(), "전제: 안내 글 없는 행을 칠함"
    image = _grab(dialog)
    band = _band_rows(dialog, row, image)
    top, bottom = _field_line(row)
    origin = row.mapTo(dialog, QPoint(0, 0)).y()

    assert band, "띠가 칠해졌다"
    assert band[0] == origin + top - PAD
    assert band[-1] == origin + bottom + PAD
    x = row.mapTo(dialog, QPoint(row.handle.geometry().center().x(), 0)).x()
    window = QColor(theme.current_tokens()["windowBg"])
    rest = range(band[-1] + 1, origin + row.height())
    assert rest, "전제: 띠 밑에 행의 나머지 자리가 있다"
    assert all(image.pixelColor(x, y) == window for y in rest)


def test_a_band_with_a_message_wraps_the_glyphs_and_keeps_off_the_next_field_line(
    qtbot, tmp_path, basis, scheme: str
) -> None:  # noqa: F811
    """안내 글 있는 행의 띠는 위 4px · 글자 아래 끝 + 4px이고 다음 행 입력칸 줄에 닿지 않아야 한다.

    행 A · BAD(시작 ≥ 끝 오류) · C, 다크 · 라이트, 2번 행에 들어감
    -> 띠 위 끝 == 입력칸 줄 위 끝 − 4, 띠 아래 끝 == min(글자 아래 끝 + 4, 3번 행 입력칸 줄 위 끝 − 2),
       띠 아래 끝 > 글자 아래 끝
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, BAD, C])
    row, after = dialog._rows[1], dialog._rows[2]
    _enter(row)
    assert row.errorLabel.isVisible() and _hovered(dialog) == [1], "전제: 오류 행을 칠함"
    image = _grab(dialog)
    band = _band_rows(dialog, row, image)
    ink = _ink_rows(dialog, row.errorLabel, image)
    origin = row.mapTo(dialog, QPoint(0, 0)).y()
    next_line = after.mapTo(dialog, QPoint(0, _field_line(after)[0])).y()

    assert band and ink, "띠 · 글자가 칠해졌다"
    assert band[0] == origin + _field_line(row)[0] - PAD
    assert band[-1] > ink[-1]
    assert band[-1] == min(ink[-1] + PAD, next_line - 2)


def test_the_bands_of_the_first_and_last_rows_are_not_clipped(
    qtbot: QtBot,
    tmp_path: Path,
    basis: type,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """목록 맨 위 행 · 맨 아래 행(안내 글 있음)의 띠가 목록 끝에서 잘리지 않아야 한다.

    행 C · A · B · BAD, 창 높이를 줄여 목록을 굴릴 수 있게 함, 행 호버는 `_enter`가 보낸 들어감만 따름
    -> 맨 위로 굴려 1번 행에 들어감: 띠 위 끝 == 입력칸 줄 위 끝 − 4 · 목록 보이는 범위 안,
       맨 아래로 굴려 4번 행에 들어감: 띠 아래 끝 == 글자 아래 끝 + 4 · 목록 보이는 범위 안
    """
    _rows_follow_tests_only(monkeypatch)
    dialog = _editor(qtbot, tmp_path, basis, [C, A, B, BAD])
    dialog.resize(dialog.width(), dialog.minimumSizeHint().height())
    _pump()
    bar = dialog.scrollArea.verticalScrollBar()
    assert bar.maximum() > 0, "전제: 목록을 굴릴 수 있다"
    viewport = dialog.scrollArea.viewport()
    view_top = viewport.mapTo(dialog, QPoint(0, 0)).y()
    view_bottom = view_top + viewport.height() - 1

    bar.setValue(0)
    _pump()
    first = dialog._rows[0]
    _enter(first)
    image = _grab(dialog)
    band = _band_rows(dialog, first, image)
    assert band and band[0] == first.mapTo(dialog, QPoint(0, _field_line(first)[0])).y() - PAD
    assert band[0] >= view_top

    bar.setValue(bar.maximum())
    _pump()
    last = dialog._rows[-1]
    _enter(last)
    assert last.errorLabel.isVisible() and _hovered(dialog) == [3], "전제: 오류 행을 칠함"
    image = _grab(dialog)
    band = _band_rows(dialog, last, image)
    ink = _ink_rows(dialog, last.errorLabel, image)
    assert band and ink and band[-1] == ink[-1] + PAD
    assert band[-1] <= view_bottom


def test_the_hover_does_not_break_across_a_row_boundary(qtbot, tmp_path, basis) -> None:  # noqa: F811
    """행 경계 위아래 1px에서 늘 어느 한 행이 호버여야 한다 — 행 사이에 틈이 없다.

    행 A · BAD · C, 손잡이 가운데 x에서 합성 포인터 이동: 1번 행 아래 끝 1px -> 2번 행 위 끝 1px ->
    2번 행 아래 끝 1px -> 3번 행 위 끝 1px
    -> 칠한 행 == [0] -> [1] -> [1] -> [2]
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, BAD, C])
    rows = dialog._rows
    x = rows[0].handle.geometry().center().x()
    _move_in(dialog, rows[0], QPoint(x, rows[0].height() // 2))
    qtbot.waitUntil(rows[0].isHovered, timeout=3000)

    for index, y in ((0, -1), (1, 0), (1, -1), (2, 0)):
        row = rows[index]
        _move(dialog, row, QPoint(x, row.height() - 1 if y < 0 else 0))
        assert _hovered(dialog) == [index], (index, y)


def test_the_row_pad_keeps_the_window_minimum_width(
    qtbot, tmp_path, basis, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: F811
    """행 위 빈 자리는 행 높이만 4px 늘리고 창 최소폭은 그대로 두어야 한다.

    행 A · B, sectionRowHoverPad 4 · 0으로 각각 창을 엶
    -> 행 높이 차 == 4, 창 minimumSizeHint 폭이 같음
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B])
    height = dialog._rows[1].height()
    monkeypatch.setitem(theme.METRICS, "sectionRowHoverPad", 0)
    bare = _editor(qtbot, tmp_path, basis, [A, B])

    assert height - bare._rows[1].height() == PAD
    assert dialog.minimumSizeHint().width() == bare.minimumSizeHint().width()


@pytest.mark.parametrize("scheme_name", ["dark", "light"])
def test_the_row_hover_token_is_one_step_from_the_window_ground(scheme_name: str) -> None:
    """rowHoverBg는 창 바탕에서 ΔE00 한 단계이고 라이트는 더 어둡고 다크는 더 밝아야 한다.

    다크 · 라이트 토큰 표
    -> 2.5 <= ΔE00(windowBg, rowHoverBg) <= 3.5, 밝기: 라이트 rowHoverBg < windowBg, 다크 >
    """
    tokens = theme.tokens_for(scheme_name)
    window, hover = tokens["windowBg"], tokens["rowHoverBg"]
    step = theme.delta_e00(window, hover)
    lighter = QColor(hover).lightnessF() > QColor(window).lightnessF()

    assert 2.5 <= step <= 3.5, step  # 한 단계 = 약 3.0(카드 호버 · 메뉴 호버와 같은 셈)
    assert lighter is (scheme_name == "dark")
