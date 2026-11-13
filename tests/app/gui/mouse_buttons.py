"""테스트가 앱에 남긴 마우스 버튼 눌림을 푸는 도우미 (#590).

위젯에 보내는 `QTest.mouseDClick`은 `MouseButtonDblClick` 이벤트 하나만 보내고 놓기를 보내지 않는다
(Qt 6 `qtestmouse.h`의 위젯 경로). Qt는 테스트가 보낸 spontaneous 마우스 이벤트로 앱 버튼 상태를
맞추고(`QGuiApplicationPrivate::captureGlobalModifierState` — `DblClick`에서 켜고 `Release`에서만 끈다),
QTest도 자기 버튼 상태(`QTestPrivate::qtestMouseButtons`)를 켠 채 둔다. 그래서 더블클릭 뒤
`QApplication.mouseButtons()`에 버튼이 남고, 뒤 테스트의 합성 포인터 이동이 끌기로 처리되어 호버가
켜지지 않는다. 창(`QWindow`) 경로 더블클릭은 누름 · 놓기 · 누름 · 놓기를 보내 버튼을 남기지 않는다.
"""

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

PRESSABLE = (
    Qt.MouseButton.LeftButton,
    Qt.MouseButton.RightButton,
    Qt.MouseButton.MiddleButton,
)  # 풀어 줄 버튼 — 테스트가 누르는 버튼


class _ReleaseSink(QWidget):
    """놓기 이벤트를 받기만 하는 보이지 않는 위젯 — 받지 않으면 QTest가 경고를 출력한다."""

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 — Qt 재정의
        """놓기를 받아 처리했다고 알린다."""
        event.accept()


def release_held_buttons() -> list[Qt.MouseButton]:
    """앱에 눌린 채 남은 마우스 버튼을 풀고, 푼 버튼을 돌려준다.

    놓기는 띄우지 않은 임시 위젯에 위젯 경로 `QTest.mouseRelease`로 보낸다. 이 경로가 앱 버튼 상태와
    QTest 버튼 상태를 함께 끈다. 테스트가 띄운 창 · 위젯에는 이벤트가 가지 않고, 실제 포인터와 Qt가
    기억하는 포인터 위치도 바뀌지 않는다. 남은 버튼이 없으면 아무것도 하지 않는다.
    """
    held = [button for button in PRESSABLE if QApplication.mouseButtons() & button]
    if not held:
        return []
    sink = _ReleaseSink()
    for button in held:
        QTest.mouseRelease(sink, button, Qt.KeyboardModifier.NoModifier, QPoint(0, 0))
    del sink  # 부모 없는 파이썬 소유 위젯이라 여기서 파괴된다 — 최상위 위젯 정리 단언에 남지 않는다
    return held


def release_left_by(nodeid: str) -> str | None:
    """테스트가 남긴 버튼을 풀고, 남겼으면 그 테스트 이름을 넣은 실패 메시지를 돌려준다.

    GUI 테스트 공용 픽스처가 테스트 끝에 부른다. 남은 버튼이 없으면 None이다.
    """
    if QApplication.instance() is None:
        return None
    held = release_held_buttons()
    if not held:
        return None
    names = ", ".join(button.name for button in held)
    return (
        f"테스트가 마우스 버튼을 눌린 채 남겼다 — {nodeid}: {names}. 위젯 대상 QTest.mouseDClick은 "
        "놓기를 보내지 않는다 — tests/app/gui/mouse_buttons.double_click을 쓴다(#590)"
    )


def double_click(widget: QWidget, pos: QPoint | None = None) -> None:
    """widget에 왼쪽 버튼 QTest 더블클릭을 보내고, QTest가 남긴 버튼 눌림을 푼다.

    widget이 받는 이벤트는 위젯 대상 `QTest.mouseDClick`과 같다(`MouseButtonDblClick` 하나). 마지막 놓기를
    widget에 보내면 카드가 그것을 클릭 놓기로 처리할 수 있어, 놓기는 `release_held_buttons`로 widget
    밖에 보낸다.

    Args:
        pos: widget 안 좌표 (기본값: widget 가운데 — QTest와 같다)
    """
    QTest.mouseDClick(
        widget,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        pos if pos is not None else QPoint(),
    )
    release_held_buttons()
