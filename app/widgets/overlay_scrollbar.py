"""스크롤 영역의 세로 스크롤바를 내용 위에 겹쳐 그린다 — 자리를 차지하지 않는다 (#611).

스크롤바가 자리를 차지하면 보일 때마다 뷰포트가 그 폭만큼 좁아진다. 띠 행은 그만큼 짧아지고, 행 안
버튼 · 글이 스크롤바에 붙거나 스크롤바가 나타나고 숨을 때마다 움직인다(오너 실기 2026-11-15). 이 막대는
뷰포트 오른쪽 끝 `zone` 폭 자리 위에 떠서 그려지고, 영역의 원래 스크롤바는 숨긴 채 값 · 범위만 맡는다.

- 평소에는 얇게(`thin`), 막대 자리에 포인터를 대거나 끄는 동안은 굵게(`thick`) 그린다. 둘 다 `zone`의
  가운데에 놓인다 — 내용은 `zone` 안쪽에 두면 막대와 겹치지 않는다
- 굴리면(값이 바뀌면) 나타나고, 가만히 두면 `HIDE_DELAY_MS` 뒤 흐려진다. 포인터가 막대 자리에 있거나
  끄는 동안은 흐려지지 않는다
- 애니메이션을 끄면(`animate`가 거짓) 늘 보이는 막대다 — 굵기도 바로 바뀐다
- 스크롤 범위가 없으면(내용이 다 보이면) 막대도 막대 자리도 없다 — 아래 내용이 포인터를 받는다

막대를 끄는 동작 · 바탕 누름 · 휠은 원래 스크롤바로 보낸다 — `sliderPressed` · `sliderReleased` ·
`actionTriggered` · 휠 이벤트 필터에 건 것(카드 목록의 `StepScroller`)이 그대로 돈다. 키보드 스크롤은 영역이
원래 스크롤바의 `triggerAction`으로 하므로 이 막대와 무관하게 돈다.

카드 목록(`app/widgets/view.py`)만 붙인다. 다른 스크롤 영역에 붙이려면 그 영역의 내용이 오른쪽 `zone`
안쪽에 끝나야 한다.
"""

from collections.abc import Callable

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QRectF, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPainterPath, QPaintEvent, QWheelEvent
from PySide6.QtWidgets import QAbstractScrollArea, QAbstractSlider, QApplication, QWidget

import app.theme as theme

# 굴린 뒤 · 포인터가 막대 자리를 떠난 뒤 흐려지기 시작할 때까지(ms) — 바로 흐려지면 굴린 결과의 자리를
# 읽을 틈이 없다
HIDE_DELAY_MS = 1000
FADE_IN_MS = 120  # 나타나는 시간 — 툴팁 나타남과 같은 값(#610)
FADE_OUT_MS = 240  # 흐려지는 시간 — 나타남보다 느리게 해 갑자기 꺼지는 느낌을 덜어 낸다
THICKEN_MS = 120  # 얇음 ↔ 굵음 시간
MIN_HANDLE = 28  # 손잡이 최소 길이(px) — style.qss QScrollBar::handle의 min-height와 같다


class OverlayScrollBar(QWidget):
    """스크롤 영역 하나의 세로 스크롤바를 내용 위에 겹쳐 그리는 막대.

    영역의 자식으로 뷰포트 오른쪽 끝 `zone` 폭 자리에 놓인다. 만들면 영역의 세로 스크롤바 정책을
    `ScrollBarAlwaysOff`로 바꾼다 — 원래 스크롤바는 숨은 채 값 · 범위 · 동작을 맡는다.
    """

    def __init__(
        self,
        area: QAbstractScrollArea,
        zone: int,
        thin: int,
        thick: int,
        animate: Callable[[], bool],
    ) -> None:
        """
        Args:
            area: 막대를 붙일 스크롤 영역 — 이 위젯의 부모가 된다
            zone: 막대 자리의 폭(px) — 뷰포트 오른쪽 끝에서 이만큼. 포인터를 받는 자리다
            thin: 평소 막대 굵기(px)
            thick: 포인터를 대거나 끄는 동안의 굵기(px) — `zone` 이하
            animate: 부를 때마다 애니메이션을 쓸지 답한다(설정이 바뀌면 다음 동작부터 따른다)
        """
        super().__init__(area)
        self._area = area
        self._zone = zone
        self._thin = thin
        self._thick = thick
        self._animate = animate
        self._opacity = 0.0 if animate() else 1.0  # 0 ~ 1 — 흐려짐 · 나타남
        self._thickness = float(thin)  # 지금 그리는 굵기(px)
        self._hovered = False  # 포인터가 막대 자리에 있는지
        self._dragOffset: float | None = None  # 끄는 중이면 누른 자리 ↔ 손잡이 위끝(px)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._fade: QVariantAnimation | None = None  # 흐려짐 · 나타남 — 움직이는 동안만 있다
        self._grow: QVariantAnimation | None = None  # 얇음 ↔ 굵음 — 움직이는 동안만 있다
        self._hideTimer = QTimer(self)
        self._hideTimer.setSingleShot(True)
        self._hideTimer.setInterval(HIDE_DELAY_MS)
        self._hideTimer.timeout.connect(self._fadeOutIfIdle)

        bar = area.verticalScrollBar()
        bar.rangeChanged.connect(self._onRangeChanged)
        bar.valueChanged.connect(self._onValueChanged)
        self._viewport = area.viewport()
        self._viewport.installEventFilter(self)  # 뷰포트 크기 · 자리 → 막대 자리
        self._place()
        self._onRangeChanged(bar.minimum(), bar.maximum())

    # ---- 읽기(테스트 · 그림) ----

    def opacity(self) -> float:
        """지금 그리는 불투명도 0 ~ 1."""
        return self._opacity

    def thickness(self) -> float:
        """지금 그리는 막대 굵기(px)."""
        return self._thickness

    def thinWidth(self) -> int:
        """평소 굵기(px)."""
        return self._thin

    def thickWidth(self) -> int:
        """포인터를 대거나 끄는 동안의 굵기(px)."""
        return self._thick

    def handleRect(self) -> QRectF:
        """손잡이 사각형(이 위젯 좌표) — 지금 굵기로, 막대 자리 가운데에 놓인다."""
        return self._handleRect(self._thickness)

    # ---- 자리 · 보임 ----

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """뷰포트의 크기 · 자리가 바뀌면 막대 자리를 따라 옮긴다."""
        # 종류부터 본다 — 영역이 지워지는 동안 오는 이벤트에서 영역(C++ 객체가 이미 없음)을 건드리지 않는다
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Move) and watched is self._viewport:
            self._place()
        return False

    def _place(self) -> None:
        """뷰포트 오른쪽 끝 `zone` 폭 자리에 놓고 맨 위로 올린다."""
        viewport = self._area.viewport().geometry()
        self.setGeometry(
            viewport.right() + 1 - self._zone, viewport.top(), self._zone, viewport.height()
        )
        self.raise_()

    def _onRangeChanged(self, minimum: int, maximum: int) -> None:
        """범위가 있을 때만 보인다 — 없어지면 상태를 처음으로 되돌린다."""
        needed = maximum > minimum
        if needed == (not self.isHidden()):
            self.update()
            return
        self._hideTimer.stop()
        self._fade = _drop(self._fade)
        self._grow = _drop(self._grow)
        self._hovered = False
        self._dragOffset = None
        self._thickness = float(self._thin)
        # 새로 생긴 막대는 흐린 채로 시작한다(굴려야 나타난다) — 애니메이션이 꺼져 있으면 늘 보인다
        self._opacity = 0.0 if self._animate() else 1.0
        self.setVisible(needed)
        self.update()

    def _onValueChanged(self, _value: int) -> None:
        """굴렸다 — 막대를 보이고, 가만히 두면 흐려지게 한다."""
        self.update()
        if self.isVisible():
            self.reveal()

    def reveal(self) -> None:
        """막대를 나타내고 흐려지는 시계를 다시 건다(포인터가 있거나 끄는 중이면 걸지 않는다)."""
        self._fadeTo(1.0)
        self._hideTimer.stop()
        if not self._busy():
            self._hideTimer.start()

    def _busy(self) -> bool:
        """포인터가 막대 자리에 있거나 끄는 중인지 — 그동안은 흐려지지 않는다."""
        return self._hovered or self._dragOffset is not None

    def _fadeOutIfIdle(self) -> None:
        if not self._busy():
            self._fadeTo(0.0)

    def _fadeTo(self, target: float) -> None:
        """불투명도를 target으로 — 애니메이션이 꺼져 있으면 늘 1이다."""
        if not self._animate():
            self._fade = _drop(self._fade)
            self._setOpacity(1.0)
            return
        if self._fade is not None and self._fade.endValue() == target:
            return  # 이미 그리로 가는 중
        self._fade = _drop(self._fade)
        if self._opacity == target:
            return
        duration = FADE_IN_MS if target > self._opacity else FADE_OUT_MS
        self._fade = self._start(self._opacity, target, duration, self._setOpacity, "_fade")

    def _setOpacity(self, value: float) -> None:
        if value != self._opacity:
            self._opacity = value
            self.update()

    def _growTo(self, target: int) -> None:
        """굵기를 target으로 — 애니메이션이 꺼져 있으면 바로."""
        self._grow = _drop(self._grow)
        if not self._animate():
            self._setThicknessNow(target)
            return
        if self._thickness == target:
            return
        self._grow = self._start(
            self._thickness, float(target), THICKEN_MS, self._setThicknessNow, "_grow"
        )

    def _start(
        self, start: float, end: float, duration: int, step: Callable[[float], None], slot: str
    ) -> QVariantAnimation:
        """start → end 애니메이션을 만들어 돌린다 — 끝나면 지우고 `slot` 속성을 비운다.

        애니메이션 객체는 움직이는 동안만 막대의 자식으로 있다(카드 모션 · `StepScroller`와 같다, #571).
        """
        animation = QVariantAnimation(self)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.setDuration(duration)
        animation.setStartValue(float(start))
        animation.setEndValue(float(end))
        animation.valueChanged.connect(lambda value: step(float(value)))
        animation.finished.connect(lambda: self._finished(animation, slot))
        animation.start()
        return animation

    def _finished(self, animation: QVariantAnimation, slot: str) -> None:
        if getattr(self, slot) is animation:
            setattr(self, slot, None)
        animation.deleteLater()

    def _setThicknessNow(self, value: float) -> None:
        if value != self._thickness:
            self._thickness = value
            self.update()

    # ---- 그리기 ----

    def _handleRect(self, thickness: float) -> QRectF:
        bar = self._area.verticalScrollBar()
        height = float(self.height())
        span = bar.maximum() - bar.minimum()
        if span <= 0:
            return QRectF()
        length = max(float(MIN_HANDLE), height * bar.pageStep() / (span + bar.pageStep()))
        length = min(length, height)
        top = (height - length) * (bar.sliderPosition() - bar.minimum()) / span
        return QRectF((self._zone - thickness) / 2, top, thickness, length)

    def paintEvent(self, event: QPaintEvent) -> None:
        """손잡이만 그린다 — 막대 자리의 바탕은 그리지 않는다(아래 행이 비친다)."""
        opacity = self._opacity if self._animate() else 1.0
        if opacity <= 0.0:
            return
        rect = self._handleRect(self._thickness)
        if rect.isEmpty():
            return
        token = "scrollHandleHover" if self._busy() else "scrollHandle"
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(opacity)
        path = QPainterPath()
        radius = rect.width() / 2  # 양끝이 반원
        path.addRoundedRect(rect, radius, radius)
        painter.fillPath(path, QColor(theme.current_tokens()[token]))
        painter.end()

    # ---- 포인터 ----

    def enterEvent(self, event: QEvent) -> None:
        """막대 자리에 들어왔다 — 나타나 굵어지고, 있는 동안은 흐려지지 않는다."""
        self._hovered = True
        self._hideTimer.stop()
        self._fadeTo(1.0)
        self._growTo(self._thick)
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        """막대 자리를 떠났다 — 끄는 중이 아니면 얇아지고 가만히 두면 흐려진다."""
        self._hovered = False
        if self._dragOffset is None:
            self._growTo(self._thin)
            self._hideTimer.start()
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """손잡이를 누르면 끌기를 시작하고, 바탕을 누르면 한 페이지 움직인다(원래 스크롤바 동작으로)."""
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        bar = self._area.verticalScrollBar()
        y = event.position().y()
        # 누를 자리는 굵은 손잡이 기준이다 — 얇게 그려져 있어도 막대 자리 전체 폭이 누를 자리다
        handle = self._handleRect(self._thick)
        self.reveal()
        if handle.top() <= y <= handle.bottom():
            self._dragOffset = y - handle.top()
            self._hideTimer.stop()
            self._growTo(self._thick)
            bar.setSliderDown(True)
        elif y < handle.top():
            bar.triggerAction(QAbstractSlider.SliderAction.SliderPageStepSub)
        else:
            bar.triggerAction(QAbstractSlider.SliderAction.SliderPageStepAdd)
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """끄는 중이면 손잡이 위끝이 포인터를 따라가게 값을 옮긴다."""
        if self._dragOffset is None:
            event.ignore()
            return
        bar = self._area.verticalScrollBar()
        handle = self._handleRect(self._thick)
        room = self.height() - handle.height()
        span = bar.maximum() - bar.minimum()
        if room > 0 and span > 0:
            top = min(max(event.position().y() - self._dragOffset, 0.0), room)
            bar.setSliderPosition(bar.minimum() + round(top / room * span))
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """끌기를 끝낸다 — 원래 스크롤바의 sliderReleased가 칸 맞춤을 한다."""
        if self._dragOffset is None or event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        self._dragOffset = None
        self._area.verticalScrollBar().setSliderDown(False)
        if not self._hovered:
            self._growTo(self._thin)
        self.reveal()
        event.accept()

    def wheelEvent(self, event: QWheelEvent) -> None:
        """막대 위의 휠은 원래 스크롤바로 보낸다 — 그 스크롤바에 건 휠 처리가 그대로 돈다."""
        QApplication.sendEvent(self._area.verticalScrollBar(), event)


def _drop(animation: QVariantAnimation | None) -> None:
    """돌던 애니메이션을 그 자리에서 멈추고 지운다 — 없으면 아무것도 하지 않는다. 늘 None을 돌려준다."""
    if animation is not None:
        animation.stop()
        animation.deleteLater()
