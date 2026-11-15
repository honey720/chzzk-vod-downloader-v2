"""직접 그리는 말풍선(툴팁) — 모서리 · 꼬리 · 그림자, 대상 옆에 바로 뜬다 (#536 — 설계 §2 「말풍선」 · §12.1).

Qt 기본 툴팁(`QToolTip`)은 기다림이 있고, 대상을 가릴 수 있고, 모양을 QSS로 다 바꿀 수 없다. 이 모듈은
말풍선 하나(`TipBubble`)를 두고, 위젯마다 거는 이벤트 필터(`TipFilter`)가 들어감 · 나감 · 누름을 보고
그 위젯의 `toolTip()`을 띄우거나 닫는다. Qt의 툴팁 이벤트는 삼켜 기본 툴팁이 함께 뜨지 않게 한다.

- 방향: 창이 알려 주는 경계(`tipBoundary()` — 헤더바 아래 끝, 전역 y)보다 위에 있는 대상은 아래로, 그 밖은
  위로. 경계가 없는 창(대화상자 등)은 아래로. 화면 밖이면 반대쪽, 그래도 안 되면 옆, 마지막에 화면 안으로 민다
- 대상과 꼬리 끝 사이는 `tipGap` — 대상 사각형과 겹치지 않는다
- 들어오면 바로(0ms) 뜨고, 나가거나 누르거나 대상이 숨으면 바로 닫힌다. 누른 뒤에는 나갈 때까지 다시 띄우지
  않는다. 드롭다운 메뉴가 열려 있는 동안에는 띄우지 않는다
- `install_tips(..., delayed=True)`로 건 위젯은 `HOVER_TIP_DELAY_MS` 머문 뒤에 뜬다 — 카드 위 툴팁(버튼 ·
  채널명 · 상태 글 · 저장 위치)이 쓴다(오너 2026-11-15 「카드에 툴팁이 뜨니까 지저분하다」, #599).
  그 전에 나가거나 누르거나 숨으면 뜨지 않는다. 다른 버튼으로 옮기면 지연을 다시 건다 — 버튼 줄을
  훑고 지나갈 때도 뜨지 않게
- 나타날 때 `TIP_FADE_IN_MS` 동안 투명에서 불투명으로(처음 빠르고 끝 느림), 닫힐 때 `TIP_FADE_OUT_MS` 동안
  사라진다(오너 2026-11-15, #610). 사라지는 중에 새 툴팁이 뜨면 사라지던 것은 바로 지우고 새 것만 나타난다
  (말풍선은 하나라 그 자리에서 다시 0부터 나타난다). 설정의 애니메이션 끔(`card_display` `animations`)이면 바로
  뜨고 바로 사라진다. 페이드는 창 불투명도가 아니라 그리기 알파다 — 등록 오류 말풍선은 본 창의 자식 위젯이라
  창 불투명도가 듣지 않는다
- 필터는 앱 전체가 아니라 위젯마다 건다(`install_tips`) — 앱 전체에 거는 파이썬 이벤트 필터는 모든 위젯의 모든
  이벤트를 파이썬으로 끌어와 느리고, 전체 테스트에서 접근 위반으로 죽은 적이 있다

색은 그릴 때마다 theme 토큰을 읽는다(실행 중 테마 전환 #247). 이 파일에 색 리터럴은 없다.
"""

import html

import shiboken6
from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPoint,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
)
from PySide6.QtGui import (
    QAbstractTextDocumentLayout,
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPalette,
    QPen,
    QTextDocument,
    QTextOption,
)
from PySide6.QtWidgets import QApplication, QWidget

import app.theme as theme
from app.viewmodels import card_display

_FLIP = {"below": "above", "above": "below", "left": "right", "right": "left"}
_SIDEWAYS = {
    "below": ("right", "left"),
    "above": ("right", "left"),
    "left": ("below", "above"),
    "right": ("below", "above"),
}
_FACING = {"below": "top", "above": "bottom", "right": "left", "left": "right"}  # 꼬리가 나는 쪽
_TIP_WEIGHT = 600  # 말풍선 글 굵기 — 굵게(설계 §12.1 「툴팁 글은 굵게」)
_SHADOW_ALPHA = 0.05  # 그림자 가장 안쪽 한 겹의 불투명도 — 바깥으로 갈수록 옅다
# 머무를 때만 뜨는 툴팁의 지연(ms) — 카드 위 툴팁(`install_tips(..., delayed=True)`). 포인터가 지나갈 때는 안 뜨고
# 머무를 때만 뜨는 값으로 오너가 정했다(2026-11-15 「약 600ms」, #599). 그 밖의 툴팁은 0ms
HOVER_TIP_DELAY_MS = 600
# 말풍선이 나타날 때 투명 → 불투명(ms, ease-out) · 닫힐 때 불투명 → 투명(ms) — 오너 2026-11-15(약 120 · 약 80,
# #610). 0이면 바로 뜨고 바로 사라진다. 상수는 쓸 때마다 읽는다(테스트가 줄인다)
TIP_FADE_IN_MS = 120
TIP_FADE_OUT_MS = 80
KEY_SEP = "\t"  # 툴팁 글의 이름 ↔ 단축키 구분 — 말풍선이 「이름 · 단축키」로 그리고 단축키는 흐린 색(#536)
# 말풍선 색 묶음 — (면, 테두리, 글) 토큰. 툴팁은 바보다 한 단계 떠 있는 면 · 진한 테두리(프로토 demoFloat),
# 등록 오류는 실패 색 계열(설계 §2 알림 자리, #536)
TONES = {
    "tip": ("tipBg", "borderStrong", "text"),
    "error": ("errorTipBg", "stateFailed", "text"),
}


def shortcut_tip(name: str, key: str) -> str:
    """「이름 · 단축키」 툴팁 글 — 말풍선이 단축키를 흐린 색으로 그린다(프로토 「등록 · Enter」, #536).

    Qt 기본 툴팁으로 뜨는 자리(말풍선 필터가 없는 창)에서는 구분 문자가 빈칸으로 보인다.
    """
    return f"{name}{KEY_SEP}{key}"


def tip_rect(target: QRect, size: QSize, side: str, screen: QRect) -> tuple[QRect, str]:
    """말풍선 자리(전역 좌표)와 실제 방향 — 대상 옆 `tipGap`, 대상 가운데에 맞춘다.

    바라는 방향(`side`)이 화면 밖이면 반대쪽, 그래도 안 되면 다른 축, 다 안 되면 바라는 방향 그대로. 마지막에
    가로지르는 축으로만 화면 안으로 민다 — 대상 옆 자리(주 축)는 그대로라 대상과 겹치지 않는다.
    """
    gap = theme.METRICS["tipGap"]

    def place(where: str) -> QRect:
        if where == "below":
            x, y = target.center().x() - size.width() // 2, target.bottom() + 1 + gap
        elif where == "above":
            x, y = target.center().x() - size.width() // 2, target.top() - gap - size.height()
        elif where == "right":
            x, y = target.right() + 1 + gap, target.center().y() - size.height() // 2
        else:
            x, y = target.left() - gap - size.width(), target.center().y() - size.height() // 2
        return QRect(x, y, size.width(), size.height())

    def fits(rect: QRect, where: str) -> bool:
        if where in ("below", "above"):
            return rect.top() >= screen.top() and rect.bottom() <= screen.bottom()
        return rect.left() >= screen.left() and rect.right() <= screen.right()

    chosen = side
    rect = place(side)
    for where in (side, _FLIP[side], *_SIDEWAYS[side]):
        candidate = place(where)
        if fits(candidate, where):
            chosen, rect = where, candidate
            break
    if chosen in ("below", "above"):
        rect.moveLeft(max(screen.left(), min(rect.left(), screen.right() - rect.width() + 1)))
    else:
        rect.moveTop(max(screen.top(), min(rect.top(), screen.bottom() - rect.height() + 1)))
    return rect, chosen


def bubble_outline(
    body: QRectF, radius: float, facing: str, center: float, half: float, length: float
) -> QPainterPath:
    """몸통 둥근 사각형과 꼬리를 한 윤곽으로 그린 경로 — 몸통 둘레를 시계 방향으로 돌며 꼬리 변에 꼬리를 끼운다.

    몸통과 꼬리를 따로 만들어 `QPainterPath.united()`로 합치면 두 도형이 꼬리 밑변에서 넓이 0으로 맞닿기만
    하므로 합쳐지지 않고 두 subpath로 남는다. 그러면 테두리 펜이 꼬리 밑변까지 그어 몸통과 꼬리 사이에 줄이
    생긴다(#606). 한 윤곽이면 꼬리 밑변은 경로에 없다.

    Args:
        facing: 꼬리가 나는 변 — "top" · "bottom" · "left" · "right"
        center: 꼬리 가운데 위치(그 변을 따라가는 축, 위젯 좌표)
        half: 꼬리 밑변 폭의 절반
        length: 꼬리 길이(밑변에서 끝까지)
    """
    left, top, right, bottom = body.left(), body.top(), body.right(), body.bottom()
    diameter = 2 * radius
    path = QPainterPath()
    path.moveTo(left + radius, top)
    if facing == "top":
        path.lineTo(center - half, top)
        path.lineTo(center, top - length)
        path.lineTo(center + half, top)
    path.lineTo(right - radius, top)
    path.arcTo(QRectF(right - diameter, top, diameter, diameter), 90, -90)
    if facing == "right":
        path.lineTo(right, center - half)
        path.lineTo(right + length, center)
        path.lineTo(right, center + half)
    path.lineTo(right, bottom - radius)
    path.arcTo(QRectF(right - diameter, bottom - diameter, diameter, diameter), 0, -90)
    if facing == "bottom":
        path.lineTo(center + half, bottom)
        path.lineTo(center, bottom + length)
        path.lineTo(center - half, bottom)
    path.lineTo(left + radius, bottom)
    path.arcTo(QRectF(left, bottom - diameter, diameter, diameter), 270, -90)
    if facing == "left":
        path.lineTo(left, center + half)
        path.lineTo(left - length, center)
        path.lineTo(left, center - half)
    path.lineTo(left, top + radius)
    path.arcTo(QRectF(left, top, diameter, diameter), 180, -90)
    path.closeSubpath()
    return path


def tip_side(target: QWidget) -> str:
    """바라는 방향 — 창의 경계(`tipBoundary()`, 전역 y)보다 대상 가운데가 위면 아래로, 아니면 위로.

    경계를 알려 주지 않는 창(대화상자 · 메뉴)은 아래로.
    """
    boundary = getattr(target.window(), "tipBoundary", None)
    if boundary is None:
        return "below"
    center = target.mapToGlobal(QPoint(0, target.height() // 2)).y()
    return "below" if center < boundary() else "above"


class TipBubble(QWidget):
    """말풍선 창 하나 — 모서리 · 대상 쪽 꼬리 · 그림자 · 굵은 글. 포인터를 받지 않고 활성을 빼앗지 않는다.

    꼬리 쪽에는 그림자 자리를 두지 않는다 — 창 사각형의 꼬리 쪽 끝이 곧 대상에서 `tipGap` 떨어진 자리다.
    색 묶음(`tone`)은 툴팁(`"tip"`)과 등록 오류(`"error"` — 실패 색 계열, #536)다(`TONES`).

    `embedded=True`면 따로 뜬 창이 아니라 `parent`의 자식 위젯으로 그린다 — 자리는 화면 대신 `parent` 사각형
    안에서 잡는다. 따로 뜬 최상위 창은 본 창을 Alt+Tab으로 뒤로 보내도 화면 맨 위에 남는다(#536 오너 실기).
    """

    def __init__(
        self, tone: str = "tip", parent: QWidget | None = None, embedded: bool = False
    ) -> None:
        if embedded:
            super().__init__(parent)
        else:
            super().__init__(
                parent,
                Qt.WindowType.ToolTip
                | Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.NoDropShadowWindowHint
                | Qt.WindowType.WindowTransparentForInput,
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setObjectName("tipBubble")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        # 창 안 자식으로 그리는지 — 등록 오류는 본 창과 함께 뒤로 가고 · 최소화되고 · 움직인다(#536 오너 실기)
        self.embedded = embedded
        self.tone = tone  # 색 묶음 — TONES의 키
        self._doc = QTextDocument(self)
        self._doc.setDocumentMargin(0)
        # 빈칸 없는 긴 글(등록 오류의 긴 주소)도 폭 상한에서 줄을 바꾼다 — 상한을 넘겨 그리지 않는다
        option = self._doc.defaultTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self._doc.setDefaultTextOption(option)
        self.side = "below"  # 지금 방향(대상에서 말풍선이 있는 쪽)
        self.target: QWidget | None = None  # 지금 띄운 대상
        self.text = ""  # 지금 띄운 글
        self.targetRect = QRect()  # 대상 사각형(전역) — 시험이 겹침을 잰다
        self._tail = 0  # 꼬리 끝 위치(가로지르는 축, 위젯 좌표)
        self._opacity = 1.0  # 그리는 불투명도 0 ~ 1 — 페이드 동안만 1보다 작다(#610)
        self._fade: QVariantAnimation | None = None  # 페이드 애니메이션 — 움직이는 동안만 있다
        self._fadingOut = False  # 닫히는 페이드 중인지 — 끝나면 숨는다

    def showing(self, target: QWidget, text: str) -> bool:
        """그 대상에 그 글로 이미 떠 있는지 — 사라지는 중이면 떠 있지 않은 것으로 본다."""
        return (
            self.isVisible() and not self._fadingOut and self.target is target and self.text == text
        )

    def opacity(self) -> float:
        """지금 그리는 불투명도(0 ~ 1)."""
        return self._opacity

    def fade(self) -> QVariantAnimation | None:
        """움직이는 페이드 애니메이션(없으면 None)."""
        return self._fade

    def isFadingOut(self) -> bool:
        """닫히는 페이드 중인지."""
        return self._fadingOut

    def margins(self) -> tuple[int, int, int, int]:
        """(왼, 위, 오른, 아래) 여백 — 꼬리 쪽은 꼬리, 나머지는 그림자 자리."""
        shadow, tail = theme.METRICS["tipShadow"], theme.METRICS["tipTail"]
        sides = {"left": shadow, "top": shadow, "right": shadow, "bottom": shadow}
        sides[_FACING[self.side]] = tail
        return sides["left"], sides["top"], sides["right"], sides["bottom"]

    def _setText(self, text: str) -> None:
        font = QFont(self.font())
        font.setPixelSize(theme.METRICS["tipFontPx"])
        font.setWeight(QFont.Weight(_TIP_WEIGHT))
        self._doc.setDefaultFont(font)
        name, sep, key = text.partition(KEY_SEP)
        if sep:
            # 「이름 · 단축키」 — 단축키는 흐린 색(그릴 때의 토큰 — 띄울 때마다 다시 쓴다)
            muted = theme.current_tokens()["textMuted"]
            self._doc.setHtml(
                f"{html.escape(name)} · <span style='color:{muted};'>{html.escape(key)}</span>"
            )
        else:
            self._doc.setPlainText(text)
        self._doc.setTextWidth(-1)
        if self._doc.idealWidth() > theme.METRICS["tipMaxWidth"]:
            self._doc.setTextWidth(theme.METRICS["tipMaxWidth"])

    def shownText(self) -> str:
        """지금 그리는 글(꾸밈 없이) — 「이름 · 단축키」 꼴도 이어 붙인 글로 돌려준다."""
        return self._doc.toPlainText()

    def _bodySize(self) -> QSize:
        width = self._doc.idealWidth() if self._doc.textWidth() < 0 else self._doc.textWidth()
        pad_x, pad_y = theme.METRICS["tipPadX"], theme.METRICS["tipPadY"]
        return QSize(round(width) + 2 * pad_x + 2, round(self._doc.size().height()) + 2 * pad_y + 2)

    def showFor(
        self,
        target: QWidget,
        text: str,
        rect: QRect | None = None,
        side: str | None = None,
        fade: bool = True,
    ) -> None:
        """대상 옆에 띄운다 — `rect`(전역)를 주면 그 사각형(메뉴 항목 등)을 대상으로 본다.

        `side`를 주면 창의 경계 대신 그 방향을 바란다(등록 오류는 늘 URL 칸 아래 — #536). 화면 밖이면 반대쪽이다.

        숨어 있었거나 사라지는 중이었으면 투명에서 나타난다(`fade`가 거짓이거나 애니메이션 끔이면 바로) — 사라지던
        것은 그 자리에서 지워진다. 이미 떠 있으면(대상 · 글만 바뀜, 창 크기에 따라 다시 놓음) 불투명 그대로 옮긴다
        — 떠 있는 말풍선이 깜빡이지 않게(#610).
        """
        appear = fade and (not self.isVisible() or self._fadingOut)
        self._stopFade()
        self.target = target
        self.text = text
        self._setText(text)
        self.targetRect = (
            rect if rect is not None else QRect(target.mapToGlobal(QPoint(0, 0)), target.size())
        )
        host = self.parentWidget() if self.embedded else None
        if host is not None:
            screen = QRect(host.mapToGlobal(QPoint(0, 0)), host.size())
        else:
            screen = target.screen().availableGeometry()
        body = self._bodySize()
        wanted = side or tip_side(target)
        self.side = wanted
        for _ in range(2):  # 방향이 바뀌면 꼬리 쪽 여백이 바뀌므로 한 번 더 센다
            left, top, right, bottom = self.margins()
            size = QSize(body.width() + left + right, body.height() + top + bottom)
            geometry, side = tip_rect(self.targetRect, size, wanted, screen)
            if side == self.side:
                break
            self.side = side
        if self.side in ("below", "above"):
            self._tail = self.targetRect.center().x() - geometry.left()
        else:
            self._tail = self.targetRect.center().y() - geometry.top()
        if host is not None:
            geometry.translate(-screen.topLeft())
        self.setGeometry(geometry)
        self._opacity = 1.0
        if appear and _animated(TIP_FADE_IN_MS):
            self._startFade(0.0, 1.0, TIP_FADE_IN_MS, QEasingCurve.Type.OutCubic)
        self.update()
        self.show()
        self.raise_()

    def fadeOut(self) -> None:
        """닫는다 — `TIP_FADE_OUT_MS` 동안 사라진 뒤 숨는다(애니메이션 끔이면 바로).

        숨어 있거나 이미 사라지는 중이면 아무것도 하지 않는다.
        """
        if not self.isVisible() or self._fadingOut:
            return
        if not _animated(TIP_FADE_OUT_MS):
            self.hideNow()
            return
        start = self._opacity
        self._stopFade()
        self._fadingOut = True
        self._startFade(start, 0.0, TIP_FADE_OUT_MS, QEasingCurve.Type.Linear)

    def _startFade(self, start: float, end: float, ms: int, curve: QEasingCurve.Type) -> None:
        """불투명도를 start → end로 ms 동안 바꾸는 애니메이션을 건다 — 끝나면 지운다(움직이는 동안만 있다)."""
        self._opacity = start
        animation = self._fade = QVariantAnimation(self)
        animation.setStartValue(start)
        animation.setEndValue(end)
        animation.setDuration(ms)
        animation.setEasingCurve(curve)
        animation.valueChanged.connect(self._onFadeStep)
        animation.finished.connect(self._onFadeFinished)
        animation.start()

    def _onFadeStep(self, value: float) -> None:
        self._opacity = float(value)
        self.update()

    def _onFadeFinished(self) -> None:
        """페이드가 끝났다 — 닫히는 중이었으면 숨고, 나타나는 중이었으면 불투명으로 두고 애니메이션을 지운다."""
        if self._fadingOut:
            self.hideNow()
            return
        self._opacity = 1.0
        self._stopFade()
        self.update()

    def _stopFade(self) -> None:
        """페이드를 그 자리에서 멈추고 애니메이션을 지운다 — 닫히는 중이었으면 그만둔다."""
        animation, self._fade = self._fade, None
        self._fadingOut = False
        if animation is not None:
            animation.stop()
            animation.deleteLater()

    def globalRect(self) -> QRect:
        """말풍선 사각형(전역) — 따로 뜬 창이든 자식이든 같은 좌표로 잰다."""
        return QRect(self.mapToGlobal(QPoint(0, 0)), self.size())

    def hideNow(self) -> None:
        """바로 숨긴다 — 페이드 중이면 멈춘다."""
        self._stopFade()
        self._opacity = 1.0
        self.hide()
        self.target = None

    def bodyRect(self) -> QRectF:
        """꼬리 · 그림자를 뺀 몸통 사각형(위젯 좌표)."""
        left, top, right, bottom = self.margins()
        return QRectF(left, top, self.width() - left - right, self.height() - top - bottom)

    def paintEvent(self, event: QPaintEvent) -> None:
        tokens = theme.current_tokens()
        radius, tail_len = theme.METRICS["tipRadius"], theme.METRICS["tipTail"]
        half = theme.METRICS["tipTailWidth"] / 2
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(self._opacity)  # 페이드 — 그림자 · 면 · 글을 함께 옅게(#610)
        body = self.bodyRect()
        low = radius + half
        if self.side in ("below", "above"):
            center = max(body.left() + low, min(self._tail, body.right() - low))
        else:
            center = max(body.top() + low, min(self._tail, body.bottom() - low))
        shape = bubble_outline(body, radius, _FACING[self.side], center, half, tail_len)
        layers = theme.METRICS["tipShadow"]
        for i in range(layers, 0, -1):  # 그림자 — 바깥으로 갈수록 옅게
            shadow = QColor(tokens["shadow"])
            shadow.setAlphaF(_SHADOW_ALPHA * (layers - i + 1) / layers)
            painter.setPen(
                QPen(
                    shadow,
                    2 * i,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                    Qt.PenJoinStyle.RoundJoin,
                )
            )
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(shape.translated(0, 1))
        face, border, text = TONES[self.tone]
        painter.setPen(QPen(QColor(tokens[border]), 1))
        painter.setBrush(QColor(tokens[face]))
        painter.drawPath(shape)
        pad_x, pad_y = theme.METRICS["tipPadX"], theme.METRICS["tipPadY"]
        painter.translate(body.left() + 1 + pad_x, body.top() + 1 + pad_y)
        context = QAbstractTextDocumentLayout.PaintContext()
        context.palette.setColor(QPalette.ColorRole.Text, QColor(tokens[text]))
        self._doc.documentLayout().draw(painter, context)
        painter.end()


_STATE: dict[str, TipBubble | None] = {"bubble": None}  # 말풍선 창 하나 — 처음 띄울 때 만든다


def tip_bubble() -> TipBubble:
    """말풍선 창 — 하나를 돌려 쓴다. 창이 지워졌으면(최상위 창 정리 등) 새로 만든다."""
    if current_bubble() is None:
        _STATE["bubble"] = TipBubble()
    return _STATE["bubble"]


def current_bubble() -> TipBubble | None:
    """지금 살아 있는 말풍선 창(없거나 C++ 객체가 지워졌으면 None)."""
    bubble = _STATE["bubble"]
    if bubble is not None and not shiboken6.isValid(bubble):
        _STATE["bubble"] = bubble = None
    return bubble


def show_tip(target: QWidget, text: str, rect: QRect | None = None) -> None:
    """그 대상 옆에 글을 띄운다 — 이미 그 대상 · 그 글로 떠 있으면 그대로 둔다(깜빡이지 않게)."""
    bubble = tip_bubble()
    if not bubble.showing(target, text):
        bubble.showFor(target, text, rect)


def hide_tip(target: QWidget | None = None) -> None:
    """말풍선을 닫는다(사라지는 페이드) — `target`을 주면 그 대상에 떠 있을 때만."""
    bubble = current_bubble()
    if bubble is not None and (target is None or bubble.target is target):
        bubble.fadeOut()


def _animated(ms: int) -> bool:
    """그 시간의 페이드를 할지 — 시간이 0보다 크고 설정의 애니메이션이 켜져 있을 때."""
    return ms > 0 and card_display.current().animations


def close_floats_if_left(window: QWidget) -> None:
    """창을 떠났으면(다른 창이 활성 · 최소화) 말풍선과 열린 메뉴를 닫는다 (#536 오너 실기).

    창의 활성 · 상태 변화(`changeEvent`)에서 부른다. 포인터를 대상에 둔 채 Alt+Tab 하면 나감 이벤트가 오지 않아
    따로 뜬 말풍선 · 메뉴가 다른 창 위에 남는다. 지연 뒤에 뜰 말풍선(카드 위 툴팁)의 기다림도 그만둔다. 그
    창이 연 메뉴가 활성을 가져간 경우(활성 창이 메뉴)는 떠난 것이 아니다. 등록 오류 말풍선(`ErrorTip`)은 창의
    자식이라 창과 함께 뒤로 가므로 닫지 않는다.
    """
    active = QApplication.activeWindow()
    popup = QApplication.activePopupWidget()
    left = window.isMinimized() or (
        active is not window and (active is None or active is not popup)
    )
    if not left:
        return
    # 지연 뒤에 뜰 말풍선도 그만둔다 — 나감 없이 떠났으면 그대로 두면 다른 창 위에 뜬다(#608 리뷰)
    if _FILTER["filter"] is not None:
        _FILTER["filter"].cancelPending()
    hide_tip()
    if popup is not None:
        popup.close()


class TipFilter(QObject):
    """위젯마다 거는 이벤트 필터 — 들어오면 그 위젯의 툴팁을 말풍선으로 바로 띄우고, 나가거나 누르면 닫는다.

    Qt의 툴팁 이벤트(`QEvent.ToolTip`)는 툴팁 글이 있으면 삼킨다 — 기본 툴팁이 함께 뜨지 않게.

    `tipDelayed` 속성이 참인 위젯(`install_tips(..., delayed=True)`)은 들어감에서 바로 띄우지 않고 타이머
    하나로 `HOVER_TIP_DELAY_MS` 뒤에 띄운다. 기다리는 대상은 하나다 — 다른 위젯에 들어가면 그 위젯으로
    바뀌고, 기다리던 위젯에서 나가거나 누르거나 숨으면 그만둔다.
    """

    def __init__(self) -> None:
        super().__init__()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._showPending)
        self._pending: QWidget | None = None  # 지연 뒤에 띄울 위젯

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        kind = event.type()
        if not obj.isWidgetType():
            return False
        if kind == QEvent.Type.MouseButtonPress:
            obj.setProperty("tipPressed", True)  # 누른 뒤에는 나갈 때까지 다시 띄우지 않는다
            self._cancel(obj)
            hide_tip(obj)
            return False
        if kind == QEvent.Type.ToolTip:
            return bool(obj.toolTip())
        if kind == QEvent.Type.Enter:
            if obj.property("tipDelayed"):
                self._pending = obj
                self._timer.start(HOVER_TIP_DELAY_MS)  # 상수는 걸 때마다 읽는다(테스트가 줄인다)
            elif self._allowed(obj):
                show_tip(obj, obj.toolTip())
            return False
        if kind in (QEvent.Type.Leave, QEvent.Type.Hide):
            obj.setProperty("tipPressed", False)
            self._cancel(obj)
            hide_tip(obj)
        return False

    def pending(self) -> QWidget | None:
        """지연 뒤에 띄우려고 기다리는 위젯(없으면 None)."""
        if self._pending is not None and not shiboken6.isValid(self._pending):
            self._pending = None
        return self._pending if self._timer.isActive() else None

    def cancelPending(self) -> None:
        """기다리는 말풍선을 그만둔다 — 기다리는 위젯이 없으면 아무것도 하지 않는다."""
        self._timer.stop()
        self._pending = None

    def _cancel(self, widget: QObject) -> None:
        """그 위젯을 기다리고 있으면 그만둔다 — 다른 위젯을 기다리는 중이면 그대로 둔다."""
        if self._pending is widget:
            self._timer.stop()
            self._pending = None

    def _showPending(self) -> None:
        widget, self._pending = self._pending, None
        if widget is not None and shiboken6.isValid(widget) and self._allowed(widget):
            show_tip(widget, widget.toolTip())

    @staticmethod
    def _allowed(widget: QWidget) -> bool:
        """지금 띄워도 되는지 — 툴팁 글 · 보임 · 누르지 않음 · 열린 드롭다운 메뉴 없음."""
        if not widget.toolTip() or not widget.isVisible() or widget.property("tipPressed"):
            return False
        return QApplication.activePopupWidget() is None


_FILTER: dict[str, TipFilter | None] = {"filter": None}  # 위젯들이 함께 쓰는 필터 하나


def tip_filter() -> TipFilter:
    """위젯들이 함께 쓰는 말풍선 필터 — 처음 부를 때 만든다."""
    if _FILTER["filter"] is None:
        _FILTER["filter"] = TipFilter()
    return _FILTER["filter"]


def install_tips(root: QWidget, delayed: bool = False) -> None:
    """root와 그 아래 위젯마다 말풍선 필터를 건다 — 여러 번 불러도 위젯마다 한 번만 건다.

    Args:
        delayed: 참이면 그 위젯들의 툴팁은 `HOVER_TIP_DELAY_MS` 머문 뒤에 뜬다(카드 위 툴팁, #599)
    """
    shared = tip_filter()
    for widget in (root, *root.findChildren(QWidget)):
        if delayed:
            widget.setProperty("tipDelayed", True)
        if not widget.property("tipInstalled"):
            widget.installEventFilter(shared)
            widget.setProperty("tipInstalled", True)


class ErrorTip(QObject):
    """입력칸 아래에 붙는 오류 말풍선 — 헤더바 등록 오류(#536 — 설계 §2 알림 자리 · §12.1 등록 오류).

    툴팁과 같은 말풍선 부품(`TipBubble`)에 실패 색 계열(`tone="error"`)이고, 늘 대상 아래로(꼬리 위) 뜬다. 포인터가
    나가도 닫히지 않는다 — 입력을 고치면(부르는 쪽이 `hide`) · Esc · 대상 밖을 누르면 닫힌다.

    말풍선은 대상 창의 자식 위젯이다(`embedded`) — 따로 뜬 창이면 본 창을 Alt+Tab으로 뒤로 보내도 화면 맨 위에
    남는다(#536 오너 실기). 자식이라 본 창과 함께 뒤로 가고 · 최소화되고 · 움직인다. 레이아웃에 들지 않고 형제
    위젯 위에 겹쳐 그리므로 헤더바에 오류 줄 자리를 비워 두지 않아도 목록이 움직이지 않는다. 최소화했다가 다시
    띄우면 오류가 남아 있는 한 그대로 보인다 — 최소화는 닫히는 조건(입력 고침 · Esc · 다른 곳 누름)이 아니다.

    누름 · Esc는 말풍선이 보이는 동안만 대상 창의 `QWindow`에 건 필터로 본다 — 그 창으로 오는 입력이 위젯보다
    먼저 지나가는 자리라 카드 위젯마다 걸지 않아도 되고, 앱 전체 필터가 아니다(전체 테스트 접근 위반 #536 기록).
    창 크기가 바뀌면 말풍선을 대상 아래로 다시 놓는다(대상이 창 안에서 움직인다).
    """

    def __init__(self, target: QWidget) -> None:
        super().__init__(target)
        self._target = target
        self._bubble: TipBubble | None = None
        self._text = ""
        self._watched: list[QObject] = []

    def bubble(self) -> TipBubble | None:
        """말풍선 창(한 번도 띄우지 않았거나 지워졌으면 None)."""
        if self._bubble is not None and not shiboken6.isValid(self._bubble):
            self._bubble = None
        return self._bubble

    def isShowing(self) -> bool:
        """말풍선이 보이는지 — 사라지는 중이면 닫힌 것으로 본다."""
        bubble = self.bubble()
        return bubble is not None and bubble.isVisible() and not bubble.isFadingOut()

    def text(self) -> str:
        """지금 보이는 오류 글(닫혀 있으면 빈 글)."""
        return self._text if self.isShowing() else ""

    def show(self, text: str) -> None:
        """대상 아래에 오류 글을 띄운다 — 떠 있으면 글만 바꿔 다시 놓는다."""
        bubble = self.bubble()
        if bubble is None:
            # 창의 자식으로 그린다 — 창이 지워지면 함께 지워진다
            bubble = self._bubble = TipBubble("error", self._target.window(), embedded=True)
            bubble.setObjectName("errorTip")
        self._text = text
        bubble.showFor(self._target, text, side="below")
        self._watch()

    def hide(self) -> None:
        """닫는다 — 이미 닫혀 있으면 아무 일도 없다."""
        self._unwatch()
        self._text = ""
        bubble = self.bubble()
        if bubble is not None and bubble.isVisible():
            bubble.fadeOut()

    def _watch(self) -> None:
        if self._watched:
            return
        window = self._target.window()
        handle = window.windowHandle()
        self._watched = [obj for obj in (handle, window) if obj is not None]
        for obj in self._watched:
            obj.installEventFilter(self)

    def _unwatch(self) -> None:
        for obj in self._watched:
            if shiboken6.isValid(obj):
                obj.removeEventFilter(self)
        self._watched = []

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress and not obj.isWidgetType():
            point = event.globalPosition().toPoint()
            inside = QRect(self._target.mapToGlobal(QPoint(0, 0)), self._target.size())
            if not inside.contains(point):
                self.hide()  # 대상 밖을 눌렀다 — 누름은 그대로 흘려 보낸다
        elif kind == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            self.hide()
        elif kind == QEvent.Type.Resize and obj.isWidgetType():
            if self.isShowing():
                self.bubble().showFor(self._target, self._text, side="below", fade=False)
        return False
