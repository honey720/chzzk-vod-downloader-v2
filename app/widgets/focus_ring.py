"""키보드 초점 표시 — 카드 목록의 focus ring과 바 컨트롤의 키보드 초점 표시 (#561 · #611).

카드 목록의 focus ring은 키보드 초점이 있는 행 안쪽 가장자리에 그리는 고리 하나다(`FocusRing`). 바
컨트롤은 받은 초점이 키보드로 왔을 때만 강조색 테두리를 그린다(`KeyFocusMarker`). 초점을 쥔 위젯이 꺼지거나
숨어서 Qt가 초점을 저절로 넘기면 받은 쪽은 그 전 초점의 종류를 이어받는다(`FocusHandover`) — 둘이 같은 판단을 쓴다.

카드 목록은 띠다(설계 §13) — 행끼리 붙어 있어 행 바깥에 고리를 띄울 자리가 없고, 바깥에 그리면 이웃 행을
덮는다. 그래서 고리는 행 안쪽 가장자리(구분선 위)에 굵기 `focusRingWidth`로 그린다. 초점 표시는 카드가 아니라
목록이 그린다 — 목록 하나에 위젯 하나다(카드마다 붙이지 않는다). 카드의 progress bar보다 위에 놓인다.
"""

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QTimer
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
# Qt가 꺼지거나 숨은 위젯의 초점을 다음 위젯으로 넘길 때 붙이는 까닭(`QWidget.focusNextPrevChild`)
_STEP_REASONS = (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason)


class FocusHandover:
    """Qt가 저절로 넘긴 초점이 그 전 초점의 종류(마우스 · 키보드)를 이어받게 한다 (#611 176n).

    초점을 쥔 위젯이 꺼지거나 숨으면 Qt는 초점 사슬의 다음 위젯에 Tab 까닭으로 초점을 준다. 창이 비활성인
    동안(확인 대화상자가 떠 있는 동안) 꺼지면 이벤트 없이 창의 초점 위젯만 바뀌고, 창이 다시 활성이 될 때 그
    위젯이 창 활성 까닭으로 초점을 받는다. 둘 다 유저가 Tab을 누른 것이 아니다 — 마우스로 ■을 눌러 멈췄는데
    다음 카드에 focus ring이 생겼다(오너 재실기 2026-11-17). 그래서 받은 쪽은 넘겨준 쪽의 종류를 따른다.

    `KeyFocusMarker`(바 컨트롤)와 카드(`ContentItemWidget`)가 초점이 나갈 때 `left`, 들어올 때 `arrived`를
    부른다. 상태는 모듈에 하나다 — 앱의 활성 창은 한 번에 하나라 창마다 나눌 필요가 없다.
    """

    def __init__(self) -> None:
        self._stepped: bool | None = (
            None  # 지금 넘어가는 초점의 종류 — 넘김이 끝나는 이벤트 루프 턴에 지운다
        )
        self._away: tuple[QWidget, bool] | None = (
            None  # 창이 비활성이 될 때 초점을 쥔 위젯의 창 · 종류
        )

    def left(self, widget: QWidget, keyboard: bool, reason: Qt.FocusReason) -> None:
        """위젯에서 초점이 나갔다 — 넘김이면 그 종류를 적어 둔다.

        창이 비활성이 되어 나갔으면 창과 종류를 적는다. 꺼지거나 숨어서 Tab 까닭으로 나갔으면 이번 넘김의
        종류를 적고, 넘김이 끝난 뒤(다음 이벤트 루프 턴) 지운다 — 같은 호출 안에서 받은 쪽이 읽는다.
        """
        if reason == Qt.FocusReason.ActiveWindowFocusReason:
            self._away = (widget.window(), keyboard)
        elif reason in _STEP_REASONS and not (widget.isEnabled() and widget.isVisible()):
            if self._stepped is None:
                QTimer.singleShot(0, self._endStep)
            self._stepped = keyboard

    def arrived(self, widget: QWidget, reason: Qt.FocusReason) -> bool | None:
        """위젯이 초점을 받았다 — 넘겨받은 초점이면 이어받을 종류(키보드면 참), 아니면 None.

        창 활성 까닭이면 그 창이 비활성이 될 때 적은 종류를 한 번만 돌려준다. Tab 까닭이면 지금 넘어가는
        초점의 종류다(목록이 보이는 차례의 첫 카드로 다시 옮기는 것도 같은 넘김이다).
        """
        if reason == Qt.FocusReason.ActiveWindowFocusReason:
            away, self._away = self._away, None
            if away is not None and away[0] is widget.window():
                return away[1]
            return None
        if reason in _STEP_REASONS:
            return self._stepped
        return None

    def _endStep(self) -> None:
        self._stepped = None


HANDOVER = FocusHandover()  # 바 컨트롤 · 카드가 함께 쓰는 넘김 기록


def paint_lifted_ring(widget: QWidget) -> None:
    """고른 면 위의 키보드 focus ring을 면에서 1px 띄워 그린다 (#611 176u).

    고른 칩 · 켠 선택 버튼의 면(barSelected)은 강조색과 대비가 낮아 면 위 테두리가 묻힌다. 그래서 위젯
    가장자리 1px에 강조색 테두리, 그 안 1px에 바 면(barBg) 간격을 그린다 — 테두리 양옆이 바 면이다. 면은 간격
    안쪽으로 줄어 보이고 위젯 크기 · 글 자리 · 이웃과의 간격은 그대로다. 면을 QSS가 칠한 뒤 부른다(QSS 테두리는
    그 상태에서 끈다). 키보드로 받은 초점일 때만 그린다 — 마우스 초점은 강조가 없다(`KeyFocusMarker`).
    """
    if not widget.hasFocus() or widget.property("keyFocus") is False:
        return
    tokens = theme.current_tokens()
    radius = theme.METRICS["controlRadius"]
    painter = QPainter(widget)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    # 선 가운데가 지나는 사각형 — 바깥 테두리는 가장자리에서 0.5, 간격은 1.5 안쪽
    for inset, token in ((0.5, "accent"), (1.5, "barBg")):
        painter.setPen(QPen(QColor(tokens[token]), 1))
        line = QRectF(widget.rect()).adjusted(inset, inset, -inset, -inset)
        painter.drawRoundedRect(line, radius - inset, radius - inset)
    painter.end()


class KeyFocusMarker(QObject):
    """바 컨트롤이 받은 초점이 키보드로 왔는지를 `keyFocus` 동적 속성("true" · "false")으로 적는다 (#611).

    바 버튼 · 드롭다운은 마우스로 누르면 초점을 받는다(Windows 콤보박스와 같음, 오너 재실기 2026-11-16) — 누르는
    순간 URL 칸 초점이 풀린다. 강조색 focus ring은 키보드로 받은 초점에만 그린다: 전역 QSS가
    `[keyFocus="false"]:focus`를 평소 테두리로 되돌린다. 창을 오가거나 펼친 메뉴가 닫혀 초점이 돌아올 때는 받기
    전 종류를 그대로 둔다 — 마우스로 연 드롭다운은 메뉴가 닫힌 뒤에도 강조색이 없고, 키보드로 연 것은 남는다.
    꺼지거나 숨은 위젯에서 저절로 넘어온 초점은 그 전 초점의 종류를 따른다(`HANDOVER`, #611 176n).

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
        if kind == QEvent.Type.FocusOut:
            HANDOVER.left(watched, watched.property("keyFocus") is True, reason)
            if reason in _KEEP_REASONS:
                return False
            keyboard = False
        else:
            inherited = HANDOVER.arrived(watched, reason)
            if inherited is not None:
                keyboard = inherited
            elif reason in _KEEP_REASONS:
                return False
            else:
                keyboard = reason in _KEY_REASONS
        if watched.property("keyFocus") is not keyboard:
            watched.setProperty("keyFocus", keyboard)
            theme.repolish(watched)
        return False
