"""스크롤 영역을 칸 단위로 굴린다 — 휠 한 칸에 한 칸, 스크롤 위치는 늘 칸 경계 (#599).

카드 목록(한 칸 = 카드 하나 + 카드 사이)이 쓴다(오너 2026-11-14 「한 칸씩 내리게」).

칸 경계는 0, 칸, 2칸, …에 맨 끝(스크롤 범위의 최댓값)을 더한 자리다. 전체 높이가 칸의 배수가 아니면
(목록 위아래 여백 · 창 높이) 맨 끝은 칸 경계가 아니다 — 마지막 카드가 다 보이도록 맨 끝도 설 수 있는
자리로 둔다. 맨 끝 바로 앞 칸에서 한 칸 내리면 맨 끝으로, 맨 끝에서 한 칸 올리면 그 앞 칸 경계로 간다.
"""

import math

from PySide6.QtCore import QAbstractAnimation, QEasingCurve, QEvent, QObject, QVariantAnimation
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QAbstractScrollArea, QAbstractSlider

# 한 칸을 움직이는 휠의 양 — 마우스 휠 한 칸(angleDelta 120, 1/8도 단위 — Qt 문서)이 한 칸이다(오너
# 2026-11-14 「한 칸씩 내리게」). 고해상도 휠 · 터치패드의 작은 값은 이만큼 모아 한 칸씩
WHEEL_NOTCH = 120

# 휠로 칸을 옮기는 애니메이션 시간(ms, ease-out) — 오너 2026-11-14 「부드럽게」. 0이면 바로 옮긴다.
# 굴리는 중에 다음 휠이 오면 목표 칸을 더해 지금 자리에서 다시 이 시간으로 간다
SCROLL_ANIMATION_MS = 120

# 스크롤바 동작 가운데 아래로 · 위로 걸음 — 그 자리를 칸 경계로 맞춘다. 페이지는 뷰포트 높이라 칸의 배수가
# 아니고, 화살표 한 걸음도 칸 경계가 아닌 자리(맨 끝 · 굴리다 멈춘 자리)에서 시작하면 경계를 벗어난다
_STEPS_DOWN = (
    QAbstractSlider.SliderAction.SliderSingleStepAdd,
    QAbstractSlider.SliderAction.SliderPageStepAdd,
)
_STEPS_UP = (
    QAbstractSlider.SliderAction.SliderSingleStepSub,
    QAbstractSlider.SliderAction.SliderPageStepSub,
)


class StepScroller(QObject):
    """스크롤 영역 하나의 세로 스크롤을 칸 단위로 맡는다.

    영역이 `wheelEvent`에서 `wheel`을 부른다. 스크롤바 위의 휠은 스크롤바가 받으므로(한 번에 휠 줄 수 ×
    singleStep만큼 간다) 스크롤바에 건 이벤트 필터가 `wheel`로 돌린다(#608 리뷰). 그 밖(스크롤바 화살표 ·
    손잡이 · 바탕 누름 · 범위 바뀜)은 스스로 스크롤바 시그널에 건다.

    - 휠: 한 칸(`WHEEL_NOTCH`)에 한 칸. pixelDelta가 있으면(터치패드 등) 칸 높이만큼의 픽셀을, 없으면
      angleDelta를 한 칸만큼 모은다. 방향이나 단위가 바뀌면 모은 값을 버린다. 이 방향으로 더 움직일 수
      없으면 받지 않아 Qt가 부모로 넘긴다
    - 휠로 옮기는 자리와 손잡이를 놓은 뒤 맞추는 자리는 `SCROLL_ANIMATION_MS` 동안 미끄러져 간다.
      굴리는 중의 다음 휠은 목표에 더한다. 스크롤바를 직접 만지면 그 자리에서 멈춘다
    - 화살표는 다음 칸 경계(singleStep = 칸), 바탕 누름(페이지)은 넘어간 칸을 버린 칸 경계
    - 범위가 바뀌어(창 크기 · 카드 추가 · 지우기 · 필터) 위치가 칸 경계를 벗어나면 가장 가까운 칸 경계로
      바로 옮긴다. 맨 끝에 있던 목록은 새 맨 끝에 남는다 — 마지막 카드가 다 보인 채다

    애니메이션 객체는 굴리는 동안만 영역의 자식으로 있다 — 끝나면 지운다(카드의 모션 객체는 움직이는
    동안만 있다, #571).
    """

    def __init__(self, area: QAbstractScrollArea, cell: int) -> None:
        super().__init__(area)
        self._area = area
        self._cell = cell  # 한 칸 높이(px)
        self._lastMaximum = area.verticalScrollBar().maximum()  # 바로 앞 범위의 맨 끝
        self._rest = 0  # 아직 한 칸 몫이 차지 않은 휠 값 — 아래 _byPixels의 단위
        self._byPixels = False  # _rest가 pixelDelta(px) 값인지 — 아니면 angleDelta
        self._target = 0  # 굴리는 중이면 가는 자리 — 굴리지 않을 때는 쓰지 않는다
        self._animation: QVariantAnimation | None = None
        bar = area.verticalScrollBar()
        bar.setSingleStep(cell)
        bar.sliderReleased.connect(self._snapReleased)
        bar.sliderPressed.connect(self.stop)
        bar.actionTriggered.connect(self._onBarAction)
        bar.rangeChanged.connect(self._onRangeChanged)
        bar.installEventFilter(self)  # 스크롤바 위의 휠 — 이 스크롤바 하나에만 건다

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        """스크롤바 위의 휠을 `wheel`로 돌린다 — 받았으면 스크롤바의 기본 처리(여러 줄 이동)를 막는다."""
        if event.type() == QEvent.Type.Wheel and obj is self._bar():
            self.wheel(event)
            return event.isAccepted()
        return False

    # ---- 칸 경계 ----

    def _last(self) -> int:
        """맨 끝 자리의 칸 번호 — 맨 끝이 칸 경계가 아니면 그 앞 칸 다음 번호다."""
        return math.ceil(self._bar().maximum() / self._cell)

    def _place(self, index: int) -> int:
        """칸 번호의 스크롤 위치 — 맨 끝 칸은 스크롤 범위의 최댓값."""
        bar = self._bar()
        index = min(max(index, 0), self._last())
        return min(index * self._cell, bar.maximum())

    def _index(self, value: int) -> int:
        """스크롤 위치에서 가장 가까운 칸 경계의 칸 번호."""
        bar = self._bar()
        value = min(max(value, 0), bar.maximum())
        below = value // self._cell
        candidates = (below, below + 1)
        return min(candidates, key=lambda index: (abs(self._place(index) - value), index))

    def boundary(self, value: int) -> int:
        """value에서 가장 가까운 칸 경계(맨 끝 포함)."""
        return self._place(self._index(value))

    def isOnBoundary(self, value: int) -> bool:
        """value가 칸 경계(맨 끝 포함)인지."""
        return self.boundary(value) == value

    def revealPlace(self, top: int, bottom: int) -> int:
        """콘텐츠의 top ~ bottom(끝 포함 안 함)이 뷰포트 안에 들도록 가장 적게 움직인 칸 경계.

        이미 다 보이면 지금 위치. 위로 벗어났으면 top이 들어오는 칸 경계까지 올리고, 아래로 벗어났으면
        bottom이 들어오는 칸 경계까지 내린다. 뷰포트보다 크면 위를 먼저 보인다.
        """
        value = self.target()
        height = self._area.viewport().height()
        if top < value:
            return self._place(top // self._cell)
        if bottom > value + height:
            index = math.ceil((bottom - height) / self._cell)
            place = self._place(index)
            return place if place <= top else self._place(top // self._cell)
        return value

    # ---- 굴리기 ----

    def target(self) -> int:
        """가려는 스크롤 위치 — 굴리는 중이면 가는 자리, 아니면 지금 위치."""
        if self.isAnimating():
            return self._target
        return self._bar().value()

    def isAnimating(self) -> bool:
        """휠로 옮긴 자리로 아직 미끄러져 가는 중인지."""
        animation = self._animation
        return animation is not None and animation.state() == QAbstractAnimation.State.Running

    def wheel(self, event: QWheelEvent) -> None:
        """휠 한 칸에 한 칸씩 움직인다 — 더 움직일 수 없는 방향이면 받지 않는다(ignore)."""
        pixels = event.pixelDelta().y()
        by_pixels = bool(pixels)
        amount = pixels if by_pixels else event.angleDelta().y()
        if not amount:
            event.ignore()
            return
        if by_pixels != self._byPixels or self._rest * amount < 0:
            self._rest = 0
        self._byPixels = by_pixels
        bar = self._bar()
        target = self.target()
        # 휠을 위로(+) 굴리면 위치가 줄어든다 — 그 방향의 끝이면 부모에 넘긴다
        if (amount > 0 and target <= bar.minimum()) or (amount < 0 and target >= bar.maximum()):
            self._rest = 0
            event.ignore()
            return
        self._rest += amount
        unit = self._cell if by_pixels else WHEEL_NOTCH
        steps = int(self._rest / unit)  # 0 쪽으로 버린다 — 남은 값은 다음 휠에 더한다
        self._rest -= steps * unit
        if steps:
            self.scrollTo(self._place(self._index(target) - steps))
        event.accept()

    def scrollTo(self, value: int, animate: bool = True) -> None:
        """스크롤 위치를 value에서 가장 가까운 칸 경계로 — 숨어 있거나 시간이 0이면 바로 놓는다."""
        bar = self._bar()
        target = self.boundary(value)
        self.stop()
        if (
            not animate
            or SCROLL_ANIMATION_MS <= 0
            or not self._area.isVisible()
            or target == bar.value()
        ):
            bar.setValue(target)
            return
        self._target = target
        animation = self._animation = QVariantAnimation(self._area)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.setDuration(SCROLL_ANIMATION_MS)
        animation.setStartValue(bar.value())
        animation.setEndValue(target)
        animation.valueChanged.connect(self._onAnimationStep)
        animation.finished.connect(self._onAnimationFinished)
        animation.start()

    def stop(self) -> None:
        """굴리기를 그 자리에서 멈추고 애니메이션을 지운다 — 없으면 아무것도 하지 않는다."""
        animation, self._animation = self._animation, None
        if animation is not None:
            animation.stop()
            animation.deleteLater()

    def finish(self) -> None:
        """굴리는 중이면 멈추고 목표 자리(범위 안의 칸 경계)에 바로 놓는다."""
        if not self.isAnimating():
            return
        self.stop()
        self._bar().setValue(self.boundary(self._target))

    def _bar(self) -> QAbstractSlider:
        return self._area.verticalScrollBar()

    def _onAnimationStep(self, value: int) -> None:
        self._bar().setValue(round(value))

    def _onAnimationFinished(self) -> None:
        """굴리기가 끝났다 — 굴리는 동안 범위가 바뀌어 목표가 칸 경계가 아니게 됐으면 맞춘다."""
        self.stop()
        bar = self._bar()
        if not self.isOnBoundary(bar.value()):
            bar.setValue(self.boundary(bar.value()))

    def _onBarAction(self, action: int) -> None:
        """스크롤바를 직접 만졌다 — 굴리기를 멈추고, 화살표 · 페이지 걸음이면 칸 경계로 맞춘다.

        걸음은 시작 자리 다음(앞) 칸 경계까지는 꼭 가고, 페이지가 그보다 멀면 넘어간 칸을 버린 경계에 선다.

        신호가 올 때 sliderPosition은 이미 걸음만큼 옮겨져 있고 값에는 아직 들지 않았다(Qt 문서) —
        여기서 고친 자리가 값이 된다.
        """
        self.stop()
        action = QAbstractSlider.SliderAction(action)  # 시그널은 정수로 온다
        bar = self._bar()
        position = bar.sliderPosition()
        start = bar.value()
        if action in _STEPS_DOWN:
            index = max(self._floorIndex(position), self._floorIndex(start) + 1)
        elif action in _STEPS_UP:
            index = min(self._ceilIndex(position), self._ceilIndex(start) - 1)
        else:
            return
        bar.setSliderPosition(self._place(index))

    def _floorIndex(self, value: int) -> int:
        """value 이하에서 가장 큰 칸 경계의 칸 번호(맨 끝이면 맨 끝 칸)."""
        if value >= self._bar().maximum():
            return self._last()
        return max(value, 0) // self._cell

    def _ceilIndex(self, value: int) -> int:
        """value 이상에서 가장 작은 칸 경계의 칸 번호."""
        return math.ceil(max(value, 0) / self._cell)

    def _snapReleased(self) -> None:
        """손잡이를 놓은 자리를 가까운 칸 경계로 맞춘다."""
        self.scrollTo(self._bar().value())

    def _onRangeChanged(self, _minimum: int, _maximum: int) -> None:
        """범위가 바뀌었다 — 손잡이를 끄는 중이 아니고 굴리는 중도 아니면 가까운 칸 경계로 바로 옮긴다.

        앞 범위의 맨 끝(0보다 큼)에 있었으면 새 맨 끝으로 옮긴다. 굴리는 중이면 끝날 때
        맞춘다(`_onAnimationFinished`) — 다만 앞 맨 끝으로 가던 중이면 목표를 새 맨 끝으로 바꾼다. 그대로 두면
        앞 맨 끝에 선 뒤 새 범위의 칸 경계로 되돌아가 맨 끝에 남지 않는다(#608 리뷰). 신호가 올 때 값은 아직
        앞 범위의 값이다 — Qt는 이 신호 뒤에 값을 새 범위로 다시 자르고, 여기서 고친 값은 범위 안이라 그대로
        남는다.
        """
        bar = self._bar()
        was_end = self._lastMaximum > 0 and bar.value() >= self._lastMaximum
        heading_end = (
            self.isAnimating() and self._lastMaximum > 0 and self._target >= self._lastMaximum
        )
        self._lastMaximum = bar.maximum()
        if heading_end and self._target != bar.maximum():
            self._target = bar.maximum()
            self._animation.setEndValue(self._target)
        if bar.isSliderDown() or self.isAnimating():
            return
        if was_end:
            if bar.value() != bar.maximum():
                bar.setValue(bar.maximum())
            return
        value = min(bar.value(), bar.maximum())
        place = self.boundary(value)
        if place != bar.value():
            bar.setValue(place)
