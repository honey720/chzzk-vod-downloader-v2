"""카드 3행 해상도 pill — 선택 표시·접힘 표시(▾)를 가진 QPushButton (#244 3행 정리).

평소(접힘)에는 **선택된 해상도 하나**만 `[1080p ▾]`로 보이고, 누르면 그 자리에서
전부 펼쳐진다(팝업이 아니다 — content/widget.py::setExpanded). 이 클래스가
드는 것은 불리언 둘뿐이다(선택·접힘 표시) — 카드마다 페인트 객체가 붙지 않는다.

- 선택 = 동적 속성 `selected` → 전역 QSS `[selected="true"]`가 채움(accent)을
  그린다. 이전엔 "선택 = 비활성 버튼(`:disabled`)"이었는데, 접힌 pill은 눌러서
  펼쳐야 하므로 선택 pill도 활성이어야 한다.
- 접힘 표시(▾) = 동적 속성 `caret` → QSS가 오른쪽 padding을 넓히고, 그 자리에
  paintEvent가 작은 삼각형을 **직접 그린다**. 글리프(U+25BE)는 폰트 스택이
  모양을 정해 macOS·Linux 실기 없이는 확인할 길이 없다(content/icons.py와 같은
  이유). 색은 theme.py 토큰 이름으로만 고른다(선택 onAccent / 호버 text /
  평소 textMuted) — 이 파일에 색 리터럴은 없다.
"""

from PySide6.QtCore import QPointF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPolygonF
from PySide6.QtWidgets import QPushButton, QSizePolicy

import theme

#: ▾ 도형의 폭(px). 높이는 절반 — pill 높이(20px) 안에서 글자와 무게가 맞는 크기.
CARET_WIDTH = 8
#: 글자와 ▾ 사이 간격(px). QSS의 caret padding-right(= 8 + CARET_WIDTH + CARET_GAP)와 맞춘다.
CARET_GAP = 4


class ResolutionPill(QPushButton):
    """해상도 pill. `selected`·`caret`는 QSS 선택자용 동적 속성이기도 하다."""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self._selected = False
        self._caret = False
        self.setProperty("role", "resolution")
        self.setProperty("selected", False)
        self.setProperty("caret", False)
        # 호버 진입·이탈에 다시 그리게 한다 — ▾ 색이 호버 토큰을 따른다
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        # 가로: 자연 폭 이상으로 늘지 않고(Maximum), 최소 폭은 레이아웃을 묶지 않는다
        # (minimumSizeHint 1px). QPushButton 기본(Minimum)은 최소 폭 = 자연 폭이라
        # "pill 전부가 한 줄에" 있는 동안 카드 최소폭이 거기에 묶여, 창을 그 아래로
        # 줄일 수 없고 "안 들어가면 접는다" 판정(content/widget.py::_layoutRowThree)이
        # 영영 안 온다. 판정은 실제 폭이 아니라 naturalWidth()로 하므로, pill이
        # 실제로 쥐어짜이는 것은 판정이 도는 리사이즈 한 틱 안에서만이다.
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def minimumSizeHint(self) -> QSize:
        """가로 최소는 레이아웃을 묶지 않는다(1px) — 자연 폭은 naturalWidth()가 준다."""
        return QSize(1, super().minimumSizeHint().height())

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
        super().paintEvent(event)
        if not self._caret:
            return
        color = QColor(theme.current_tokens()[self.caretToken()])
        right = self.width() - 8  # 오른쪽 가장자리 padding 8 안쪽
        left = right - CARET_WIDTH
        mid_y = self.height() / 2
        half_h = CARET_WIDTH / 4  # 높이 = 폭의 절반
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawPolygon(
            QPolygonF(
                [
                    QPointF(left, mid_y - half_h),
                    QPointF(right, mid_y - half_h),
                    QPointF((left + right) / 2, mid_y + half_h),
                ]
            )
        )
        painter.end()
