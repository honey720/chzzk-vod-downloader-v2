"""구간 창 순서 바꾸기 테스트 도우미 — 손잡이 끌기 · Alt+↑/↓ (#586).

끌기는 실제 포인터를 옮기지 않는다. 손잡이 위젯에 마우스 이벤트를 직접 보낸다 — 실제 화면에서 포인터 자리에
기대는 테스트는 다른 창 · 사람 손에 흔들린다(inbox 154 유형). 이벤트의 전역 위치는 위젯 좌표를 바꿔 만든다.
"""

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

LEFT = Qt.MouseButton.LeftButton
NO_BUTTON = Qt.MouseButton.NoButton


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


PRESSABLE = (
    Qt.MouseButton.LeftButton,
    Qt.MouseButton.RightButton,
    Qt.MouseButton.MiddleButton,
)  # 풀어 줄 버튼 — 테스트가 누르는 버튼


def release_buttons(window: QWidget) -> None:
    """앱에 눌린 채 남은 마우스 버튼을 window에 합성 놓기로 풀고, 버튼 없음을 단언한다.

    앞 테스트(위젯에 보낸 QTest 더블클릭 등)가 버튼을 눌린 상태로 남기면 뒤 테스트의 합성 포인터 이동이
    끌기가 되어 호버가 켜지지 않는다(#586). 놓기는 창(QWindow) 왼쪽 위 (0, 0)에 보낸다 — 실제 포인터는
    옮기지 않는다. (1, 1)에 놓으면 뒤에 (1, 1)을 거치는 첫 합성 이동이 같은 자리라 버려져 호버가 켜지지
    않았다(로컬 재현). 호버를 재는 테스트는 창을 연 직후에 부른다.
    """
    handle = window.windowHandle()
    assert handle is not None, "전제: 창이 떠 있다"
    for button in PRESSABLE:
        if QApplication.mouseButtons() & button:
            QTest.mouseRelease(handle, button, Qt.KeyboardModifier.NoModifier, QPoint(0, 0))
            QApplication.processEvents()
    assert QApplication.mouseButtons() == NO_BUTTON, (
        f"전제: 눌린 마우스 버튼이 없다 — {QApplication.mouseButtons()!r}"
    )


def focused(window: QWidget) -> QWidget:
    """window 안에서 초점이 있는 위젯을 돌려준다. 없으면 키를 보내기 전에 테스트를 실패시킨다.

    앱 전체 초점(``QApplication.focusWidget()``)은 창이 활성일 때만 그 창의 위젯이다. 공유 화면에서 창이
    활성을 잃으면 None이 되고, 활성 창도 없을 때 None에 키를 보내면 QTest가 프로세스를 abort한다(#586). 창의 초점 위젯
    (``window.focusWidget()``)은 활성과 상관없이 그 창에서 초점을 받을 위젯이다.
    """
    widget = window.focusWidget()
    assert widget is not None, f"{type(window).__name__}에 초점 위젯이 없다 — 키를 보내지 않는다"
    return widget


def alt_move(widget: QWidget, key: Qt.Key, extra: Qt.KeyboardModifier | None = None) -> None:
    """시각 칸 widget에 Alt+↑/↓(extra가 있으면 그 수정 키도 함께)를 보낸다.

    widget이 None이면 키를 보내지 않고 실패한다 — QTest는 대상이 None이면 앱 전체 초점 · 활성 창 차례로
    보내고(다른 창이 활성이면 그 창이 키를 받는다), 둘 다 없으면 프로세스를 abort한다(#586).
    """
    assert widget is not None, "키를 보낼 위젯이 None이다 — 키를 보내지 않는다"
    modifiers = Qt.KeyboardModifier.AltModifier
    if extra is not None:
        modifiers |= extra
    QTest.keyClick(widget, key, modifiers)
    QApplication.processEvents()
