"""창 경로 합성 이동 앞 귀퉁이 이동 도우미 테스트 (#667)."""

from collections.abc import Iterator

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QWindow
from PySide6.QtWidgets import QApplication, QWidget

from tests.app.gui.pointer_moves import PointerMoves

WIDTH, HEIGHT = 300, 200  # 테스트 창 크기(px)


def _show(child_at: tuple[int, int, int, int]) -> QWidget:
    top = QWidget()
    top.resize(WIDTH, HEIGHT)
    child = QWidget(top)
    child.setGeometry(*child_at)
    top.show()
    QApplication.processEvents()
    return top


@pytest.fixture
def shown(qapp: QApplication) -> Iterator[QWindow]:
    """보이는 300×200 창의 `QWindow` — 자식 위젯 하나가 (40, 30, 100, 60)에 있다."""
    top = _show((40, 30, 100, 60))
    yield top.windowHandle()
    top.hide()


def _recorder() -> tuple[PointerMoves, list[tuple[object, QPoint]]]:
    calls: list[tuple[object, QPoint]] = []
    return PointerMoves(lambda target, pos=QPoint(), *rest: calls.append((target, pos))), calls


def test_first_move_into_a_window_goes_through_the_top_left_corner(shown: QWindow) -> None:
    """창의 첫 이동이면 목표와 다른 위젯이 있는 왼쪽 위 귀퉁이로 먼저 움직인 뒤 목표로 움직여야 한다.

    자식 위젯 안 (50, 40)으로 이동 -> 원래 이동 호출 (1, 1) · (50, 40)
    """
    moves, calls = _recorder()
    moves(shown, QPoint(50, 40))
    assert [pos for _, pos in calls] == [QPoint(1, 1), QPoint(50, 40)]


def test_a_different_point_in_the_same_window_is_moved_to_directly(shown: QWindow) -> None:
    """같은 창의 다른 자리로 움직이면 귀퉁이 이동 없이 바로 움직여야 한다.

    (50, 40) -> (80, 60) -> 둘째 이동의 원래 호출은 (80, 60) 하나
    """
    moves, calls = _recorder()
    moves(shown, QPoint(50, 40))
    calls.clear()
    moves(shown, QPoint(80, 60))
    assert [pos for _, pos in calls] == [QPoint(80, 60)]


def test_the_same_point_again_goes_through_a_corner(shown: QWindow) -> None:
    """바로 앞 이동과 같은 자리로 다시 움직이면 귀퉁이로 먼저 움직여야 한다.

    (50, 40) -> (50, 40) -> 둘째 이동의 원래 호출은 (1, 1) · (50, 40)
    """
    moves, calls = _recorder()
    moves(shown, QPoint(50, 40))
    calls.clear()
    moves(shown, QPoint(50, 40))
    assert [pos for _, pos in calls] == [QPoint(1, 1), QPoint(50, 40)]


def test_a_corner_on_the_goal_widget_is_skipped(qapp: QApplication) -> None:
    """왼쪽 위 귀퉁이가 목표와 같은 위젯 위면 그 귀퉁이를 건너뛰어야 한다.

    자식 위젯 (0, 0, 100, 60), 그 안 (50, 40)으로 첫 이동 -> 원래 호출 (298, 1) · (50, 40)
    """
    top = _show((0, 0, 100, 60))
    try:
        moves, calls = _recorder()
        moves(top.windowHandle(), QPoint(50, 40))
        assert [pos for _, pos in calls] == [QPoint(WIDTH - 2, 1), QPoint(50, 40)]
    finally:
        top.hide()


def test_a_corner_that_was_the_last_point_is_skipped(shown: QWindow, qapp: QApplication) -> None:
    """새 창의 첫 이동에서 왼쪽 위 귀퉁이가 바로 앞 이동의 화면 자리면 그 귀퉁이를 건너뛰어야 한다.

    같은 자리의 창 둘, 앞 창 (1, 1) 이동 -> 새 창 자식 위젯 안 (50, 40) 이동 -> 원래 호출 (298, 1) · (50, 40)
    """
    other = _show((40, 30, 100, 60))
    try:
        other.windowHandle().setPosition(shown.position())  # 두 창의 같은 자리가 같은 화면 좌표
        QApplication.processEvents()
        moves, calls = _recorder()
        moves(other.windowHandle(), QPoint(1, 1))
        calls.clear()
        moves(shown, QPoint(50, 40))
        assert [pos for _, pos in calls] == [QPoint(WIDTH - 2, 1), QPoint(50, 40)]
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
