"""메뉴바의 메뉴 버튼 — 상태 버블과 깜빡이는 테두리를 얹는다 (#523, 설계 §2 · §12.1).

버블은 버튼 오른쪽 안쪽 여백(QSS `QPushButton#menuDownloadButton`의 `padding-right`)에 놓는다. 그 여백은
버블이 숨어도 그대로라 메뉴바가 움직이지 않는다. 버블 · 테두리는 마우스를 받지 않는 자식 위젯이 그린다 —
틱마다 `setStyleSheet`을 쓰지 않는다. 색은 테마 토큰으로만 고른다(이 파일에 색 리터럴이 없다).
"""

import math

from PySide6.QtCore import QRectF, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPen, QResizeEvent
from PySide6.QtWidgets import QPushButton, QWidget

import app.theme as theme
from app.widgets.icons import BUBBLE_FILL_TOKENS, BUBBLE_STATES, bubble_pixmap

#: 테두리 깜빡임 한 주기(ms) — 1.5 ~ 2초(초당 3번 넘게 번쩍이지 않는다, 설계 §12.1 · 프로토 12b).
BLINK_MS = 1800

#: 깜빡이는 테두리 두께(px) — 버튼 QSS 테두리(1px)와 같은 자리에 그린다.
BLINK_BORDER = 1


class StateBubble(QWidget):
    """버블 하나 — 상태 색 원 안에 상태 기호. 마우스를 받지 않는다(툴팁은 버튼이 든다)."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        side = theme.METRICS["menuBubble"]
        self.setFixedSize(side, side)
        self._state = ""
        self.hide()

    def state(self) -> str:
        """그리는 상태 — 숨었으면 빈 글."""
        return self._state

    def setState(self, state: str) -> None:
        """상태를 바꾼다 — 빈 글이면 숨는다. 같은 상태면 아무것도 하지 않는다."""
        if state and state not in BUBBLE_STATES:
            raise ValueError(f"알 수 없는 버블 상태: {state!r}")
        if state == self._state:
            return
        self._state = state
        self.setVisible(bool(state))
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        """지금 테마의 상태 색으로 버블을 그린다 — 테마가 바뀌면 다음 그리기에 따라간다."""
        if not self._state:
            return
        tokens = theme.current_tokens()
        pixmap = bubble_pixmap(
            self._state,
            tokens[BUBBLE_FILL_TOKENS[self._state]],
            tokens["onAccent"],
            self.width(),
            self.devicePixelRatioF(),
        )
        painter = QPainter(self)
        painter.drawPixmap(0, 0, pixmap)


class BlinkBorder(QWidget):
    """버튼 테두리 자리에 그리는 강조 테두리 — 세기(0 ~ 1)만큼 강조 색이 짙어진다. 마우스를 받지 않는다."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._level = 0.0
        self.hide()

    def level(self) -> float:
        """지금 세기."""
        return self._level

    def setLevel(self, level: float) -> None:
        """세기를 바꾸고 다시 그린다."""
        self._level = level
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        """강조 색(초점 색 — 설계 §12.1)에 세기만큼의 불투명도로 둥근 테두리를 그린다."""
        if self._level <= 0:
            return
        color = QColor(theme.current_tokens()["accent"])
        color.setAlphaF(min(1.0, self._level))
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(color, BLINK_BORDER))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        half = BLINK_BORDER / 2
        radius = theme.METRICS["radius"]
        painter.drawRoundedRect(
            QRectF(self.rect()).adjusted(half, half, -half, -half), radius, radius
        )


class MenuButton(QPushButton):
    """상태 버블 · 깜빡이는 테두리를 얹은 메뉴 버튼."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.bubble = StateBubble(self)
        self.blinkBorder = BlinkBorder(self)
        self._blink = QVariantAnimation(self)
        self._blink.setStartValue(0.0)
        self._blink.setEndValue(1.0)
        self._blink.setDuration(BLINK_MS)
        self._blink.setLoopCount(-1)
        self._blink.valueChanged.connect(self._onBlinkStep)

    def setBubble(self, state: str) -> None:
        """버블 상태를 바꾼다 — 빈 글이면 숨는다(자리는 QSS 여백이 그대로 둔다)."""
        self.bubble.setState(state)

    def isBlinking(self) -> bool:
        """테두리가 깜빡이는 중인지."""
        return self._blink.state() == QVariantAnimation.State.Running

    def startBlink(self) -> None:
        """테두리 깜빡임을 시작한다 — 이미 깜빡이면 그대로 둔다."""
        if self.isBlinking():
            return
        self.blinkBorder.setGeometry(self.rect())
        self.blinkBorder.show()
        self.blinkBorder.raise_()
        self._blink.start()

    def stopBlink(self) -> None:
        """테두리 깜빡임을 멈추고 테두리를 지운다."""
        self._blink.stop()
        self.blinkBorder.setLevel(0.0)
        self.blinkBorder.hide()

    def _onBlinkStep(self, progress: float) -> None:
        """한 주기 안의 자리(0 ~ 1) → 세기 — 0에서 시작해 가운데에서 가장 짙고 끝에서 다시 0(코사인)."""
        self.blinkBorder.setLevel(0.5 - 0.5 * math.cos(2 * math.pi * progress))

    def resizeEvent(self, event: QResizeEvent) -> None:
        """버블을 오른쪽 안쪽 여백(기본 여백 안쪽)에, 테두리를 버튼 전체에 둔다."""
        super().resizeEvent(event)
        side = self.bubble.width()
        x = self.width() - theme.METRICS["buttonPadX"] - side
        self.bubble.move(x, (self.height() - side) // 2)
        self.blinkBorder.setGeometry(self.rect())
