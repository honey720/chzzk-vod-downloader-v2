"""구간 창 순서 바꾸기 테스트 도우미 — 손잡이 끌기 · Alt+↑/↓ (#586).

끌기는 실제 포인터를 옮기지 않는다. 손잡이 위젯에 마우스 이벤트를 직접 보낸다 — 실제 화면에서 포인터 자리에
기대는 테스트는 다른 창 · 사람 손에 흔들린다(inbox 154 유형). 이벤트의 전역 위치는 위젯 좌표를 바꿔 만든다.
"""

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from pytestqt.qtbot import QtBot

LEFT = Qt.MouseButton.LeftButton
NO_BUTTON = Qt.MouseButton.NoButton
MOVE_WAIT_MS = 2000  # Alt+↑/↓ 옮기기를 기다리는 한도(ms) — 다음 차례 한 번이면 끝난다


def send_mouse(
    widget: QWidget,
    kind: QEvent.Type,
    global_pos: QPoint,
    button: Qt.MouseButton = LEFT,
    buttons: Qt.MouseButton = LEFT,
) -> None:
    """widget에 마우스 이벤트 하나를 직접 보낸다 — 지역 위치는 전역 위치에서 바꿔 넣는다."""
    local = QPointF(widget.mapFromGlobal(global_pos))
    event = QMouseEvent(
        kind, local, QPointF(global_pos), button, buttons, Qt.KeyboardModifier.NoModifier
    )
    QApplication.sendEvent(widget, event)


def press_at(widget: QWidget) -> QPoint:
    """widget 가운데를 왼쪽 버튼으로 누른다. 누른 전역 위치를 돌려준다."""
    start = widget.mapToGlobal(widget.rect().center())
    send_mouse(widget, QEvent.Type.MouseButtonPress, start)
    return start


def gap_point(dialog, gap: int) -> QPoint:
    """들어갈 틈(0 = 맨 위 행 앞 ~ 행 수 = 맨 끝)을 가리키는 전역 위치 — 테스트가 행 기하에서 따로 센다.

    틈이 행 앞이면 그 행의 위쪽 끝 바로 아래(행 가운데보다 위), 맨 끝이면 마지막 행 아래 끝 바로 위
    (가운데보다 아래)다. x는 목록 가운데다.
    """
    rows = dialog._rows
    container = dialog._rowContainer
    x = container.width() // 2
    if gap < len(rows):
        y = rows[gap].y() + 1
    else:
        y = rows[-1].y() + rows[-1].height() - 1
    return container.mapToGlobal(QPoint(x, y))


def drag_handle(dialog, source: int, gap: int | None = None, to: QPoint | None = None) -> None:
    """source 행의 손잡이를 끌어 gap 틈(또는 전역 위치 to)에 놓는다.

    누름 → 끌기 거리 넘게 한 번 움직임 → 놓을 자리로 움직임 → 놓기. 사이마다 이벤트를 처리한다.
    """
    handle = dialog._rows[source].handle
    assert handle.isVisible(), "전제: 손잡이가 보인다"
    start = press_at(handle)
    QApplication.processEvents()
    step = QApplication.startDragDistance() + 1
    send_mouse(handle, QEvent.Type.MouseMove, start + QPoint(0, step), NO_BUTTON)
    QApplication.processEvents()
    target = to if to is not None else gap_point(dialog, gap)
    send_mouse(handle, QEvent.Type.MouseMove, target, NO_BUTTON)
    QApplication.processEvents()
    send_mouse(handle, QEvent.Type.MouseButtonRelease, target, LEFT, NO_BUTTON)
    QApplication.processEvents()


def alt_move(
    qtbot: QtBot, widget: QWidget, key: Qt.Key, extra: Qt.KeyboardModifier | None = None
) -> None:
    """시각 칸 widget에 Alt+↑/↓(extra가 있으면 그 수정 키도 함께)를 보내고 옮기기가 끝날 때까지 기다린다.

    창은 키를 받은 다음 차례에 행을 옮긴다 — 받아 둔 키가 없어질 때까지 조건 대기한다(옮기지 않는
    키면 곧바로 끝난다). 옮긴 결과(초점 칸 · 행 순서)는 부른 테스트가 단언한다.
    """
    dialog = widget.window()
    assert hasattr(dialog, "isMovePending"), "전제: widget이 구간 창 안의 칸이다"
    modifiers = Qt.KeyboardModifier.AltModifier
    if extra is not None:
        modifiers |= extra
    QTest.keyClick(widget, key, modifiers)
    qtbot.waitUntil(lambda: not dialog.isMovePending(), timeout=MOVE_WAIT_MS)
