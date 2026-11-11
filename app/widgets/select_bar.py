"""선택 줄 — 선택 모드에서 선택 버튼 바로 아래, 목록 위에 떠 있는 줄 (#561 — 설계 §12.1 「선택 모드」).

차례는 「N개 선택」 | 세 상태 체크박스 · 지우기(휴지통) · 취소(✕)다. 왼쪽은 글, 구분선 오른쪽은 누르는 것만 —
체크박스는 오른쪽 묶음에 있어 글 길이가 바뀌어도 자리가 그대로다. 면은 말풍선과 같은 떠 있는 면(면 · 테두리 1px ·
모서리 8 · 그림자)이고, 그림자는 누름을 받지 않는 따로 된 위젯이 그린다 — 밑 카드가 그대로 눌린다. 자리는 창이
정한다(`MainWindow._placeSelectBar` — 오른쪽 끝 = 선택 버튼 오른쪽 끝, 헤더바는 가리지 않는다).

나타나고 사라질 때 투명도만 바뀐다(애니메이션 끔이면 바로). 투명도 효과는 그동안만 붙였다가 뗀다 — 늘 붙여 두면
그 위젯을 그릴 때마다 화면 밖 그림을 한 번 더 그린다. 색은 그릴 때마다 theme 토큰을 읽는다(실행 중 테마 전환).
"""

import re

from PySide6.QtCore import QCoreApplication, QPointF, QRect, QRectF, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QKeyEvent, QPainter, QPainterPath, QPaintEvent, QPen
from PySide6.QtWidgets import QGraphicsOpacityEffect, QHBoxLayout, QLabel, QPushButton, QWidget

import app.theme as theme
from app.viewmodels import card_display
from app.widgets.icons import IconButton
from app.widgets.view import is_delete_key

FADE_MS = 90  # 나타남 · 사라짐 시간(ms) — 프로토 TIP_FADE_MS
SHADOW_ALPHA = 0.05  # 그림자 가장 안쪽 한 겹의 불투명도 — 말풍선과 같다(tip_bubble)


def off_border_token(face: str) -> str:
    """꺼진 체크박스 테두리 토큰 — 떠 있는 면과 비텍스트 대비 3.0을 넘는 첫 토큰(borderStrong → textMuted → text).

    borderStrong은 떠 있는 면 위에서 대비가 낮아 네모가 보이지 않았다(프로토 inbox 49).
    """
    tokens = theme.current_tokens()
    for name in ("borderStrong", "textMuted", "text"):
        if theme.contrast_ratio(tokens[name], tokens[face]) >= 3.0:
            return name
    return "text"


class SelectCheck(QPushButton):
    """세 상태 체크박스 — 0 하나도 ☐ · 1 일부 ━ · 2 모두 ☑. 상태는 창이 고른 수로 정한다(`setState`).

    누름이 스스로 상태를 바꾸지 않는다 — 누르면 창이 모두 선택 ↔ 모두 해제(일부면 모두 선택)를 한다. 켜짐(일부 ·
    모두)은 강조색 채움과 그 안 기호(onAccent), 꺼짐은 테두리만이다. 호버 · 눌림은 강조색 단계다.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("selectCheck")
        self.setFixedSize(theme.METRICS["selectCheckWidth"], theme.METRICS["barControl"])
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self._state = 0

    def state(self) -> int:
        """0 하나도 · 1 일부 · 2 모두."""
        return self._state

    def setState(self, state: int) -> None:
        """상태를 바꾼다."""
        if state != self._state:
            self._state = state
            self.update()

    def colors(self) -> dict[str, str]:
        """지금 상태 · 호버 · 눌림의 채움 · 테두리 · 기호 토큰 — 채움 · 기호가 없으면 빈 글."""
        pressed, hover = self.isDown(), self.underMouse()
        if self._state:
            fill = "accentPressed" if pressed else ("accentHover" if hover else "accent")
            return {"fill": fill, "border": fill, "mark": "onAccent"}
        border = "accent" if (hover or pressed) else off_border_token("tipBg")
        return {"fill": "surfacePressed" if pressed else "", "border": border, "mark": ""}

    def paintEvent(self, event: QPaintEvent) -> None:
        tokens = theme.current_tokens()
        names = self.colors()
        side = theme.METRICS["selectCheckBox"]
        box = QRectF(
            (self.width() - side) / 2 + 0.5, (self.height() - side) / 2 + 0.5, side - 1, side - 1
        )
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(tokens[names["border"]]), 1))
        painter.setBrush(QColor(tokens[names["fill"]]) if names["fill"] else Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(box, 3, 3)
        if self._state:
            pen = QPen(QColor(tokens[names["mark"]]), 2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            x, y = box.left(), box.top()
            if self._state == 1:  # 일부 ━
                painter.drawLine(
                    QPointF(x + 4, y + side / 2 - 0.5), QPointF(x + side - 5, y + side / 2 - 0.5)
                )
            else:  # 모두 ✓
                painter.drawPolyline(
                    [QPointF(x + 3.5, y + 8), QPointF(x + 6.5, y + 11), QPointF(x + 11.5, y + 5)]
                )
        if self.hasFocus():
            painter.setPen(QPen(QColor(tokens["accent"]), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(box.adjusted(-2, -2, 2, 2), 4, 4)
        painter.end()


class _Separator(QWidget):
    """글 ↔ 버튼 묶음 구분선(1px, 선택 줄 테두리 색). 누름을 받지 않는다."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFixedSize(1, theme.METRICS["selectSepHeight"])

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(theme.current_tokens()["borderStrong"]))
        painter.end()


class SelectBarShadow(QWidget):
    """선택 줄 그림자 — 말풍선과 같은 겹 그림자. 누름을 받지 않는다. 위끝은 헤더바 아래끝에서 자른다."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._body = QRect()

    def follow(self, body: QRect, ceiling: int) -> None:
        """줄 사각형 `body`(부모 좌표)를 둘러싸게 놓는다 — `ceiling`(부모 y)보다 위로는 그리지 않는다."""
        layers = theme.METRICS["tipShadow"]
        top = max(ceiling, body.top() - layers)
        self.setGeometry(
            body.left() - layers, top, body.width() + 2 * layers, body.bottom() + 2 + layers - top
        )
        self._body = QRect(body.topLeft() - self.pos(), body.size())
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        tokens = theme.current_tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        radius = theme.METRICS["tipRadius"]
        path.addRoundedRect(QRectF(self._body), radius, radius)
        layers = theme.METRICS["tipShadow"]
        for i in range(layers, 0, -1):  # 바깥으로 갈수록 옅게
            shadow = QColor(tokens["shadow"])
            shadow.setAlphaF(SHADOW_ALPHA * (layers - i + 1) / layers)
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
            painter.drawPath(path.translated(0, 1))
        painter.end()


class SelectBar(QWidget):
    """선택 줄 — 「N개 선택」 | 체크박스 · 지우기 · 취소. 그 자리의 누름은 받는다(밑 카드로 가지 않는다).

    시그널:
        checkClicked: 체크박스를 눌렀다
        deleteClicked: 지우기를 눌렀다
        cancelClicked: 취소를 눌렀다
    """

    checkClicked = Signal()
    deleteClicked = Signal()
    cancelClicked = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("selectBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.shadow = SelectBarShadow(parent)
        control = theme.METRICS["barControl"]
        layout = QHBoxLayout(self)
        # 위아래 1 = 테두리, 오른쪽 0 = 취소(✕)가 선택 버튼과 같은 x에 선다
        layout.setContentsMargins(theme.METRICS["selectBarPad"], 1, 0, 1)
        layout.setSpacing(8)
        self.countLabel = QLabel(self)
        self.countLabel.setObjectName("selectCount")
        # 글은 오른쪽 정렬 — 고른 수가 바뀌어도 글 끝이 구분선 앞 같은 자리다
        self.countLabel.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.countLabel)
        layout.addStretch(1)  # 버튼 묶음은 오른쪽 끝에 붙는다
        layout.addWidget(_Separator(self), 0, Qt.AlignmentFlag.AlignVCenter)
        # 만드는 차례가 Tab 차례다 — 체크박스 → 지우기 → 취소(글은 초점을 받지 않는다)
        self.checkBox = SelectCheck(self)
        self.checkBox.clicked.connect(self.checkClicked)
        layout.addWidget(self.checkBox)
        self.deleteButton = self._iconButton("trash", "deleteButton")
        self.deleteButton.setProperty("danger", True)  # 포인터가 오면 실패 색 면(style.qss)
        self.deleteButton.setHoverToken("onAccent")  # 실패 색 면 위 도형
        self.deleteButton.clicked.connect(self.deleteClicked)
        layout.addWidget(self.deleteButton)
        self.cancelButton = self._iconButton("delete", "cancelButton")
        self.cancelButton.clicked.connect(self.cancelClicked)
        layout.addWidget(self.cancelButton)
        self.setFixedHeight(control + 2)
        self._count = 0
        self._total = 0
        self._shown = False  # 나타나 있는지(사라지는 중이면 아니다)
        self._fade: QVariantAnimation | None = None
        self.retranslate()
        self.hide()
        self.shadow.hide()

    def _iconButton(self, icon: str, name: str) -> IconButton:
        button = IconButton(self)
        button.setObjectName(name)
        button.setProperty("role", "selectIcon")
        button.setIconName(icon)
        button.setGlyphMetric("narrowIcon")
        button.setIdleToken("text")
        side = theme.METRICS["barControl"]
        button.setFixedSize(side, side)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        return button

    def retranslate(self) -> None:
        """툴팁 · 글을 지금 언어로."""
        self.checkBox.setToolTip(QCoreApplication.translate("SelectBar", "Select all"))
        self.deleteButton.setToolTip(QCoreApplication.translate("SelectBar", "Delete"))
        self.cancelButton.setToolTip(QCoreApplication.translate("SelectBar", "Cancel"))
        self._applyCount()

    @staticmethod
    def countText(count: int) -> str:
        """「N개 선택」 글."""
        return QCoreApplication.translate("SelectBar", "%1 selected").replace("%1", str(count))

    def setCounts(self, count: int, total: int, check: int) -> None:
        """고른 수 · 목록 전체 수 · 체크박스 상태를 건다.

        글 칸 폭은 목록 전체 수의 자릿수로 잡는다 — 그 자릿수만큼 가장 넓은 숫자를 넣은 글의 폭. 고르고 푸는 동안
        줄 폭이 그대로고, 목록 수의 자릿수가 바뀔 때(9 ↔ 10 …)만 바뀐다. 실제 글이 더 넓으면 그 폭이다 — 글은
        늘 다 들어간다(프로토 inbox 54).
        """
        self.checkBox.setState(check)
        if (count, total) != (self._count, self._total):
            self._count, self._total = count, total
            self._applyCount()

    def _applyCount(self) -> None:
        label = self.countLabel
        text = self.countText(self._count)
        metrics = label.fontMetrics()
        widest = max("0123456789", key=metrics.horizontalAdvance)
        sample = re.sub(r"\d+", widest * len(str(max(1, self._total))), text)
        label.setText(text)
        label.setFixedWidth(max(self._labelWidth(sample), self._labelWidth(text)))

    def _labelWidth(self, text: str) -> int:
        """글 칸이 `text`를 잘림 없이 그리는 폭 — QLabel의 크기 힌트로 잰다(QSS 여백 · 실제 그리는 자리가 든다)."""
        label = self.countLabel
        label.ensurePolished()
        shown = label.text()
        low, high = label.minimumWidth(), label.maximumWidth()
        label.setMinimumWidth(0)
        label.setMaximumWidth(16777215)
        label.setText(text)
        width = label.sizeHint().width()
        label.setText(shown)
        label.setMinimumWidth(low)
        label.setMaximumWidth(high)
        return width

    def isShown(self) -> bool:
        """나타나 있는지 — 사라지는 중이면 거짓."""
        return self._shown

    def opacity(self) -> float:
        """지금 투명도 — 숨었으면 0, 바뀌는 중이면 그 값."""
        if not self.isVisible():
            return 0.0
        effect = self.graphicsEffect()
        return effect.opacity() if isinstance(effect, QGraphicsOpacityEffect) else 1.0

    def place(self, right: int, top: int, ceiling: int) -> None:
        """오른쪽 끝 `right`(포함하지 않는 x) · 위끝 `top`에 놓는다 — 폭은 글 · 버튼이 다 들어가는 폭이다."""
        self.layout().activate()
        width = self.sizeHint().width()
        self.setGeometry(right - width, top, width, self.height())
        self.shadow.follow(self.geometry(), ceiling)
        if self._shown:
            self.shadow.raise_()
            self.raise_()

    def fadeTo(self, show: bool) -> None:
        """투명도로 나타나거나 사라진다 — 애니메이션 끔이면 바로."""
        parts = (self.shadow, self)
        if show:
            self.shadow.raise_()
            self.raise_()
        if show == self._shown:
            return
        self._shown = show
        start = self.opacity() if self._fade is not None else (0.0 if show else 1.0)
        if self._fade is not None:
            self._fade.stop()
            self._fade.deleteLater()
            self._fade = None
        if not card_display.current().animations:
            for part in parts:
                part.setGraphicsEffect(None)
                part.setVisible(show)
            return
        effects = []
        for part in parts:
            effect = QGraphicsOpacityEffect(part)
            effect.setOpacity(start)
            part.setGraphicsEffect(effect)
            effects.append(effect)
            part.show()
        fade = QVariantAnimation(self)
        fade.setDuration(FADE_MS)
        fade.setStartValue(float(start))
        fade.setEndValue(1.0 if show else 0.0)
        fade.valueChanged.connect(lambda value: [e.setOpacity(value) for e in effects])
        fade.finished.connect(lambda: self._fadeDone(fade, show))
        self._fade = fade
        fade.start()

    def _fadeDone(self, fade: QVariantAnimation, show: bool) -> None:
        if self._fade is not fade:
            return
        for part in (self.shadow, self):
            part.setGraphicsEffect(None)
            part.setVisible(show)
        self._fade = None
        fade.deleteLater()

    def paintEvent(self, event: QPaintEvent) -> None:
        """떠 있는 면과 테두리 1px — 테두리를 QSS로 두면 안쪽 자리가 1px 줄어 취소가 선택 버튼 x에 서지 못한다."""
        tokens = theme.current_tokens()
        radius = theme.METRICS["tipRadius"]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(tokens["borderStrong"]), 1))
        painter.setBrush(QColor(tokens["tipBg"]))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        painter.end()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """초점이 선택 줄 버튼에 있을 때의 Delete(macOS ⌘+Backspace) — 고른 카드를 지운다. Esc는 창이 받는다."""
        if is_delete_key(event):
            event.accept()
            self.deleteClicked.emit()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:
        event.accept()  # 밑 카드로 가지 않는다

    def mouseReleaseEvent(self, event) -> None:
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        event.accept()
