"""창 경로 합성 이동 앞 1px 옆 이동 도우미 테스트 (#667)."""

from collections.abc import Iterator

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QWindow
from PySide6.QtWidgets import QApplication, QWidget

from tests.app.gui.pointer_moves import PointerMoves


@pytest.fixture
def shown(qapp: QApplication) -> Iterator[QWindow]:
    """보이는 300×200 창의 `QWindow`."""
    widget = QWidget()
    widget.resize(300, 200)
    widget.show()
    QApplication.processEvents()
    yield widget.windowHandle()
    widget.hide()


def _recorder() -> tuple[PointerMoves, list[tuple[object, QPoint]]]:
    calls: list[tuple[object, QPoint]] = []
    return PointerMoves(lambda target, pos=QPoint(), *rest: calls.append((target, pos))), calls


def test_first_move_into_a_window_is_preceded_by_a_one_pixel_step(shown: QWindow) -> None:
    """창의 첫 이동이면 목표 오른쪽 1px로 먼저 움직인 뒤 목표로 움직여야 한다.

    (50, 40)으로 이동 -> 원래 이동 호출 (51, 40) · (50, 40)
    """
    moves, calls = _recorder()
    moves(shown, QPoint(50, 40))
    assert [pos for _, pos in calls] == [QPoint(51, 40), QPoint(50, 40)]


def test_a_different_point_in_the_same_window_is_moved_to_directly(shown: QWindow) -> None:
    """같은 창의 다른 자리로 움직이면 1px 옆 이동 없이 바로 움직여야 한다.

    (50, 40) -> (80, 60) -> 둘째 이동의 원래 호출은 (80, 60) 하나
    """
    moves, calls = _recorder()
    moves(shown, QPoint(50, 40))
    calls.clear()
    moves(shown, QPoint(80, 60))
    assert [pos for _, pos in calls] == [QPoint(80, 60)]


def test_the_same_point_again_is_preceded_by_a_one_pixel_step(shown: QWindow) -> None:
    """바로 앞 이동과 같은 자리로 다시 움직이면 1px 옆으로 먼저 움직여야 한다.

    (50, 40) -> (50, 40) -> 둘째 이동의 원래 호출은 (51, 40) · (50, 40)
    """
    moves, calls = _recorder()
    moves(shown, QPoint(50, 40))
    calls.clear()
    moves(shown, QPoint(50, 40))
    assert [pos for _, pos in calls] == [QPoint(51, 40), QPoint(50, 40)]


def test_the_step_goes_left_when_the_right_pixel_was_the_last_point(
    shown: QWindow, qapp: QApplication
) -> None:
    """새 창의 첫 이동에서 오른쪽 1px가 바로 앞 이동의 화면 자리면 왼쪽 1px로 먼저 움직여야 한다.

    같은 자리의 창 둘, 앞 창 (51, 40) 이동 -> 새 창 (50, 40) 이동 -> 원래 호출 (49, 40) · (50, 40)
    """
    other = QWidget()
    other.resize(300, 200)
    other.show()
    QApplication.processEvents()
    try:
        other.windowHandle().setPosition(shown.position())  # 두 창의 같은 자리가 같은 화면 좌표
        QApplication.processEvents()
        moves, calls = _recorder()
        moves(other.windowHandle(), QPoint(51, 40))
        calls.clear()
        moves(shown, QPoint(50, 40))
        assert [pos for _, pos in calls] == [QPoint(49, 40), QPoint(50, 40)]
    finally:
        other.hide()


def test_widget_moves_are_passed_through_unchanged(qapp: QApplication) -> None:
    """위젯 경로 이동은 덧붙는 이동 없이 그대로 넘겨야 한다.

    위젯에 (10, 10) 이동 -> 원래 이동 호출 하나(위젯, (10, 10))
    """
    widget = QWidget()
    moves, calls = _recorder()
    moves(widget, QPoint(10, 10))
    assert calls == [(widget, QPoint(10, 10))]
