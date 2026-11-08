"""직접 그리는 말풍선(툴팁) — 모서리 · 꼬리 · 그림자, 대상 옆에 바로 뜬다 (#536 — 설계 §2 「말풍선」 · §12.1).

Qt 기본 툴팁(`QToolTip`)은 기다림이 있고, 대상을 가릴 수 있고, 모양을 QSS로 다 바꿀 수 없다. 이 모듈은
말풍선 하나(`TipBubble`)를 두고, 위젯마다 거는 이벤트 필터(`TipFilter`)가 들어감 · 나감 · 누름을 보고
그 위젯의 `toolTip()`을 띄우거나 닫는다. Qt의 툴팁 이벤트는 삼켜 기본 툴팁이 함께 뜨지 않게 한다.

- 방향: 창이 알려 주는 경계(`tipBoundary()` — 헤더바 아래 끝, 전역 y)보다 위에 있는 대상은 아래로, 그 밖은
  위로. 경계가 없는 창(대화상자 등)은 아래로. 화면 밖이면 반대쪽, 그래도 안 되면 옆, 마지막에 화면 안으로 민다
- 대상과 꼬리 끝 사이는 `tipGap` — 대상 사각형과 겹치지 않는다
- 들어오면 바로(0ms) 뜨고, 나가거나 누르거나 대상이 숨으면 바로 닫힌다. 누른 뒤에는 나갈 때까지 다시 띄우지
  않는다. 드롭다운 메뉴가 열려 있는 동안에는 띄우지 않는다
- 필터는 앱 전체가 아니라 위젯마다 건다(`install_tips`) — 앱 전체에 거는 파이썬 이벤트 필터는 모든 위젯의 모든
  이벤트를 파이썬으로 끌어와 느리고, 전체 테스트에서 접근 위반으로 죽은 적이 있다

색은 그릴 때마다 theme 토큰을 읽는다(실행 중 테마 전환 #247). 이 파일에 색 리터럴은 없다.
"""

import html

import shiboken6
from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QRectF, QSize, Qt
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
KEY_SEP = "\t"  # 툴팁 글의 이름 ↔ 단축키 구분 — 말풍선이 「이름 · 단축키」로 그리고 단축키는 흐린 색(#536)
# 말풍선 색 묶음 — (면, 테두리, 글) 토큰. 툴팁은 바 면, 등록 오류는 실패 색 계열(설계 §2 알림 자리, #536)
TONES = {
    "tip": ("surface", "border", "text"),
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
    """

    def __init__(self, tone: str = "tip", parent: QWidget | None = None) -> None:
        super().__init__(
            parent,
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
            | Qt.WindowType.WindowTransparentForInput,
        )
        self.setObjectName("tipBubble")
        for attribute in (
            Qt.WidgetAttribute.WA_TranslucentBackground,
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            Qt.WidgetAttribute.WA_ShowWithoutActivating,
        ):
            self.setAttribute(attribute)
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

    def showing(self, target: QWidget, text: str) -> bool:
        """그 대상에 그 글로 이미 떠 있는지."""
        return self.isVisible() and self.target is target and self.text == text

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
        self, target: QWidget, text: str, rect: QRect | None = None, side: str | None = None
    ) -> None:
        """대상 옆에 띄운다 — `rect`(전역)를 주면 그 사각형(메뉴 항목 등)을 대상으로 본다.

        `side`를 주면 창의 경계 대신 그 방향을 바란다(등록 오류는 늘 URL 칸 아래 — #536). 화면 밖이면 반대쪽이다.
        """
        self.target = target
        self.text = text
        self._setText(text)
        self.targetRect = (
            rect if rect is not None else QRect(target.mapToGlobal(QPoint(0, 0)), target.size())
        )
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
        self.setGeometry(geometry)
        self.update()
        self.show()
        self.raise_()

    def hideNow(self) -> None:
        """바로 숨긴다."""
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
        body = self.bodyRect()
        shape = QPainterPath()
        shape.addRoundedRect(body, radius, radius)
        tail = QPainterPath()
        low = radius + half
        if self.side in ("below", "above"):
            cx = max(body.left() + low, min(self._tail, body.right() - low))
            edge = body.top() if self.side == "below" else body.bottom()
            tip = edge - tail_len if self.side == "below" else edge + tail_len
            tail.moveTo(cx - half, edge)
            tail.lineTo(cx, tip)
            tail.lineTo(cx + half, edge)
        else:
            cy = max(body.top() + low, min(self._tail, body.bottom() - low))
            edge = body.left() if self.side == "right" else body.right()
            tip = edge - tail_len if self.side == "right" else edge + tail_len
            tail.moveTo(edge, cy - half)
            tail.lineTo(tip, cy)
            tail.lineTo(edge, cy + half)
        tail.closeSubpath()
        shape = shape.united(tail)
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
    """말풍선을 닫는다 — `target`을 주면 그 대상에 떠 있을 때만."""
    bubble = current_bubble()
    if bubble is not None and (target is None or bubble.target is target):
        bubble.hideNow()


class TipFilter(QObject):
    """위젯마다 거는 이벤트 필터 — 들어오면 그 위젯의 툴팁을 말풍선으로 바로 띄우고, 나가거나 누르면 닫는다.

    Qt의 툴팁 이벤트(`QEvent.ToolTip`)는 툴팁 글이 있으면 삼킨다 — 기본 툴팁이 함께 뜨지 않게.
    """

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        kind = event.type()
        if not obj.isWidgetType():
            return False
        if kind == QEvent.Type.MouseButtonPress:
            obj.setProperty("tipPressed", True)  # 누른 뒤에는 나갈 때까지 다시 띄우지 않는다
            hide_tip(obj)
            return False
        if kind == QEvent.Type.ToolTip:
            return bool(obj.toolTip())
        if kind == QEvent.Type.Enter:
            if self._allowed(obj):
                show_tip(obj, obj.toolTip())
            return False
        if kind in (QEvent.Type.Leave, QEvent.Type.Hide):
            obj.setProperty("tipPressed", False)
            hide_tip(obj)
        return False

    @staticmethod
    def _allowed(widget: QWidget) -> bool:
        """지금 띄워도 되는지 — 툴팁 글 · 보임 · 누르지 않음 · 열린 드롭다운 메뉴 없음."""
        if not widget.toolTip() or not widget.isVisible() or widget.property("tipPressed"):
            return False
        return QApplication.activePopupWidget() is None


_FILTER: dict[str, TipFilter | None] = {"filter": None}  # 위젯들이 함께 쓰는 필터 하나


def install_tips(root: QWidget) -> None:
    """root와 그 아래 위젯마다 말풍선 필터를 건다 — 여러 번 불러도 위젯마다 한 번만 건다."""
    if _FILTER["filter"] is None:
        _FILTER["filter"] = TipFilter()
    for widget in (root, *root.findChildren(QWidget)):
        if not widget.property("tipInstalled"):
            widget.installEventFilter(_FILTER["filter"])
            widget.setProperty("tipInstalled", True)


class ErrorTip(QObject):
    """입력칸 아래에 붙는 오류 말풍선 — 헤더바 등록 오류(#536 — 설계 §2 알림 자리 · §12.1 등록 오류).

    툴팁과 같은 말풍선 부품(`TipBubble`)에 실패 색 계열(`tone="error"`)이고, 늘 대상 아래로(꼬리 위) 뜬다. 포인터가
    나가도 닫히지 않는다 — 입력을 고치면(부르는 쪽이 `hide`) · Esc · 대상 밖을 누르면 닫힌다. 말풍선은 떠 있는
    창이라 헤더바에 오류 줄 자리를 비워 두지 않아도 목록이 움직이지 않는다.

    누름 · Esc는 말풍선이 보이는 동안만 대상 창의 `QWindow`에 건 필터로 본다 — 그 창으로 오는 입력이 위젯보다
    먼저 지나가는 자리라 카드 위젯마다 걸지 않아도 되고, 앱 전체 필터가 아니다(전체 테스트 접근 위반 #536 기록).
    창이 움직이거나 크기가 바뀌면 말풍선을 대상 아래로 다시 놓는다.
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
        """말풍선이 보이는지."""
        bubble = self.bubble()
        return bubble is not None and bubble.isVisible()

    def text(self) -> str:
        """지금 보이는 오류 글(닫혀 있으면 빈 글)."""
        return self._text if self.isShowing() else ""

    def show(self, text: str) -> None:
        """대상 아래에 오류 글을 띄운다 — 떠 있으면 글만 바꿔 다시 놓는다."""
        bubble = self.bubble()
        if bubble is None:
            # 창을 부모로 둔다 — 창이 지워지면 함께 지워진다(ToolTip 창 종류라 따로 뜨는 창이다)
            bubble = self._bubble = TipBubble("error", self._target.window())
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
            bubble.hideNow()

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
        elif kind in (QEvent.Type.Move, QEvent.Type.Resize) and obj.isWidgetType():
            if self.isShowing():
                self.bubble().showFor(self._target, self._text, side="below")
        elif kind == QEvent.Type.Hide and obj.isWidgetType():
            self.hide()
        return False
