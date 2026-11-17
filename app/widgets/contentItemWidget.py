# -*- coding: utf-8 -*-

################################################################################
## 목록 카드의 위젯 · 레이아웃 (#244 · #245 · #550 — 설계 §12.1 카드 배치)
##
## 손으로 유지하는 코드다(설계 기록 .ui는 #541에서 지웠다). 번역 컨텍스트
## `ContentItemWidget`의 글은 lupdate가 이 파일과 app/viewmodels/card_text.py에서 읽는다.
##
## 구조: 썸네일(110×62) | 12 | 세 줄(16 · 4 · 18 · 4 · 20) — 카드 높이 80(테두리 포함)
##   썸네일 줄이기(설정 키 — app/viewmodels/card_display.py)면 썸네일 68×38이 위 두 줄 옆에 서고 3줄은
##   썸네일 밑까지 넓어진다(#554 — 설계 §12.1). 목록 전체가 같은 모양이고 창 폭으로 바뀌지 않는다
##   1줄: 채널 그림(16, 원형) · 채널명(12 회색) · 날짜(12 회색) ··· (카드 버튼 자리)
##   2줄: 제목(14 굵게) — 라벨 폭은 글자 폭, 잘렸을 때만 글자 위 툴팁
##   3줄: 상태 원 아이콘 · 상태 글 ··· 저장 위치(폴더 윤곽 + 마지막 폴더, 기본 위치와 다를 때만)
##        · 정보(해상도 · 크기 · 길이 · 구간)
##   카드 버튼: 1줄 오른쪽 끝(위끝 = 썸네일 위끝)에 겹쳐 놓는 상자. 평소 숨고, 호버 · 카드 안
##        키보드 초점 때 그 상태의 버튼을 보인다. 그 밑은 1줄 높이 덮개가 카드 바탕으로 덮는다
##        (그림만 — 클릭은 밑으로)
##   하단 progressBar: 카드 바닥 전체 폭 오버레이(받는 중 · 일시정지는 진행만큼, 완료 · 실패는 없음 —
##        카드 전체 색조, #571)
##   배경 채움(설정 키)이면 막대 대신 카드 면(CardFace)이 진행만큼 왼쪽부터 칠한다
##   체크 원(#561): 선택 모드에서만 썸네일(CardThumbnail)이 왼쪽 위에 그린다 — 위젯이 아니라 그림이다.
##        썸네일 위라 줄 자리 · 카드 폭은 그대로다
## 줄 높이는 고정이다 — 상태가 바뀌어도 줄 자리 · 카드 높이가 같다(목록이 들썩이면 안 된다).
## 크기 · 간격은 theme.METRICS, 글자 · 색은 전역 QSS(theme 토큰)가 정한다.
################################################################################

from PySide6.QtCore import (
    QCoreApplication,
    QEasingCurve,
    QMetaObject,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QVariantAnimation,
)
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QSizePolicy,
    QStyle,
    QStyleOptionProgressBar,
    QVBoxLayout,
    QWidget,
)

import app.theme as theme
from app.viewmodels import card_display
from app.widgets.eliding_label import ElidingLabel
from app.widgets.icons import STATUS_GRID, STATUS_STROKE, IconButton
from app.widgets.menu_button import StateBubble


def _level(amount: float) -> int:
    """0 ~ 1 비율을 0 ~ 255 단계로 묶는다 — 화면에 그려지는 색 단위다(그 밑의 변화는 화소가 같다)."""
    return round(max(0.0, min(1.0, amount)) * 255)


BAND_GROW_MS = (
    120  # 고를 때 왼쪽 띠가 행 가운데에서 위아래로 자라는 시간(ms, OutCubic — 오너 2026-11-15)
)
BAND_SHRINK_MS = 80  # 풀 때 띠가 줄어드는 시간(ms)


def mix(start: QColor, end: QColor, amount: float) -> QColor:
    """`start`에서 `end`로 `amount`(0 ~ 1)만큼 간 색 — 불투명한 두 색의 sRGB 선형 섞기다 (#571)."""
    t = max(0.0, min(1.0, amount))
    return QColor.fromRgbF(
        start.redF() + (end.redF() - start.redF()) * t,
        start.greenF() + (end.greenF() - start.greenF()) * t,
        start.blueF() + (end.blueF() - start.blueF()) * t,
    )


class CardFace(QFrame):
    """카드 면 — QSS가 바탕 · 테두리 · 색조를 그리고, 그 위에 모션 색조와 고른 카드 표시를 그린다 (#554).

    카드마다 붙는 효과 객체가 아니라 값 몇 개(고름, 띠 높이 비율, 아래 행과 잇는지, 모션 동안의 색조 토큰 ·
    세기)만 든다. 띠 애니메이션 객체는 움직이는 동안만 있다. 진행 표시는 카드 아래끝 막대 하나다 — 배경 채움
    방식은 없앴다(설계 §13, #611).

    완료 · 실패 모션(#571) 동안은 QSS 색조(`tint` 속성) 대신 면이 색조를 세기만큼 바탕 위에 칠한다
    (`setWash`) — QSS는 속성 값마다 한 색이라 서서히 나타나게 할 수 없다. 모션이 끝나면 카드가 `tint`
    속성을 걸고 칠하기를 걷는다. 세기 · 섞기는 1/255 단위로 묶어 화소가 바뀌지 않으면 다시 그리지 않는다.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._selected = False  # 선택 모드에서 고른 카드 — 강조색 테두리(#561)
        self._band = 0.0  # 왼쪽 띠의 높이 비율 0 ~ 1 — 고르면 1로 자라고 풀면 0으로 줄어든다(#611)
        self._joinBelow = False  # 아래 행도 골라 띠가 구분선 자리까지 이어지는지(#611)
        self._bandMotion: QVariantAnimation | None = None  # 띠가 자라고 줄어드는 동안만 있다
        self._washToken = ""  # 모션 동안 칠하는 색조 토큰 — 빈 글이면 없음(#571)
        self._wash = 0  # 색조 세기 0 ~ 255

    def isSelected(self) -> bool:
        """고른 카드의 강조색 테두리를 그리는지."""
        return self._selected

    def setSelected(self, on: bool, animate: bool = False) -> None:
        """고른 행 표시를 켜고 끈다 — 고른 면(QSS `picked`, 옅은 강조색)과 왼쪽 강조색 띠. 줄 배치는 그대로다.

        바탕은 늘 바로 바뀐다. `animate`면 띠가 행 세로 가운데에서 위아래로 자라고(`BAND_GROW_MS`) 풀 때
        줄어든다(`BAND_SHRINK_MS`) — 목록이 카드 한 장만 바뀔 때 넘긴다(#611).
        """
        if on != self._selected:
            self._selected = on
            self.setProperty("picked", on)
            theme.repolish(self)
        self._moveBand(1.0 if on else 0.0, animate)

    def band(self) -> float:
        """왼쪽 띠의 지금 높이 비율 0 ~ 1."""
        return self._band

    def bandMotion(self) -> QVariantAnimation | None:
        """띠가 자라거나 줄어드는 애니메이션 — 움직이지 않으면 None."""
        return self._bandMotion

    def joinsBelow(self) -> bool:
        """띠가 아래 구분선 자리까지 이어지는지."""
        return self._joinBelow

    def setJoinBelow(self, on: bool) -> None:
        """아래 행도 골랐으면 띠를 구분선 자리까지 칠해 두 행의 띠를 한 줄로 잇는다 — 목록이 정한다(#611)."""
        if on != self._joinBelow:
            self._joinBelow = on
            self._repaintBand()

    def bandRect(self) -> QRectF:
        """왼쪽 띠 사각형(면 좌표) — 띠가 없으면 빈 사각형.

        행 위끝 ~ 구분선 위가 다 자란 띠다. 자라는 동안은 그 세로 가운데를 중심으로 높이가 비율만큼이다. 다
        자랐고 아래 행과 이으면 구분선 자리(아래끝 1px)까지 칠한다.
        """
        if self._band <= 0.0:
            return QRectF()
        width = theme.METRICS["cardSelectBar"]
        full = self.height() - theme.METRICS["cardSeparator"]
        if self._band >= 1.0:
            height = self.height() if self._joinBelow and self._selected else full
            return QRectF(0, 0, width, height)
        height = full * self._band
        return QRectF(0, (full - height) / 2, width, height)

    def _moveBand(self, target: float, animate: bool) -> None:
        """띠 높이 비율을 target으로 — `animate`가 아니면 바로, 도는 애니메이션은 그 자리에서 멈춘다."""
        if self._bandMotion is not None:
            if animate and self._bandMotion.endValue() == target:
                return  # 이미 그리로 가는 중
            self._bandMotion.stop()
            self._bandMotion.deleteLater()
            self._bandMotion = None
        if not animate or self._band == target:
            self._setBand(target)
            return
        motion = QVariantAnimation(self)
        motion.setEasingCurve(QEasingCurve.Type.OutCubic)
        motion.setDuration(BAND_GROW_MS if target > self._band else BAND_SHRINK_MS)
        motion.setStartValue(self._band)
        motion.setEndValue(target)
        motion.valueChanged.connect(self._onBandStep)
        motion.finished.connect(self._onBandDone)
        self._bandMotion = motion
        motion.start()

    def _onBandStep(self, value: float) -> None:
        self._setBand(float(value))

    def _onBandDone(self) -> None:
        motion, self._bandMotion = self._bandMotion, None
        if motion is not None:
            self._setBand(float(motion.endValue()))
            motion.deleteLater()

    def _setBand(self, value: float) -> None:
        if value != self._band:
            self._band = value
            self._repaintBand()

    def _repaintBand(self) -> None:
        """띠 자리를 다시 그린다 — 띠는 면과 그 위의 progress bar가 함께 그린다."""
        self.update(QRect(0, 0, theme.METRICS["cardSelectBar"], self.height()))
        for bar in self.findChildren(
            CardProgress, options=Qt.FindChildOption.FindDirectChildrenOnly
        ):
            bar.update()

    def wash(self) -> tuple[str, float]:
        """(모션 동안 칠하는 색조 토큰, 세기 0 ~ 1) — 칠하지 않으면 ("", 0.0)."""
        return self._washToken, self._wash / 255

    def setWash(self, token: str, amount: float) -> None:
        """색조 `token`을 세기 `amount`(0 ~ 1)로 바탕 위에 칠한다 — 토큰이 빈 글이거나 0이면 걷는다 (#571)."""
        level = _level(amount) if token else 0
        token = token if level else ""
        if (token, level) != (self._washToken, self._wash):
            self._washToken, self._wash = token, level
            self.update()

    def _step(self) -> str:
        """바탕 단계 — "" · "Hover" · "Picked" · "PickedHover" (QSS 바탕 규칙과 같은 차례, #611 176o)."""
        hovered = "Hover" if self.property("hover") is True else ""
        return ("Picked" + hovered) if self._selected else hovered

    def _washColor(self) -> QColor:
        """모션 동안 칠하는 색조 — 지금 호버 단계의 색조(QSS 색조 규칙과 같은 토큰, #571)."""
        return QColor(theme.current_tokens()[self._washToken + self._step()])

    def colorAt(self, x: int) -> QColor:
        """면 좌표 `x`에서 보이는 바탕색 — 색조 · 호버를 따른다(카드 버튼 덮개가 쓴다)."""
        tokens = theme.current_tokens()
        step = self._step()
        tint = self.property("tint")
        if tint in ("finished", "failed"):
            name = "cardTintFinished" if tint == "finished" else "cardTintFailed"
            ground = QColor(tokens[name + step])
        else:
            ground = QColor(tokens["card" + step + "Bg"])  # cardBg · cardHoverBg · cardPicked…Bg
        if self._washToken:
            ground = mix(ground, self._washColor(), self._wash / 255)
        return ground

    def paintEvent(self, event: QPaintEvent) -> None:
        """QSS 바탕 · 구분선 위에 모션 색조(행 전체, 구분선 위)와 고른 행의 왼쪽 강조색 띠를 그린다."""
        super().paintEvent(event)
        if self._washToken:
            inner = QRectF(0, 0, self.width(), self.height() - theme.METRICS["cardSeparator"])
            painter = QPainter(self)
            wash = self._washColor()
            wash.setAlpha(self._wash)
            painter.fillRect(inner, wash)
            painter.end()
        draw_picked_band(self, QPointF(0, 0), self)


def draw_picked_band(face: CardFace, origin: QPointF, device: QWidget) -> None:
    """고른 행의 왼쪽 끝 강조색 세로 띠(`CardFace.bandRect`)를 `device`에 그린다 (#561 · #611).

    호버 · 눌림이어도 강조색 그대로다. 카드 면과 그 위의 progress bar가 같은 띠를 각자 그린다 — progress
    bar가 면 위에 놓여 면이 그린 띠의 아래 끝을 가리므로 그 자리는 progress bar가 자기 위에 덧그린다(띠가
    진행 막대 위에 보인다 — 연달아 고른 띠가 받는 중 카드에서도 끊기지 않는다). `origin`은 `device` 좌표에서
    면의 왼쪽 위다.
    """
    rect = face.bandRect()
    if rect.isEmpty():
        return
    painter = QPainter(device)
    painter.fillRect(rect.translated(origin), QColor(theme.current_tokens()["accent"]))
    painter.end()


class CardProgress(QProgressBar):
    """카드 바닥의 progress bar — 고른 카드면 자기 자리에 걸친 강조색 테두리를 덧그린다 (#561).

    progress bar는 카드 면(`CardFace`)의 자식으로 면 위에 놓여, 면이 그린 고름 테두리의 아래 변 안쪽 1px과
    양 끝을 가린다. 카드마다 위젯을 더하지 않고 이미 있는 progress bar가 같은 테두리를 자기 좌표로 옮겨
    그린다 — 값 하나(`_picked`)만 든다.

    완료 · 실패 모션(#571) 동안은 채운 몫에 새 상태 색을 섞기만큼 덧칠하고 막대 전체를 불투명도만큼 흐리게
    그린다(`setLook`) — QSS는 상태마다 한 색이라 색이 서서히 바뀌게 할 수 없다. 카드마다 효과 객체를
    붙이지 않고 값 셋(덧칠 토큰 · 섞기 · 불투명도)만 든다. 1/255 단위로 묶어 화소가 같으면 다시 그리지 않는다.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._picked = False  # 카드를 골랐는지 — 강조색 테두리를 덧그린다
        self._toward = ""  # 채운 몫에 덧칠하는 색 토큰 — 빈 글이면 덧칠 없음(#571)
        self._blend = 0  # 덧칠 세기 0 ~ 255
        self._opacity = 255  # 막대 불투명도 0 ~ 255(#571)

    def look(self) -> tuple[str, float, float]:
        """(덧칠 색 토큰, 덧칠 세기 0 ~ 1, 불투명도 0 ~ 1)."""
        return self._toward, self._blend / 255, self._opacity / 255

    def setLook(self, toward: str, blend: float, opacity: float) -> None:
        """채운 몫에 `toward` 색을 `blend`만큼 덧칠하고 막대를 `opacity`로 그린다 — ("", 0, 1)이면 QSS 그대로."""
        blend_level = _level(blend) if toward else 0
        look = (toward if blend_level else "", blend_level, _level(opacity))
        if look != (self._toward, self._blend, self._opacity):
            self._toward, self._blend, self._opacity = look
            self.update()

    def isPicked(self) -> bool:
        """고른 카드의 테두리를 덧그리는지."""
        return self._picked

    def setPicked(self, on: bool) -> None:
        """고름 테두리 덧그리기를 켜고 끈다 — 카드가 `setSelected`에서 부른다."""
        if on != self._picked:
            self._picked = on
            self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        """progress bar를 그린 뒤, 카드 면의 왼쪽 띠가 걸치면 그 위에 덧그린다 — 띠가 막대 위에 보인다."""
        if self._toward or self._opacity < 255:
            self._paintLook()
        else:
            super().paintEvent(event)
        face = self.parentWidget()
        if isinstance(face, CardFace):
            draw_picked_band(face, QPointF(-self.x(), -self.y()), self)

    def _paintLook(self) -> None:
        """모션 동안의 막대 — QSS 막대를 불투명도로 그리고, 채운 몫에 덧칠 색을 섞기만큼 얹는다.

        QSS 막대는 스타일에 그리게 한다(`CE_ProgressBar` — `QProgressBar.paintEvent`와 같은 그리기). 덧칠은
        채운 몫의 사각형을 막대 아래 두 모서리(`cardRadius`)로 잘라 칠한다 — QSS 채운 몫과 같은 모양이다.
        """
        painter = QPainter(self)
        painter.setOpacity(self._opacity / 255)
        option = QStyleOptionProgressBar()
        self.initStyleOption(option)
        self.style().drawControl(QStyle.ControlElement.CE_ProgressBar, option, painter, self)
        span = self.maximum() - self.minimum()
        if self._toward and span > 0:
            done = (self.value() - self.minimum()) / span * self.width()
            radius = theme.METRICS["cardRadius"]
            bar = QRectF(self.rect())
            shape = QPainterPath()
            shape.addRoundedRect(
                bar.adjusted(0, -radius, 0, 0), radius, radius
            )  # 아래 두 모서리만 둥글다
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setClipPath(shape)
            color = QColor(theme.current_tokens()[self._toward])
            color.setAlphaF(self._blend / 255)
            painter.fillRect(QRectF(0, 0, done, bar.height()), color)
        painter.end()


class CardThumbnail(QLabel):
    """카드 썸네일 — 선택 모드에서는 왼쪽 위에 체크 원을 얹어 그린다 (#561).

    안 고른 카드는 빈 원(원 테두리 + 어두운 반투명 바탕), 고른 카드는 강조색 채움 + ✓ + 원 테두리다. 색은
    그릴 때마다 theme 토큰을 읽는다(실행 중 테마 전환) — 색 리터럴은 없다. 원은 위젯이 아니라 썸네일이
    그리는 그림이다 — 원 위를 눌러도 카드를 누른 것이고, 선택 모드를 드나들 때 카드마다 위젯을 만들거나
    보이고 숨기지 않는다(카드가 많아도 선택 모드에 들어가는 시간이 늘지 않는다).
    """

    # ✓가 꺾이는 세 점 — 지름 20(cardPickMark) 격자의 좌표. 지름에 비례해 줄인다
    CHECK = ((5.6, 10.4), (8.8, 13.6), (14.4, 7.0))

    def __init__(self, parent: QWidget, compact: bool) -> None:
        """체크 원 자리를 정한다 — 그리기는 카드가 켠다.

        Args:
            parent: 카드 면
            compact: 썸네일 줄이기인지 — 참이면 작은 지름(cardPickMarkCompact)
        """
        super().__init__(parent)
        self._markShown = False  # 체크 원을 그리는지 — 선택 모드
        self._markPicked = False  # 고른 카드의 원인지 — 채움 · ✓
        inset = theme.METRICS["cardPickMarkInset"]
        side = theme.METRICS["cardPickMarkCompact" if compact else "cardPickMark"]
        self._markRect = QRect(inset, inset, side, side)  # 원 자리(썸네일 좌표)

    def hasPickMark(self) -> bool:
        """체크 원을 그리는지 — 선택 모드에서만 참이다."""
        return self._markShown

    def isMarkPicked(self) -> bool:
        """체크 원이 고른 모양(강조색 채움 + ✓)인지."""
        return self._markPicked

    def pickMarkRect(self) -> QRect:
        """체크 원 자리(썸네일 좌표) — 원을 그리지 않을 때도 같은 자리다."""
        return QRect(self._markRect)

    def setPickMark(self, shown: bool, picked: bool) -> None:
        """체크 원을 그릴지 · 고른 모양인지 정한다 — 바뀔 때만, 화면에 나와 있을 때만 원 자리를 다시 그린다.

        선택 모드를 드나들면 목록의 모든 카드가 이 함수를 부른다. 스크롤 밖 카드까지 `update()`를 걸면 카드
        1000장에서 들어가는 시간이 두 배가 넘게 든다 — 가려진 썸네일은 화면에 나올 때 새 모양으로 그려진다.
        """
        if (shown, picked) != (self._markShown, self._markPicked):
            self._markShown, self._markPicked = shown, picked
            if not self.visibleRegion().isEmpty():
                self.update(self._markRect)

    def paintEvent(self, event: QPaintEvent) -> None:
        """썸네일을 그리고, 선택 모드면 그 위에 지금 테마 색으로 체크 원을 그린다."""
        super().paintEvent(event)
        if not self._markShown:
            return
        tokens = theme.current_tokens()
        ring = theme.PICK_MARK_RING_WIDTH
        side = self._markRect.width()
        if self._markPicked:
            ground = QColor(tokens["accent"])
        else:
            ground = QColor(tokens["pickMarkScrim"])
            ground.setAlphaF(theme.PICK_MARK_SCRIM_ALPHA)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(self._markRect.topLeft())
        painter.setPen(QPen(QColor(tokens["pickMarkRing"]), ring))
        painter.setBrush(ground)
        painter.drawEllipse(QRectF(ring / 2, ring / 2, side - ring, side - ring))
        if self._markPicked:
            scale = side / theme.METRICS["cardPickMark"]
            (start_x, start_y), *rest = self.CHECK
            check = QPainterPath()
            check.moveTo(start_x * scale, start_y * scale)
            for x, y in rest:
                check.lineTo(x * scale, y * scale)
            # ✓ 선 — 상태 아이콘 규칙(16 격자에서 STATUS_STROKE)을 지름에 비례해 키운다(inbox 177)
            check_width = STATUS_STROKE * side / STATUS_GRID
            pen = QPen(QColor(tokens["onAccent"]), check_width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(check)
        painter.end()


class ChannelAvatar(QLabel):
    """채널 그림 — 받은 그림이 없으면(주소 없음 · 받기 실패) 앱이 그린 기본 그림을 보인다 (#554, 설계 §3).

    기본 그림은 원 바탕 위 사람 모양이다. 색은 그릴 때마다 theme 토큰을 읽는다(실행 중 테마 전환) — 색
    리터럴은 없다.
    """

    def paintEvent(self, event: QPaintEvent) -> None:
        """그림이 있으면 그대로, 없으면 기본 그림을 그린다."""
        pixmap = self.pixmap()
        if pixmap is not None and not pixmap.isNull():
            super().paintEvent(event)
            return
        tokens = theme.current_tokens()
        side = min(self.width(), self.height())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(tokens["thumbBg"]))
        painter.drawEllipse(QRectF(0, 0, side, side))
        painter.setBrush(QColor(tokens["textMuted"]))
        head = side * 0.36
        painter.drawEllipse(QRectF((side - head) / 2, side * 0.18, head, head))
        clip = QPainterPath()
        clip.addEllipse(QRectF(0, 0, side, side))
        painter.setClipPath(clip)
        body = side * 0.72
        painter.drawEllipse(QRectF((side - body) / 2, side * 0.62, body, body))

    def hasPicture(self) -> bool:
        """받은 그림을 보이고 있는지 — 거짓이면 기본 그림이다."""
        pixmap = self.pixmap()
        return pixmap is not None and not pixmap.isNull()


class HoverShade(QWidget):
    """카드 버튼 밑의 1줄 글을 카드 바탕으로 덮는 오버레이 — 그림만, 마우스는 밑으로 간다 (#550).

    왼쪽 ``FADE``는 투명 → 바탕색으로 흐려지고, 그 오른쪽은 바탕색으로 채운다. 바탕색은 그 자리에서
    보이는 카드 면의 색이다(호버 · 완료 · 실패 색조, `CardFace.colorAt`) — 그릴 때 읽는다.
    카드마다 붙는 효과 객체(QGraphicsEffect)가 아니라 그리기만 하는 위젯이다.
    """

    FADE = 24  # 흐려지는 폭(px) — 버튼 앞 글이 뚝 끊겨 보이지 않게
    SOLID = 8  # 버튼 상자 왼쪽 앞의 채우는 폭(px) — 버튼 간격과 같다

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, event: QPaintEvent) -> None:
        """지금 테마 · 호버의 카드 바탕색으로 흐림과 채움을 그린다."""
        frame = self.parentWidget()
        if isinstance(frame, CardFace):
            color = frame.colorAt(self.x() + self.width() - 1)
        else:
            color = QColor(theme.current_tokens()["cardBg"])
        clear = QColor(color)
        clear.setAlpha(0)
        fade = min(self.FADE, self.width())
        gradient = QLinearGradient(0, 0, fade, 0)
        gradient.setColorAt(0, clear)
        gradient.setColorAt(1, color)
        painter = QPainter(self)
        painter.fillRect(0, 0, fade, self.height(), gradient)
        painter.fillRect(fade, 0, self.width() - fade, self.height(), color)


def _row(parent: QWidget, name: str, height: int) -> tuple[QWidget, QHBoxLayout]:
    """높이를 고정한 한 줄 — 줄 자리가 글꼴 · 상태와 무관하게 같다."""
    row = QWidget(parent)
    row.setObjectName(name)
    row.setFixedHeight(height)
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    return row, layout


class Ui_ContentItemWidget(object):
    def setupUi(self, ContentItemWidget) -> None:
        if not ContentItemWidget.objectName():
            ContentItemWidget.setObjectName("ContentItemWidget")
        ContentItemWidget.resize(600, 80)

        pad = theme.METRICS["cardPadding"]
        pad_y = theme.METRICS["cardPadY"]
        gap = theme.METRICS["cardRowSpacing"]
        bar_h = theme.METRICS["barHeight"]
        button = theme.METRICS["cardButton"]
        status_icon = theme.METRICS["cardStatusIcon"]

        self.contentItemLayout = QVBoxLayout(ContentItemWidget)
        self.contentItemLayout.setObjectName("contentItemLayout")
        # 바깥 여백 0 — 카드 사이는 목록이 준다(cardGap), 좌우 위치는 목록 여백이 정한다
        self.contentItemLayout.setContentsMargins(0, 0, 0, 0)
        self.contentFrame = CardFace(ContentItemWidget)
        self.contentFrame.setObjectName("contentFrame")
        self.contentFrame.setFrameShape(QFrame.Shape.StyledPanel)
        self.contentFrame.setFrameShadow(QFrame.Shadow.Raised)

        # 몸통은 격자다 — 썸네일 열 · 글 열, 줄 셋. 썸네일 줄이기면 썸네일이 위 두 줄만 차지하고 3줄이 두
        # 열에 걸친다(#554). 모양은 만들 때 한 번 정한다 — 목록 전체가 같은 설정을 쓴다
        self.compactThumbnail = card_display.current().compact_thumbnail
        self.bodyLayout = QGridLayout(self.contentFrame)
        self.bodyLayout.setObjectName("bodyLayout")
        self.bodyLayout.setContentsMargins(pad, pad_y, pad, pad_y)
        self.bodyLayout.setHorizontalSpacing(theme.METRICS["cardThumbGap"])
        self.bodyLayout.setVerticalSpacing(gap)
        self.bodyLayout.setColumnStretch(1, 1)

        # 썸네일 — 110×62(줄이기면 68×38). 세로 그림(클립)은 상자 안에서 비율을 지키고 남는 자리를
        # 그림의 평균색으로 채운다(app/widgets/widget.py::_composeThumbnail)
        self.thumbnailLabel = CardThumbnail(self.contentFrame, self.compactThumbnail)
        self.thumbnailLabel.setObjectName("thumbnailLabel")
        self.thumbnailLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if self.compactThumbnail:
            self.thumbnailLabel.setFixedSize(
                theme.METRICS["cardThumbCompactWidth"], theme.METRICS["cardThumbCompactHeight"]
            )
        else:
            self.thumbnailLabel.setFixedSize(
                theme.METRICS["cardThumbWidth"], theme.METRICS["cardThumbHeight"]
            )
        thumb_rows = 2 if self.compactThumbnail else 3
        self.bodyLayout.addWidget(
            self.thumbnailLabel, 0, 0, thumb_rows, 1, Qt.AlignmentFlag.AlignTop
        )

        # ---- 1줄: 채널 그림 · 채널명 · 날짜 ----
        self.topRow, self.topLayout = _row(
            self.contentFrame, "topRow", theme.METRICS["cardRow1Height"]
        )
        self.topLayout.setSpacing(gap)
        self.channelImageLabel = ChannelAvatar(self.topRow)
        self.channelImageLabel.setObjectName("channelImageLabel")
        self.channelImageLabel.setFixedSize(QSize(status_icon, status_icon))
        self.channelImageLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.topLayout.addWidget(self.channelImageLabel)
        self.channelNameLabel = ElidingLabel(self.topRow, tip_when_elided=True)
        self.channelNameLabel.setObjectName("channelNameLabel")
        # 최소폭은 app/widgets/widget.py::_clampChannelMinWidth가 이름 길이에 맞춰 조인다. 최대폭은
        # 아주 긴 채널명이 날짜를 밀어내지 않게 한다
        self.channelNameLabel.setMinimumWidth(64)
        self.channelNameLabel.setMaximumWidth(150)
        self.topLayout.addWidget(self.channelNameLabel)
        self.topLayout.addSpacing(gap)
        self.dateLabel = QLabel(self.topRow)
        self.dateLabel.setObjectName("dateLabel")
        self.topLayout.addWidget(self.dateLabel)
        self.topLayout.addStretch(1)
        self.bodyLayout.addWidget(self.topRow, 0, 1)

        # ---- 2줄: 제목 — 라벨 폭은 글자 폭(그 너머는 누르는 자리가 아니다) ----
        self.titleRow, self.titleLayout = _row(
            self.contentFrame, "titleRow", theme.METRICS["cardRow2Height"]
        )
        # 제목은 잘렸을 때만 제목 전체를 툴팁으로 단다 — 잘린 채널명과 같은 방식(오너 2026-11-15, #610)
        self.titleLabel = ElidingLabel(self.titleRow, tip_when_elided=True)
        self.titleLabel.setObjectName("titleLabel")
        self.titleLabel.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.titleLabel.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self.titleLayout.addWidget(self.titleLabel)
        self.titleLayout.addStretch(1)
        self.bodyLayout.addWidget(self.titleRow, 1, 1)

        # ---- 3줄: 상태 원 아이콘 · 상태 글 ··· 저장 위치 · 정보 ----
        self.statusRow, self.resolutionLayout = _row(
            self.contentFrame, "statusRow", theme.METRICS["cardRow3Height"]
        )
        self.resolutionLayout.setObjectName("resolutionLayout")
        self.resolutionLayout.setSpacing(gap)
        self.statusIcon = StateBubble(self.statusRow)
        self.statusIcon.setObjectName("statusIcon")
        self.statusIcon.setFixedSize(QSize(status_icon, status_icon))
        # 아이콘이 없는 상태(대기 · 조회 중)는 상태 글이 그 자리에서 시작한다 — 프로토와 같다(#550)
        self.resolutionLayout.addWidget(self.statusIcon)
        self.statusLabel = ElidingLabel(self.statusRow, tip_when_elided=True)
        self.statusLabel.setObjectName("statusLabel")
        self.resolutionLayout.addWidget(self.statusLabel)
        self.resolutionLayout.addStretch(1)
        # 저장 위치 — 기본 위치와 다를 때만. 회색 폴더 윤곽 + 마지막 폴더 이름, 누를 수 없고 툴팁에
        # 전체 경로(설계 §12.1 카드 배치)
        # 아이콘은 도형을 그릴 때마다 테마 토큰(textMuted)을 읽는 IconButton이다(실행 중 테마 전환).
        # 누를 수 없다 — 호버 표면을 끄고(interactive=False) 초점도 받지 않는다
        self.pathIcon = IconButton(self.statusRow)
        self.pathIcon.setObjectName("pathIcon")
        self.pathIcon.setProperty("role", "icon")
        self.pathIcon.setInteractive(False)
        self.pathIcon.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.pathIcon.setFixedSize(QSize(status_icon, status_icon))
        self.pathIcon.setVisible(False)
        self.resolutionLayout.addWidget(self.pathIcon)
        self.directoryLabel = ElidingLabel(self.statusRow, elide_mode=Qt.TextElideMode.ElideMiddle)
        self.directoryLabel.setObjectName("directoryLabel")
        self.directoryLabel.setVisible(False)
        self.resolutionLayout.addWidget(self.directoryLabel)
        # 정보(해상도 · 크기 · 길이 · 구간) — 무엇을 적을지는 app/widgets/widget.py::_fitRow3가 접는
        # 단계로 정한다(#554)
        self.fileSizeLabel = ElidingLabel(self.statusRow, with_tip=False)
        self.fileSizeLabel.setObjectName("fileSizeLabel")
        self.fileSizeLabel.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.resolutionLayout.addWidget(self.fileSizeLabel)
        if self.compactThumbnail:
            self.bodyLayout.addWidget(self.statusRow, 2, 0, 1, 2)
        else:
            self.bodyLayout.addWidget(self.statusRow, 2, 1)

        # ---- 카드 버튼 — 1줄 오른쪽 끝에 겹쳐 놓는다(자리는 widget.py::_placeHover) ----
        # 덮개(hoverShade)는 버튼 밑의 1줄 글을 카드 바탕으로 덮는 그림이다 — 마우스를 받지 않아
        # 클릭은 밑으로 간다. 버튼 상자도 버튼 사이 빈자리는 마우스를 받지 않는다
        self.hoverShade = HoverShade(self.contentFrame)
        self.hoverShade.setObjectName("hoverShade")
        self.hoverShade.setVisible(False)
        self.hoverBox = QWidget(self.contentFrame)
        self.hoverBox.setObjectName("hoverBox")
        self.hoverBox.setVisible(False)
        self.hoverLayout = QHBoxLayout(self.hoverBox)
        self.hoverLayout.setContentsMargins(0, 0, 0, 0)
        self.hoverLayout.setSpacing(gap)

        def card_button(name: str) -> IconButton:
            b = IconButton(self.hoverBox)
            b.setObjectName(name)
            b.setFixedSize(QSize(button, button))
            b.setProperty("role", "icon")
            # 카드 안 버튼은 Tab 차례에서 뺀다 — Tab 한 번에 카드 하나(카드가 초점을 받고 Enter ·
            # 마우스가 버튼 일을 한다, #550)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setVisible(False)
            self.hoverLayout.addWidget(b)
            return b

        # 차례: 대기 ✎ ✂ ✕ · 받는 중 ‖ ✕ · 일시정지 ▶ ✕ · 완료 폴더 ✕ · 실패 다시 시도 ✕ · 조회 중 ✕
        # (✂ 구간 편집은 구간을 받을 수 있는 종류만 — #599)
        self.editButton = card_button("editButton")
        self.sectionButton = card_button("sectionButton")
        self.pauseButton = card_button("pauseButton")
        self.openDirectoryButton = card_button("openDirectoryButton")
        self.retryButton = card_button("retryButton")
        self.deleteButton = card_button("deleteButton")

        # ---- 하단 진행바 — 카드 아래 가장자리에 딱 붙는 전체 폭 오버레이 ----
        # 레이아웃 행으로 넣으면 보일 때만 카드가 자라 높이 불변 규칙이 깨진다(#245)
        self.progressBar = CardProgress(self.contentFrame)
        self.progressBar.setObjectName("progressBar")
        self.progressBar.setMinimumSize(QSize(0, bar_h))
        self.progressBar.setMaximumSize(QSize(16777215, bar_h))
        self.progressBar.setMaximum(100)
        self.progressBar.setValue(0)
        self.progressBar.setTextVisible(False)
        self.progressBar.setProperty("state", "waiting")
        self.progressBar.setVisible(False)

        self.contentItemLayout.addWidget(self.contentFrame)

        self.retranslateUi(ContentItemWidget)

        QMetaObject.connectSlotsByName(ContentItemWidget)

    # setupUi

    def retranslateUi(self, ContentItemWidget) -> None:
        ContentItemWidget.setWindowTitle(
            QCoreApplication.translate("ContentItemWidget", "ContentItemWidget", None)
        )
        self.editButton.setToolTip(QCoreApplication.translate("ContentItemWidget", "Edit", None))
        self.sectionButton.setToolTip(
            QCoreApplication.translate("ContentItemWidget", "Edit sections", None)
        )
        self.pauseButton.setToolTip(QCoreApplication.translate("ContentItemWidget", "Pause", None))
        self.openDirectoryButton.setToolTip(
            QCoreApplication.translate("ContentItemWidget", "Open directory", None)
        )
        self.retryButton.setToolTip(QCoreApplication.translate("ContentItemWidget", "Retry", None))
        self.deleteButton.setToolTip(
            QCoreApplication.translate("ContentItemWidget", "Delete", None)
        )

    # retranslateUi
