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
##   하단 progressBar: 카드 바닥 전체 폭 오버레이(받는 중 · 일시정지는 진행만큼, 완료 · 실패는 끝까지)
##   배경 채움(설정 키)이면 막대 대신 카드 면(CardFace)이 진행만큼 왼쪽부터 칠한다
## 줄 높이는 고정이다 — 상태가 바뀌어도 줄 자리 · 카드 높이가 같다(목록이 들썩이면 안 된다).
## 크기 · 간격은 theme.METRICS, 글자 · 색은 전역 QSS(theme 토큰)가 정한다.
################################################################################

from PySide6.QtCore import QCoreApplication, QMetaObject, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPaintEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

import app.theme as theme
from app.viewmodels import card_display
from app.widgets.eliding_label import ElidingLabel
from app.widgets.icons import IconButton
from app.widgets.menu_button import StateBubble


class CardFace(QFrame):
    """카드 면 — QSS가 바탕 · 테두리 · 색조를 그리고, 배경 채움 방식이면 그 위에 진행 채움을 그린다 (#554).

    채움은 진행만큼 왼쪽부터 카드 면 안쪽(테두리 안)을 칠한다 — 둥근 모서리로 잘라 낸다. 틱마다
    `setStyleSheet`를 쓰지 않고 `paintEvent`에서 그리며, 채움 끝이 움직이면 바뀐 띠만 다시 그린다
    (설계 §8의 6). 진행 끝선은 강조하지 않는다(설계 §12.1). 카드마다 붙는 효과 객체가 아니라 값 둘
    (채움 비율 · 색 토큰)만 든다.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._fill = 0.0  # 채운 비율 0 ~ 1
        self._fillToken = ""  # 채움 색 토큰 — 빈 글이면 채움 없음

    def fill(self) -> tuple[float, str]:
        """(채운 비율, 색 토큰)."""
        return self._fill, self._fillToken

    def setFill(self, fraction: float, token: str) -> None:
        """채움을 바꾼다 — 바뀐 띠만 다시 그린다. 토큰이 빈 글이면 채움 없음."""
        fraction = max(0.0, min(1.0, fraction)) if token else 0.0
        if (fraction, token) == (self._fill, self._fillToken):
            return
        if token != self._fillToken:
            self._fill, self._fillToken = fraction, token
            self.update()
            return
        left, right = sorted((self._fill, fraction))
        self._fill = fraction
        inner = self.width() - 2
        self.update(int(left * inner), 0, int((right - left) * inner) + 3, self.height())

    def fillRight(self) -> int:
        """채움의 오른쪽 끝(면 좌표 px). 채움이 없으면 0."""
        return round(1 + self._fill * (self.width() - 2)) if self._fillToken else 0

    def colorAt(self, x: int) -> QColor:
        """면 좌표 `x`에서 보이는 바탕색 — 채움 · 색조 · 호버를 따른다(카드 버튼 덮개가 쓴다)."""
        tokens = theme.current_tokens()
        if self._fillToken and x < self.fillRight():
            return QColor(tokens[self._fillToken])
        hovered = self.property("hover") is True
        tint = self.property("tint")
        if tint in ("finished", "failed"):
            name = "cardTintFinished" if tint == "finished" else "cardTintFailed"
            return QColor(tokens[name + ("Hover" if hovered else "")])
        return QColor(tokens["cardHoverBg" if hovered else "cardBg"])

    def paintEvent(self, event: QPaintEvent) -> None:
        """QSS 바탕 · 테두리를 그린 뒤 진행 채움을 테두리 안쪽에 그린다."""
        super().paintEvent(event)
        if not self._fillToken or self._fill <= 0:
            return
        radius = theme.METRICS["cardRadius"] - 1
        inner = QRectF(1, 1, self.width() - 2, self.height() - 2)
        path = QPainterPath()
        path.addRoundedRect(inner, radius, radius)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setClipPath(path)
        painter.fillRect(
            QRectF(1, 1, self._fill * inner.width(), inner.height()),
            QColor(theme.current_tokens()[self._fillToken]),
        )


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
    보이는 카드 면의 색이다(호버 · 완료 · 실패 색조 · 배경 채움, `CardFace.colorAt`) — 그릴 때 읽는다.
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
        self.bodyLayout.setContentsMargins(pad, pad, pad, pad)
        self.bodyLayout.setHorizontalSpacing(theme.METRICS["cardThumbGap"])
        self.bodyLayout.setVerticalSpacing(gap)
        self.bodyLayout.setColumnStretch(1, 1)

        # 썸네일 — 110×62(줄이기면 68×38). 세로 그림(클립)은 상자 안에서 비율을 지키고 남는 자리를
        # 그림의 평균색으로 채운다(app/widgets/widget.py::_composeThumbnail)
        self.thumbnailLabel = QLabel(self.contentFrame)
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
        self.fileSizeLabel = ElidingLabel(self.statusRow, tip_when_elided=True)
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

        # 차례: 대기 ✎ ✕ · 받는 중 ‖ ✕ · 일시정지 ▶ ✕ · 완료 폴더 ✕ · 실패 다시 시도 ✕ · 조회 중 ✕
        self.editButton = card_button("editButton")
        self.pauseButton = card_button("pauseButton")
        self.openDirectoryButton = card_button("openDirectoryButton")
        self.retryButton = card_button("retryButton")
        self.deleteButton = card_button("deleteButton")

        # ---- 하단 진행바 — 카드 아래 가장자리에 딱 붙는 전체 폭 오버레이 ----
        # 레이아웃 행으로 넣으면 보일 때만 카드가 자라 높이 불변 규칙이 깨진다(#245)
        self.progressBar = QProgressBar(self.contentFrame)
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
        self.pauseButton.setToolTip(QCoreApplication.translate("ContentItemWidget", "Pause", None))
        self.openDirectoryButton.setToolTip(
            QCoreApplication.translate("ContentItemWidget", "Open directory", None)
        )
        self.retryButton.setToolTip(QCoreApplication.translate("ContentItemWidget", "Retry", None))
        self.deleteButton.setToolTip(
            QCoreApplication.translate("ContentItemWidget", "Delete", None)
        )

    # retranslateUi
