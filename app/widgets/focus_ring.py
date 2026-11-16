"""키보드 초점 표시 — 카드 목록의 focus ring과 바 컨트롤의 키보드 초점 표시 (#561 · #611).

카드 목록의 focus ring은 키보드 초점이 있는 행 안쪽 가장자리에 그리는 고리 하나다(`FocusRing`). 바
컨트롤은 받은 초점이 키보드로 왔을 때만 강조색 테두리를 그린다(`KeyFocusMarker`).

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


# 키보드로 받은 초점 — 이 까닭으로 받은 초점만 강조색 테두리를 그린다(Tab · Shift+Tab · 단축키)
_KEY_REASONS = (
    Qt.FocusReason.TabFocusReason,
    Qt.FocusReason.BacktabFocusReason,
    Qt.FocusReason.ShortcutFocusReason,
)
# 받기 전 종류를 그대로 두는 초점 변화 — 창을 오감 · 펼친 메뉴가 닫혀 초점이 돌아옴
_KEEP_REASONS = (Qt.FocusReason.ActiveWindowFocusReason, Qt.FocusReason.PopupFocusReason)


class KeyFocusMarker(QObject):
    """바 컨트롤이 받은 초점이 키보드로 왔는지를 `keyFocus` 동적 속성("true" · "false")으로 적는다 (#611).

    바 버튼 · 드롭다운은 마우스로 누르면 초점을 받는다(Windows 콤보박스와 같음, 오너 재실기 2026-11-16) — 누르는
    순간 URL 칸 초점이 풀린다. 강조색 focus ring은 키보드로 받은 초점에만 그린다: 전역 QSS가
    `[keyFocus="false"]:focus`를 평소 테두리로 되돌린다. 창을 오가거나 펼친 메뉴가 닫혀 초점이 돌아올 때는 받기
    전 종류를 그대로 둔다 — 마우스로 연 드롭다운은 메뉴가 닫힌 뒤에도 강조색이 없고, 키보드로 연 것은 남는다.

    위젯마다 건다(`watch`) — 앱 전체 필터가 아니다.
    """

    def watch(self, widget: QWidget) -> None:
        """그 위젯의 초점 들어옴 · 나감을 지켜본다 — 처음에는 키보드 초점이 아닌 것으로 둔다.

        속성을 심은 뒤 스타일을 다시 계산한다 — 크기를 재느라 이미 polish된 위젯(아이콘 버튼)은 새 속성 선택자를
        다시 계산하기 전까지 모른다.
        """
        widget.setProperty("keyFocus", False)
        theme.repolish(widget)
        widget.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        kind = event.type()
        if kind not in (QEvent.Type.FocusIn, QEvent.Type.FocusOut) or not watched.isWidgetType():
            return False
        reason = event.reason()
        if reason in _KEEP_REASONS:
            return False
        keyboard = kind == QEvent.Type.FocusIn and reason in _KEY_REASONS
        if watched.property("keyFocus") is not keyboard:
            watched.setProperty("keyFocus", keyboard)
            theme.repolish(watched)
        return False
