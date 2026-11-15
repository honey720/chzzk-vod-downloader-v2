"""카드 목록의 focus ring — 키보드 초점이 있는 행 안쪽 가장자리에 그리는 고리 하나 (#561 · #611).

카드 목록은 띠다(설계 §13) — 행끼리 붙어 있어 행 바깥에 고리를 띄울 자리가 없고, 바깥에 그리면 이웃 행을
덮는다. 그래서 고리는 행 안쪽 가장자리(구분선 위)에 굵기 `focusRingWidth`로 그린다. 초점 표시는 카드가 아니라
목록이 그린다 — 목록 하나에 위젯 하나다(카드마다 붙이지 않는다). 카드의 progress bar보다 위에 놓인다.
"""

from PySide6.QtCore import QEvent, QObject, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QWidget

import app.theme as theme

_FOLLOWED = (QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Hide)


class FocusRing(QWidget):
    """초점 행을 따라다니는 고리 — 카드 컨테이너의 자식이라 행과 함께 굴러가고 잘린다.

    행의 구분선 위 사각형과 같은 자리 · 크기로 놓이고, 그 안쪽 가장자리에 고리를 그린다. 마우스를 받지 않는다 —
    고리 자리의 누름 · 호버는 밑의 카드로 간다. 옮길 때 모션은 없다.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._target: QWidget | None = None  # 따라가는 카드 — 없으면 고리를 숨긴다
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.hide()

    def target(self) -> QWidget | None:
        """따라가는 카드 — 없으면 None."""
        return self._target

    def follow(self, card: QWidget | None) -> None:
        """고리가 따라갈 카드를 바꾼다 — None이면 숨긴다.

        이벤트 필터는 따라가는 카드에만, 따라가는 동안만 건다. 카드를 지우는 쪽은 지우기 전에
        `follow(None)`을 부른다 — 지워진 카드에서 필터를 떼지 못한다.
        """
        if card is self._target:
            return
        if self._target is not None:
            self._target.removeEventFilter(self)
        if card is not None:
            card.installEventFilter(self)
            self.raise_()  # 뒤에 들어온 카드보다 위에 — 따라갈 카드를 덮어 그린다
        self._target = card
        self.place()

    def place(self) -> None:
        """고리를 따라가는 카드의 지금 자리(구분선 위)에 놓는다 — 카드가 없거나 숨었으면 고리도 숨는다."""
        card = self._target
        if card is None or card.isHidden():
            self.hide()
            return
        rect = card.geometry().adjusted(0, 0, 0, -theme.METRICS["cardSeparator"])
        if rect != self.geometry():
            self.setGeometry(rect)
            self.update()
        if self.isHidden():
            self.show()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """따라가는 카드가 옮겨지거나 크기가 바뀌거나 숨으면 고리를 다시 놓는다."""
        if watched is self._target and event.type() in _FOLLOWED:
            self.place()
        return False

    def paintEvent(self, event: QPaintEvent) -> None:
        if self._target is None:
            return
        half = theme.METRICS["focusRingWidth"] / 2
        # 선 가운데가 지나는 사각형 — 행 가장자리에서 안쪽으로 굵기 절반
        line = QRectF(self.rect()).adjusted(half, half, -half, -half)
        painter = QPainter(self)
        painter.setPen(QPen(QColor(theme.current_tokens()["focusOnAccent"]), half * 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(line)
        painter.end()
