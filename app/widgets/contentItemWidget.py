# -*- coding: utf-8 -*-

################################################################################
## 목록 카드의 위젯 · 레이아웃 (#244 · #245 · #550 — 설계 §12.1 카드 배치)
##
## 손으로 유지하는 코드다(설계 기록 .ui는 #541에서 지웠다). 번역 컨텍스트
## `ContentItemWidget`의 글은 lupdate가 이 파일과 app/viewmodels/card_text.py에서 읽는다.
##
## 구조: 썸네일(110×62 고정) | 12 | 세 줄(16 · 4 · 18 · 4 · 20) — 카드 높이 80(테두리 포함)
##   1줄: 채널 그림(16, 원형) · 채널명(12 회색) · 날짜(12 회색) ··· (카드 버튼 자리)
##   2줄: 제목(14 굵게) — 라벨 폭은 글자 폭, 잘렸을 때만 글자 위 툴팁
##   3줄: 상태 원 아이콘 · 상태 글 ··· 저장 위치(폴더 윤곽 + 마지막 폴더, 기본 위치와 다를 때만)
##        · 정보(해상도 · 크기 · 길이 · 구간)
##   카드 버튼: 1줄 오른쪽 끝(위끝 = 썸네일 위끝)에 겹쳐 놓는 상자. 평소 숨고, 호버 · 카드 안
##        키보드 초점 때 그 상태의 버튼을 보인다. 그 밑은 1줄 높이 덮개가 카드 바탕으로 덮는다
##        (그림만 — 클릭은 밑으로)
##   하단 progressBar: 카드 바닥 전체 폭 오버레이(진행 · 일시정지)
## 줄 높이는 고정이다 — 상태가 바뀌어도 줄 자리 · 카드 높이가 같다(목록이 들썩이면 안 된다).
## 크기 · 간격은 theme.METRICS, 글자 · 색은 전역 QSS(theme 토큰)가 정한다.
################################################################################

from PySide6.QtCore import QCoreApplication, QMetaObject, QSize, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPaintEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

import app.theme as theme
from app.widgets.eliding_label import ElidingLabel
from app.widgets.icons import IconButton
from app.widgets.menu_button import StateBubble


class HoverShade(QWidget):
    """카드 버튼 밑의 1줄 글을 카드 바탕으로 덮는 오버레이 — 그림만, 마우스는 밑으로 간다 (#550).

    왼쪽 ``FADE``는 투명 → 바탕색으로 흐려지고, 그 오른쪽은 바탕색으로 채운다. 바탕색은 카드가
    호버면 ``cardHoverBg``, 아니면 ``cardBg``다 — 부모 카드 면(``contentFrame``)의 ``hover`` 속성을
    그릴 때 읽는다. 카드마다 붙는 효과 객체(QGraphicsEffect)가 아니라 그리기만 하는 위젯이다.
    """

    FADE = 24  # 흐려지는 폭(px) — 버튼 앞 글이 뚝 끊겨 보이지 않게
    SOLID = 8  # 버튼 상자 왼쪽 앞의 채우는 폭(px) — 버튼 간격과 같다

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, event: QPaintEvent) -> None:
        """지금 테마 · 호버의 카드 바탕색으로 흐림과 채움을 그린다."""
        frame = self.parentWidget()
        hovered = frame is not None and frame.property("hover") is True
        color = QColor(theme.current_tokens()["cardHoverBg" if hovered else "cardBg"])
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
        self.contentFrame = QFrame(ContentItemWidget)
        self.contentFrame.setObjectName("contentFrame")
        self.contentFrame.setFrameShape(QFrame.Shape.StyledPanel)
        self.contentFrame.setFrameShadow(QFrame.Shadow.Raised)

        self.bodyLayout = QHBoxLayout(self.contentFrame)
        self.bodyLayout.setObjectName("bodyLayout")
        self.bodyLayout.setContentsMargins(pad, pad, pad, pad)
        self.bodyLayout.setSpacing(theme.METRICS["cardThumbGap"])

        # 썸네일 — 110×62 고정. 세로 그림(클립)은 상자 안에서 비율을 지키고 남는 자리를 그림의
        # 평균색으로 채운다(app/widgets/widget.py::_composeThumbnail)
        self.thumbnailLabel = QLabel(self.contentFrame)
        self.thumbnailLabel.setObjectName("thumbnailLabel")
        self.thumbnailLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumbnailLabel.setFixedSize(
            theme.METRICS["cardThumbWidth"], theme.METRICS["cardThumbHeight"]
        )
        self.bodyLayout.addWidget(self.thumbnailLabel, 0, Qt.AlignmentFlag.AlignTop)

        self.contentLayout = QVBoxLayout()
        self.contentLayout.setObjectName("contentLayout")
        self.contentLayout.setSpacing(gap)

        # ---- 1줄: 채널 그림 · 채널명 · 날짜 ----
        self.topRow, self.topLayout = _row(
            self.contentFrame, "topRow", theme.METRICS["cardRow1Height"]
        )
        self.topLayout.setSpacing(gap)
        self.channelImageLabel = QLabel(self.topRow)
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
        self.contentLayout.addWidget(self.topRow)

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
        self.contentLayout.addWidget(self.titleRow)

        # ---- 3줄: 상태 원 아이콘 · 상태 글 ··· 저장 위치 · 정보 ----
        self.statusRow, self.resolutionLayout = _row(
            self.contentFrame, "statusRow", theme.METRICS["cardRow3Height"]
        )
        self.resolutionLayout.setObjectName("resolutionLayout")
        self.resolutionLayout.setSpacing(gap)
        self.statusIcon = StateBubble(self.statusRow)
        self.statusIcon.setObjectName("statusIcon")
        self.statusIcon.setFixedSize(QSize(status_icon, status_icon))
        # 아이콘이 없는 상태(대기 · 조회 중)에도 자리를 지킨다 — 상태 글의 시작 x가 어느 상태에서나 같다
        icon_policy = self.statusIcon.sizePolicy()
        icon_policy.setRetainSizeWhenHidden(True)
        self.statusIcon.setSizePolicy(icon_policy)
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
        # 정보(해상도 · 크기 · 길이 · 구간) — 폭은 app/widgets/widget.py::_fitInfo가 먼저 확보한다
        # (말줄임하지 않는다 — 3줄 접힘 차례는 C3)
        self.fileSizeLabel = ElidingLabel(self.statusRow, tip_when_elided=True)
        self.fileSizeLabel.setObjectName("fileSizeLabel")
        self.fileSizeLabel.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.resolutionLayout.addWidget(self.fileSizeLabel)
        self.contentLayout.addWidget(self.statusRow)

        self.bodyLayout.addLayout(self.contentLayout, 1)

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
