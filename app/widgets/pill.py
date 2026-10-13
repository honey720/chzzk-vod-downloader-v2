"""카드 3행 해상도 pill — 선택 표시·접힘 표시(▾)·보조 글자를 가진 QPushButton (#244 3행 정리).

평소(접힘)에는 **선택된 해상도 하나**만 `[1080p ▾]`로 보이고, 누르면 그 자리에서
전부 펼쳐진다(팝업이 아니다 — app/widgets/widget.py::setExpanded). 이 클래스가
드는 것은 불리언 둘(선택·접힘 표시)과 보조 글자 문자열 하나뿐이다 — 카드마다
페인트 객체가 붙지 않는다.

- 선택 = 동적 속성 `selected` → 전역 QSS `[selected="true"]`가 채움(accent)을
  그린다. 이전엔 "선택 = 비활성 버튼(`:disabled`)"이었는데, 접힌 pill은 눌러서
  펼쳐야 하므로 선택 pill도 활성이어야 한다.
- 접힘 표시(▾) = 동적 속성 `caret` → QSS가 오른쪽 padding을 넓히고, 그 자리에
  paintEvent가 작은 삼각형을 **직접 그린다**. 글리프(U+25BE)는 폰트 스택이
  모양을 정해 macOS·Linux 실기 없이는 확인할 길이 없다(app/widgets/icons.py와 같은
  이유). 색은 theme.py 토큰 이름으로만 고른다(선택 onAccent / 호버 text /
  평소 textMuted) — 이 파일에 색 리터럴은 없다.
- 보조 글자("60fps") = 본 글자 오른쪽에 같은 크기로, 한 단계 흐리게 **직접 그린다**
  (#318). 버튼의 글자(`text()`)는 본 글자("1080p(원본)")뿐이고, QSS가 그것을 왼쪽에
  붙여 그린다(`text-align: left`). 보조 글자의 몫만큼 `sizeHint()`가 넓어지고 그 자리에
  그린다. 한 버튼 안에서 색이 다른 글자 둘은 QSS만으로 낼 수 없다. 색은 본 글자보다
  한 단계 흐린 토큰이다(평소 textDisabled / 호버 textMuted / 선택은 onAccent를 조금
  투명하게).
- 폭이 자연 폭보다 좁으면(접힌 pill이 크기 글에 밀릴 때) 글자를 줄인다 — 먼저 보조
  글자를 그리지 않고, 그래도 모자라면 본 글자를 말줄임(…)한다(shownTexts). 줄였으면
  툴팁의 첫 줄에 전체 글자를 적는다. 최소 폭이 1px이라 레이아웃이 pill을 얼마든지
  누를 수 있다 — 글자를 폭에 맞추지 않으면 보조 글자가 본 글자 위에 겹쳐 그려진다.
"""

from PySide6.QtCore import QEvent, QPointF, QSize, Qt
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPolygonF
from PySide6.QtWidgets import (
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QStylePainter,
    QToolTip,
)

import app.theme as theme

#: ▾ 도형의 폭(px). 높이는 절반 — pill 높이(20px) 안에서 글자와 무게가 맞는 크기.
CARET_WIDTH = 8
#: 글자와 ▾ 사이 간격(px). QSS의 caret padding-right(= 8 + CARET_WIDTH + CARET_GAP)와 맞춘다.
CARET_GAP = 4
#: pill 좌우 가장자리 여백(px). QSS `[role="resolution"]`의 padding 좌우 값과 맞춘다.
EDGE_PADDING = 8
#: 본 글자와 보조 글자 사이 간격(px) — 글자 한 칸보다 좁아 한 덩어리로 읽힌다.
SECONDARY_GAP = 4
#: 선택된 pill에서 보조 글자의 불투명도(0~1). 채움(accent) 위의 글자색(onAccent)을 이만큼만
#: 칠해 본 글자보다 흐리게 한다 — 더 낮추면 파란 바탕에서 읽기 어렵다.
SECONDARY_SELECTED_OPACITY = 0.7


class ResolutionPill(QPushButton):
    """해상도 pill. `selected`·`caret`는 QSS 선택자용 동적 속성이기도 하다."""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self._selected = False
        self._caret = False
        self._secondary = ""
        self.setProperty("role", "resolution")
        self.setProperty("selected", False)
        self.setProperty("caret", False)
        # 호버 진입·이탈에 다시 그리게 한다 — ▾ 색이 호버 토큰을 따른다
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        # 가로: 자연 폭 이상으로 늘지 않고(Maximum), 최소 폭은 레이아웃을 묶지 않는다
        # (minimumSizeHint 1px). QPushButton 기본(Minimum)은 최소 폭 = 자연 폭이라
        # "pill 전부가 한 줄에" 있는 동안 카드 최소폭이 거기에 묶여, 창을 그 아래로
        # 줄일 수 없고 "안 들어가면 접는다" 판정(app/widgets/widget.py::_layoutRowThree)이
        # 영영 안 온다. 판정은 실제 폭이 아니라 naturalWidth()로 하므로, pill이
        # 실제로 쥐어짜이는 것은 판정이 도는 리사이즈 한 틱 안에서만이다.
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def minimumSizeHint(self) -> QSize:
        """가로 최소는 레이아웃을 묶지 않는다(1px) — 자연 폭은 naturalWidth()가 준다."""
        return QSize(1, super().minimumSizeHint().height())

    def sizeHint(self) -> QSize:
        """본 글자의 자연 크기에 보조 글자의 몫(간격 + 글자 폭)을 더한다."""
        hint = super().sizeHint()  # 폴리시가 여기서 끝난다 — 아래 글꼴 계산보다 먼저다
        if self._secondary:
            hint.setWidth(hint.width() + SECONDARY_GAP + self._secondaryWidth())
        return hint

    def setSecondaryText(self, text: str) -> None:
        """본 글자 뒤에 흐리게 붙는 보조 글자를 정한다 — 빈 문자열이면 없다."""
        if text == self._secondary:
            return
        self._secondary = text
        self.updateGeometry()
        self.update()

    def secondaryText(self) -> str:
        """지금의 보조 글자. 없으면 빈 문자열."""
        return self._secondary

    def secondaryToken(self) -> str:
        """보조 글자에 쓸 색 토큰 — 본 글자의 색(QSS)보다 한 단계 흐린 것.

        본 글자는 평소 textMuted, 호버 text, 선택 onAccent다. 보조 글자는 평소
        textDisabled, 호버 textMuted다. 선택은 채움 위에 쓸 더 흐린 토큰이 없어 같은
        onAccent를 쓰고 secondaryColor가 투명도로 흐리게 한다.
        """
        if self._selected:
            return "onAccent"
        if self.underMouse():
            return "textMuted"
        return "textDisabled"

    def secondaryColor(self) -> QColor:
        """보조 글자를 그리는 색 — 토큰의 색이고, 선택된 pill에서는 조금 투명하다."""
        color = QColor(theme.current_tokens()[self.secondaryToken()])
        if self._selected:
            color.setAlphaF(SECONDARY_SELECTED_OPACITY)
        return color

    def _secondaryWidth(self) -> int:
        return QFontMetrics(self.font()).horizontalAdvance(self._secondary)

    def fullText(self) -> str:
        """본 글자와 보조 글자 전부 — "1080p(원본) 60fps". 줄인 pill의 툴팁에 적는다."""
        return f"{self.text()} {self._secondary}" if self._secondary else self.text()

    def shownTexts(self) -> tuple[str, str]:
        """지금 폭에서 그리는 (본 글자, 보조 글자) — 겹치지 않게 폭에 맞춘 것이다.

        글자에 쓸 수 있는 폭은 지금 폭에서 여백(▾ 몫 포함)을 뺀 값이다. 둘 다 들어가면
        그대로, 본 글자만 들어가면 보조 글자를 뺀다. 본 글자도 안 들어가면 말줄임(…)한다.
        자연 폭(sizeHint) 이상이면 늘 둘 다 그린다.
        """
        metrics = QFontMetrics(self.font())
        main = metrics.horizontalAdvance(self.text())
        # 여백 = 스타일이 정한 자연 폭 − 본 글자 폭 (QSS의 padding을 여기 다시 적지 않는다)
        room = self.width() - (super().sizeHint().width() - main)
        if self._secondary and main + SECONDARY_GAP + self._secondaryWidth() <= room:
            return self.text(), self._secondary
        if main <= room:
            return self.text(), ""
        return metrics.elidedText(self.text(), Qt.TextElideMode.ElideRight, max(room, 0)), ""

    def isTruncated(self) -> bool:
        """지금 폭에서 글자를 줄여(보조 글자 숨김 · 말줄임) 그리는가."""
        return self.shownTexts() != (self.text(), self._secondary)

    def shownToolTip(self) -> str:
        """마우스를 올리면 뜨는 글 — 글자를 줄였으면 첫 줄이 전체 글자다.

        setToolTip으로 받은 글(파일 크기 등)은 그대로 두고 그 앞줄에 붙인다.
        """
        tip = self.toolTip()
        if not self.isTruncated():
            return tip
        return f"{self.fullText()}\n{tip}" if tip else self.fullText()

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            tip = self.shownToolTip()
            if tip:
                QToolTip.showText(event.globalPos(), tip, self)
            else:
                QToolTip.hideText()
                event.ignore()
            return True
        return super().event(event)

    def naturalWidth(self) -> int:
        """▾ 없이 텍스트+padding만의 자연 폭 — "들어가는가" 판정은 이 값으로 한다.

        `sizeHint()`는 지금 `caret` 속성에 따라 padding-right가 달라 판정에 쓰면
        모드에 따라 답이 흔들린다(되먹임). ▾ 몫(CARET_WIDTH + CARET_GAP)은 QSS의
        padding-right 차(20 − 8)와 같은 값이다.
        """
        width = self.sizeHint().width()
        return width - (CARET_WIDTH + CARET_GAP) if self._caret else width

    def setSelected(self, selected: bool) -> None:
        """선택 표시(채움)를 켜고 끈다 — 속성 변경 뒤 repolish로 QSS를 다시 계산시킨다."""
        if selected == self._selected:
            return
        self._selected = selected
        self.setProperty("selected", selected)
        theme.repolish(self)

    def isSelected(self) -> bool:
        """지금 선택된 해상도인가."""
        return self._selected

    def setCaret(self, caret: bool) -> None:
        """접힘 표시(▾)를 켜고 끈다 — 접힌 상태의 선택 pill에만 켠다."""
        if caret == self._caret:
            return
        self._caret = caret
        self.setProperty("caret", caret)
        theme.repolish(self)  # padding-right가 바뀌므로 sizeHint도 따라 바뀐다
        self.updateGeometry()

    def hasCaret(self) -> bool:
        """접힘 표시(▾)가 켜져 있는가."""
        return self._caret

    def caretToken(self) -> str:
        """▾에 쓸 색 토큰 — 선택 > 호버 > 평소."""
        if self._selected:
            return "onAccent"
        if self.underMouse():
            return "text"
        return "textMuted"

    def paintEvent(self, event) -> None:
        main, secondary = self.shownTexts()
        # 버튼은 스타일이 그대로 그린다 — 글자만 지금 폭에 맞춘 것으로 바꿔 넘긴다
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = main
        style_painter = QStylePainter(self)
        style_painter.drawControl(QStyle.ControlElement.CE_PushButton, option)
        style_painter.end()
        if not self._caret and not secondary:
            return
        tokens = theme.current_tokens()
        right = self.width() - EDGE_PADDING  # 오른쪽 가장자리 여백 안쪽
        painter = QPainter(self)
        if self._caret:
            left = right - CARET_WIDTH
            mid_y = self.height() / 2
            half_h = CARET_WIDTH / 4  # 높이 = 폭의 절반
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(tokens[self.caretToken()]))
            painter.drawPolygon(
                QPolygonF(
                    [
                        QPointF(left, mid_y - half_h),
                        QPointF(right, mid_y - half_h),
                        QPointF((left + right) / 2, mid_y + half_h),
                    ]
                )
            )
            painter.restore()
            right = left - CARET_GAP
        if secondary:
            # 본 글자와 같은 글꼴 · 같은 밑줄(baseline)에 그린다 — 본 글자는 스타일이 세로
            # 가운데로 그리므로 같은 식으로 밑줄을 구한다
            metrics = QFontMetrics(self.font())
            baseline = (self.height() - metrics.height()) // 2 + metrics.ascent()
            painter.setFont(self.font())
            painter.setPen(self.secondaryColor())
            painter.drawText(right - self._secondaryWidth(), baseline, secondary)
        painter.end()
