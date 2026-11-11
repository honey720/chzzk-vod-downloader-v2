"""카드 목록의 focus ring — 키보드 초점이 있는 카드 바깥에 그리는 고리 하나 (#561).

카드 안에 초점 선을 그리면 고름 테두리와 같은 「카드를 두르는 선」으로 보이고 progress bar에 가려진다. 그래서
초점 표시는 카드가 아니라 목록이 카드 바깥에 그린다 — 목록 하나에 위젯 하나다(카드마다 붙이지 않는다).
"""

from PySide6.QtCore import QEvent, QObject, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPaintEvent, QPen
from PySide6.QtWidgets import QWidget

import app.theme as theme

# 따라가는 카드의 자리 · 크기 · 보임이 바뀌는 이벤트 — 이때만 고리를 다시 놓는다
_FOLLOWED = (QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Hide)
_SIDES = ("left", "top", "right", "bottom")  # 변을 세는 차례 — `_reach` · `attachedSides`


class FocusRing(QWidget):
    """초점 카드를 따라다니는 고리 — 카드 컨테이너의 자식이라 카드와 함께 굴러가고 잘린다.

    카드 바깥으로 `focusRingGap`만큼 띄우고 굵기 `focusRingWidth`로 그린다. 컨테이너 가장자리까지의 여유가
    그보다 작은 변에서는 띄우지 않고 카드 테두리에 붙여 그린다(여백은 바꾸지 않는다). 마우스를 받지 않는다 —
    고리 자리의 누름 · 호버는 밑의 카드로 간다. 옮길 때 모션은 없다.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._target: QWidget | None = None  # 따라가는 카드 — 없으면 고리를 숨긴다
        self._reach = (0, 0, 0, 0)  # 변마다 고리가 카드 밖으로 나간 px(`_SIDES` 차례)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.hide()

    def target(self) -> QWidget | None:
        """따라가는 카드 — 없으면 None."""
        return self._target

    def follow(self, card: QWidget | None) -> None:
        """고리가 따라갈 카드를 바꾼다 — None이면 숨긴다.

        이벤트 필터는 따라가는 카드와 컨테이너에만, 따라가는 동안만 건다. 카드를 지우는 쪽은 지우기 전에
        `follow(None)`을 부른다 — 지워진 카드에서 필터를 떼지 못한다.
        """
        if card is self._target:
            return
        host = self.parentWidget()
        if self._target is not None:
            self._target.removeEventFilter(self)
        elif card is not None:
            host.installEventFilter(self)
        if card is None:
            host.removeEventFilter(self)
        else:
            card.installEventFilter(self)
            self.raise_()  # 뒤에 들어온 카드보다 위에 — 따라갈 카드를 덮어 그린다
        self._target = card
        self.place()

    def attachedSides(self) -> tuple[str, ...]:
        """띄우지 못하고 카드 테두리에 붙여 그린 변 — 고리가 보일 때만 뜻이 있다."""
        full = theme.METRICS["focusRingGap"] + theme.METRICS["focusRingWidth"]
        return tuple(side for side, reach in zip(_SIDES, self._reach) if reach < full)

    def place(self) -> None:
        """고리를 따라가는 카드의 지금 자리에 놓는다 — 카드가 없거나 숨었으면 고리도 숨는다."""
        card = self._target
        if card is None or card.isHidden():
            self.hide()
            return
        host = self.parentWidget()
        box = card.geometry()
        width = theme.METRICS["focusRingWidth"]
        full = theme.METRICS["focusRingGap"] + width
        rooms = (
            box.x(),
            box.y(),
            host.width() - box.x() - box.width(),
            host.height() - box.y() - box.height(),
        )
        reach = tuple(full if room >= full else max(0, min(room, width)) for room in rooms)
        rect = box.adjusted(-reach[0], -reach[1], reach[2], reach[3])
        if reach != self._reach or rect != self.geometry():
            self._reach = reach
            self.setGeometry(rect)
            self.update()
        if self.isHidden():
            self.show()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """따라가는 카드가 옮겨지거나 숨으면, 컨테이너 크기가 바뀌면 고리를 다시 놓는다.

        컨테이너도 보는 까닭 — 스크롤바가 나타나면 카드는 그대로인데 오른쪽 여유만 줄어든다.
        """
        kind = event.type()
        if watched is self._target:
            if kind in _FOLLOWED:
                self.place()
        elif kind == QEvent.Type.Resize:
            self.place()
        return False

    def paintEvent(self, event: QPaintEvent) -> None:
        if self._target is None:
            return
        half = theme.METRICS["focusRingWidth"] / 2
        radius = theme.METRICS["cardRadius"]
        left, top, right, bottom = self._reach
        # 선의 가운데가 지나는 사각형 — 고리의 바깥 끝에서 굵기 절반 안쪽
        line = QRectF(self.rect()).adjusted(half, half, -half, -half)
        # 모서리는 카드 모서리와 중심이 같은 호 — 선 가운데가 카드 변에서 떨어진 만큼 반경이 커진다
        rx_left, rx_right = max(0.0, radius + left - half), max(0.0, radius + right - half)
        ry_top, ry_bottom = max(0.0, radius + top - half), max(0.0, radius + bottom - half)
        path = QPainterPath()
        path.moveTo(line.left() + rx_left, line.top())
        path.lineTo(line.right() - rx_right, line.top())
        path.arcTo(line.right() - 2 * rx_right, line.top(), 2 * rx_right, 2 * ry_top, 90, -90)
        path.lineTo(line.right(), line.bottom() - ry_bottom)
        path.arcTo(
            line.right() - 2 * rx_right,
            line.bottom() - 2 * ry_bottom,
            2 * rx_right,
            2 * ry_bottom,
            0,
            -90,
        )
        path.lineTo(line.left() + rx_left, line.bottom())
        path.arcTo(line.left(), line.bottom() - 2 * ry_bottom, 2 * rx_left, 2 * ry_bottom, 270, -90)
        path.lineTo(line.left(), line.top() + ry_top)
        path.arcTo(line.left(), line.top(), 2 * rx_left, 2 * ry_top, 180, -90)
        path.closeSubpath()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(theme.current_tokens()["focusOnAccent"]), half * 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.end()
