"""구간 창 행 호버 — 포인터가 있는 행의 바탕 · 다시 맞추기 · 끄는 동안 끔 (#586).

실제 포인터를 옮기지 않는다. 들어감 · 나감은 창(QWindow)에 보내는 합성 포인터 이동이나 행에 보내는
들어감 이벤트로 만들고, 행을 다시 만든 뒤의 「포인터 밑 위젯」은 `app.widgets.view.widget_under_pointer`
대역으로 정한다(#575 방식). 기대 색은 theme 토큰이다.
"""

# 픽스처(basis)는 test_section_editor에서 들여와 매개변수 이름으로 받는다 — 같은 이름의 재정의는 의도다
# ruff: noqa: F811

from collections.abc import Callable, Iterator

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QEnterEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

import app.theme as theme
import app.widgets.view as view_module
import CVDv2 as main_module
from app.views.section_dialog import SectionEditDialog, SectionRow
from tests.app.gui.section_reorder import NO_BUTTON, alt_move, drag_handle, press_at, send_mouse
from tests.app.gui.test_section_dialog_c5 import (  # noqa: F401 — 픽스처는 이름으로 쓴다
    A,
    B,
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
    """행 바탕 화소 — 안내 줄 왼쪽 들여 쓴 자리(손잡이 폭 가운데 · 안내 줄 세로 가운데)를 창 그림에서 읽는다."""
    slot = row.messageSlot
    spot = slot.mapTo(dialog, QPoint(row.handle.width() // 2, slot.height() // 2))
    return _pixel(dialog, spot.x(), spot.y())


def _enter(widget: QWidget) -> None:
    """widget에 들어감 이벤트를 보낸다 — 포인터는 옮기지 않는다."""
    center = QPointF(widget.rect().center())
    QApplication.sendEvent(widget, QEnterEvent(center, center, widget.mapToGlobal(center)))
    _pump()


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


def test_a_hovered_row_paints_the_surface_ground_and_others_keep_the_window_ground(
    qtbot, tmp_path, basis, scheme: str
) -> None:  # noqa: F811
    """포인터가 들어간 행은 바탕이 surface이고 다른 행은 창 바탕 그대로여야 한다.

    행 A · B · C, 다크 · 라이트, 창에 합성 포인터 이동으로 2번 행 안내 줄 왼쪽에 둠
    -> 2번 행 바탕 화소 == surface, 1 · 3번 행 == windowBg, 칠한 행 == [1]
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B, C])
    tokens = theme.current_tokens()
    rows = dialog._rows
    assert _ground(dialog, rows[1]) == QColor(tokens["windowBg"]), "전제: 호버 전 창 바탕"
    _move_in(dialog, rows[1].messageSlot, _slot_spot(rows[1]))
    qtbot.waitUntil(lambda: rows[1].isHovered(), timeout=3000)

    assert _hovered(dialog) == [1]
    assert _ground(dialog, rows[1]) == QColor(tokens["surface"])
    assert _ground(dialog, rows[0]) == QColor(tokens["windowBg"])
    assert _ground(dialog, rows[2]) == QColor(tokens["windowBg"])


def test_the_hover_stays_while_the_pointer_is_over_a_child_of_the_row(
    qtbot, tmp_path, basis
) -> None:  # noqa: F811
    """포인터가 행 안의 시각 칸 · ✕ · 손잡이로 옮겨 가도 그 행의 호버가 남아야 한다.

    행 A · B, 합성 포인터 이동: 2번 행 바탕 -> 시작 시분초 칸 -> ✕ -> 손잡이
    -> 각 자리에서 칠한 행 == [1], 2번 행 바탕 화소 == surface
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B])
    surface = QColor(theme.current_tokens()["surface"])
    row = dialog._rows[1]
    _move_in(dialog, row.messageSlot, _slot_spot(row))
    qtbot.waitUntil(row.isHovered, timeout=3000)

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
    qtbot.waitUntil(row.isHovered, timeout=3000)

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
    -> 순서 B · C · A, 칠한 행 == [1], 2번 행 바탕 화소 == surface
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B, C])
    _point_at(monkeypatch, lambda: dialog._rows[1].infoLabel)

    drag_handle(dialog, 0, 3)
    _pump()

    assert [tuple(r) for r in dialog.viewModel().rows] == [B, C, A]
    assert _hovered(dialog) == [1]
    assert _ground(dialog, dialog._rows[1]) == QColor(theme.current_tokens()["surface"])


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
    1번 행 바탕 화소 == surface
    """
    dialog = _editor(qtbot, tmp_path, basis, [A, B, C])
    _point_at(monkeypatch, lambda: dialog._rows[0].deleteButton)

    QTest.mouseClick(dialog._rows[0].deleteButton, Qt.MouseButton.LeftButton)
    _pump()

    assert [tuple(r) for r in dialog.viewModel().rows] == [B, C]
    assert _hovered(dialog) == [0]
    assert _ground(dialog, dialog._rows[0]) == QColor(theme.current_tokens()["surface"])


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
