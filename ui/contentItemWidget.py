# -*- coding: utf-8 -*-

################################################################################
## contentItemWidget.ui의 수동 유지 구현 (#244 카드 재설계)
##
## ⚠️ 이 파일은 pyside6-uic 재생성 대상이 아니다 — 설치된 uic가 .ui의
## `<item stretch>` 문법을 거부하고, stretch·theme.METRICS 연동 같은 수동
## 튜닝이 재생성 시 사라진다. 구조 변경 시 .ui(설계 기록)와 이 파일을
## 함께 손으로 고칠 것.
##
## 구조(#244 오너 확정 설계): 썸네일(16:9, 카드 안쪽 높이를 가득) | 4행 컬럼
##   1행 topLayout:        채널이미지·채널명(굵게)·종류(muted) [간격] 상태아이콘·상태텍스트·진행률·삭제
##   2행 titleLayout:      제목(가장 크고 밝다) — 한 줄 ElideRight
##   3행 resolutionLayout: 해상도 pill들 [간격] 파일 크기
##   4행 directoryLayout:  폴더 열기 + 저장 경로(ElideMiddle, 인라인 편집)
##   하단 progressBar:     카드 아래 가장자리에 딱 붙는 전체 폭 — 진행 중일 때만 보임
##
## 좌측 기준선은 둘뿐이다: 썸네일 왼쪽(=cardPadding)과 컨텐츠 열 왼쪽.
## 4행 전부 컨텐츠 열의 같은 x에서 시작한다(tests/unit/test_card_layout.py가
## 폭 3종으로 게이트). 크기·간격은 전부 theme.METRICS에서 온다 — 오너가
## theme.py 숫자만 바꾸면 카드가 따라온다. 글자 크기·색은 전역 QSS(위계
## 토큰 theme.FONTS)가 입힌다 — 위젯별 인라인 styleSheet은 전부 걷어냈다.
################################################################################

from PySide6.QtCore import (QCoreApplication, QMetaObject, QSize, Qt)
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel,
    QLineEdit, QProgressBar, QPushButton, QVBoxLayout)

import theme
from content.eliding_label import ElidingLabel


class Ui_ContentItemWidget(object):
    def setupUi(self, ContentItemWidget):
        if not ContentItemWidget.objectName():
            ContentItemWidget.setObjectName(u"ContentItemWidget")
        ContentItemWidget.resize(600, 120)

        pad = theme.METRICS["cardPadding"]
        icon = theme.METRICS["iconSize"]
        row_gap = theme.METRICS["cardRowSpacing"]
        bar_h = theme.METRICS["barHeight"]

        self.contentItemLayout = QVBoxLayout(ContentItemWidget)
        self.contentItemLayout.setObjectName(u"contentItemLayout")
        # 좌우 0 — 카드 프레임의 좌우 위치는 목록 컨테이너·창 여백
        # (theme.METRICS["outerMargin"])이 정해, 상단·하단 바와 같은
        # 정렬선을 쓴다. 위 6은 카드 사이 간격의 일부다.
        self.contentItemLayout.setContentsMargins(0, 6, 0, 0)
        self.contentFrame = QFrame(ContentItemWidget)
        self.contentFrame.setObjectName(u"contentFrame")
        self.contentFrame.setFrameShape(QFrame.Shape.StyledPanel)
        self.contentFrame.setFrameShadow(QFrame.Shadow.Raised)

        # 프레임 직속 레이아웃은 여백 0 — 안쪽 여백은 bodyLayout이 준다.
        # 하단 진행바가 카드 가장자리에 딱 붙어야 해서다.
        self.contentFrameLayout = QVBoxLayout(self.contentFrame)
        self.contentFrameLayout.setObjectName(u"contentFrameLayout")
        self.contentFrameLayout.setContentsMargins(0, 0, 0, 0)
        self.contentFrameLayout.setSpacing(0)

        # ---- 본체: 썸네일 | 컨텐츠 열 ----
        self.bodyLayout = QHBoxLayout()
        self.bodyLayout.setObjectName(u"bodyLayout")
        self.bodyLayout.setContentsMargins(pad, pad, pad, pad)
        self.bodyLayout.setSpacing(pad)

        # 썸네일 — 크기는 여기서 정하지 않는다. 우측 컨텐츠 열의 실제
        # 높이에 맞춰 ContentItemWidget이 런타임에 16:9로 계산한다
        # ("원하는 카드 높이에서 16:9로 폭이 나온다" — #244 확정 설계).
        self.thumbnailLabel = QLabel(self.contentFrame)
        self.thumbnailLabel.setObjectName(u"thumbnailLabel")
        self.thumbnailLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.bodyLayout.addWidget(self.thumbnailLabel)

        self.contentLayout = QVBoxLayout()
        self.contentLayout.setObjectName(u"contentLayout")
        self.contentLayout.setSpacing(row_gap)

        # ---- 1행: 채널 식별 ··· 상태 · 삭제 ----
        # 남는 공간은 가운데 스트레치 하나만 흡수한다 — 좌우 군집 내부
        # 간격은 setSpacing 고정값(4px)이라 창이 넓어져도 안 벌어진다.
        self.topLayout = QHBoxLayout()
        self.topLayout.setObjectName(u"topLayout")
        self.topLayout.setSpacing(4)

        self.channelImageLabel = QLabel(self.contentFrame)
        self.channelImageLabel.setObjectName(u"channelImageLabel")
        self.channelImageLabel.setMinimumSize(QSize(icon, icon))
        self.channelImageLabel.setMaximumSize(QSize(icon, icon))
        self.channelImageLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.topLayout.addWidget(self.channelImageLabel)

        self.channelNameLabel = ElidingLabel(self.contentFrame)
        self.channelNameLabel.setObjectName(u"channelNameLabel")
        # 채널명은 길이가 임의라 폭이 빠듯할 때 가장 먼저 줄어야 한다(PR #229
        # 후속 오너 지시). 최소폭은 좁은 창에서 "..." 하나로 붕괴하는 것을
        # 막고(#239 흡수), 최대폭은 아주 긴 채널명이 상태 표시를 밀어내는
        # 것을 막는다.
        self.channelNameLabel.setMinimumWidth(64)
        self.channelNameLabel.setMaximumWidth(150)

        self.topLayout.addWidget(self.channelNameLabel)

        # 컨텐츠 종류 — 채널명 옆 가운뎃점 구분("LCK · video"), muted.
        # 표시 텍스트("· video")는 content/widget.py::setupDynamicUi가 채운다.
        self.contentTypeLabel = QLabel(self.contentFrame)
        self.contentTypeLabel.setObjectName(u"contentTypeLabel")

        self.topLayout.addWidget(self.contentTypeLabel)

        self.topLayout.addStretch(1)

        # 상태 아이콘+텍스트 묶음 — 색은 전역 QSS의 [state="..."] 규칙이
        # 상태별로 입힌다(content/widget.py::applyStateStyle이 속성을 건다).
        self.stateIconLabel = QLabel(self.contentFrame)
        self.stateIconLabel.setObjectName(u"stateIconLabel")
        self.stateIconLabel.setMinimumSize(QSize(16, icon))
        self.stateIconLabel.setMaximumSize(QSize(16, icon))
        self.stateIconLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.topLayout.addWidget(self.stateIconLabel)

        self.statusLabel = ElidingLabel(self.contentFrame)
        self.statusLabel.setObjectName(u"statusLabel")

        self.topLayout.addWidget(self.statusLabel)

        self.progressLabel = QLabel(self.contentFrame)
        self.progressLabel.setObjectName(u"progressLabel")

        self.topLayout.addWidget(self.progressLabel)

        self.deleteButton = QPushButton(self.contentFrame)
        self.deleteButton.setObjectName(u"deleteButton")
        self.deleteButton.setMinimumSize(QSize(icon, icon))
        self.deleteButton.setMaximumSize(QSize(icon, icon))
        self.deleteButton.setProperty("role", u"icon")

        self.topLayout.addWidget(self.deleteButton)

        self.contentLayout.addLayout(self.topLayout)

        # ---- 2행: 제목 ----
        self.titleLayout = QHBoxLayout()
        self.titleLayout.setObjectName(u"titleLayout")
        self.titleLabel = ElidingLabel(self.contentFrame)
        self.titleLabel.setObjectName(u"titleLabel")
        self.titleLabel.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.titleLayout.addWidget(self.titleLabel, 1)

        self.titleEdit = QLineEdit(self.contentFrame)
        self.titleEdit.setObjectName(u"titleEdit")
        self.titleEdit.setClearButtonEnabled(True)

        self.titleLayout.addWidget(self.titleEdit, 1)

        self.contentLayout.addLayout(self.titleLayout)

        # ---- 3행: 해상도 ··· 파일 크기 ----
        # 버튼은 content/widget.py가 왼쪽부터 순서대로 꽂고(스트레치 앞),
        # 남는 공간은 가운데 스트레치 하나가 흡수, 파일 크기는 우측 끝
        # (삭제 버튼과 같은 오른쪽 기준선)에 붙는다.
        self.resolutionLayout = QHBoxLayout()
        self.resolutionLayout.setObjectName(u"resolutionLayout")
        self.resolutionLayout.setSpacing(4)
        self.resolutionLayout.addStretch(1)

        self.fileSizeLabel = ElidingLabel(self.contentFrame)
        self.fileSizeLabel.setObjectName(u"fileSizeLabel")

        self.resolutionLayout.addWidget(self.fileSizeLabel)

        self.contentLayout.addLayout(self.resolutionLayout)

        # ---- 4행: 폴더 열기 + 저장 경로 ----
        # 폴더 버튼이 경로 왼쪽에 붙어 한 덩어리다(#244 — 우측 끝에 혼자
        # 떠 있던 것을 경로와 묶음). 남는 공간은 경로 라벨이 흡수한다.
        self.directoryLayout = QHBoxLayout()
        self.directoryLayout.setObjectName(u"directoryLayout")
        self.directoryLayout.setSpacing(4)

        self.openDirectoryButton = QPushButton(self.contentFrame)
        self.openDirectoryButton.setObjectName(u"openDirectoryButton")
        self.openDirectoryButton.setMinimumSize(QSize(icon, icon))
        self.openDirectoryButton.setMaximumSize(QSize(icon, icon))
        self.openDirectoryButton.setProperty("role", u"icon")

        self.directoryLayout.addWidget(self.openDirectoryButton)

        self.directoryLabel = ElidingLabel(self.contentFrame, elide_mode=Qt.TextElideMode.ElideMiddle)
        self.directoryLabel.setObjectName(u"directoryLabel")

        self.directoryLayout.addWidget(self.directoryLabel, 1)

        self.directoryEdit = QLineEdit(self.contentFrame)
        self.directoryEdit.setObjectName(u"directoryEdit")
        self.directoryEdit.setClearButtonEnabled(True)

        self.directoryLayout.addWidget(self.directoryEdit, 1)

        self.contentLayout.addLayout(self.directoryLayout)

        self.bodyLayout.addLayout(self.contentLayout)

        self.contentFrameLayout.addLayout(self.bodyLayout)

        # ---- 하단 진행바 — 카드 아래 가장자리에 딱 붙는 전체 폭 ----
        # 진행 중일 때만 보인다(applyStateStyle) — 빈 막대는 정보가 없다.
        # 좌우·아래 1px은 카드 테두리 안쪽에 들어오게 하는 최소 여백이다.
        self.barLayout = QHBoxLayout()
        self.barLayout.setObjectName(u"barLayout")
        self.barLayout.setContentsMargins(1, 0, 1, 1)
        self.barLayout.setSpacing(0)

        self.progressBar = QProgressBar(self.contentFrame)
        self.progressBar.setObjectName(u"progressBar")
        self.progressBar.setMinimumSize(QSize(0, bar_h))
        self.progressBar.setMaximumSize(QSize(16777215, bar_h))
        self.progressBar.setMaximum(100)
        self.progressBar.setValue(0)
        self.progressBar.setTextVisible(False)
        self.progressBar.setProperty("state", u"waiting")
        self.progressBar.setVisible(False)

        self.barLayout.addWidget(self.progressBar)

        self.contentFrameLayout.addLayout(self.barLayout)

        self.contentItemLayout.addWidget(self.contentFrame)

        self.retranslateUi(ContentItemWidget)

        QMetaObject.connectSlotsByName(ContentItemWidget)
    # setupUi

    def retranslateUi(self, ContentItemWidget):
        ContentItemWidget.setWindowTitle(QCoreApplication.translate("ContentItemWidget", u"ContentItemWidget", None))
#if QT_CONFIG(tooltip)
        self.channelImageLabel.setToolTip(QCoreApplication.translate("ContentItemWidget", u"Channel image", None))
#endif // QT_CONFIG(tooltip)
        self.channelImageLabel.setText(QCoreApplication.translate("ContentItemWidget", u"Channel image", None))
#if QT_CONFIG(tooltip)
        self.channelNameLabel.setToolTip(QCoreApplication.translate("ContentItemWidget", u"Channel name", None))
#endif // QT_CONFIG(tooltip)
        self.channelNameLabel.setText(QCoreApplication.translate("ContentItemWidget", u"Channel name", None))
#if QT_CONFIG(tooltip)
        self.contentTypeLabel.setToolTip(QCoreApplication.translate("ContentItemWidget", u"Content type", None))
#endif // QT_CONFIG(tooltip)
        self.contentTypeLabel.setText(QCoreApplication.translate("ContentItemWidget", u"Content type", None))
#if QT_CONFIG(tooltip)
        self.stateIconLabel.setToolTip(QCoreApplication.translate("ContentItemWidget", u"Status", None))
#endif // QT_CONFIG(tooltip)
#if QT_CONFIG(tooltip)
        self.statusLabel.setToolTip(QCoreApplication.translate("ContentItemWidget", u"Status", None))
#endif // QT_CONFIG(tooltip)
        self.statusLabel.setText(QCoreApplication.translate("ContentItemWidget", u"Status", None))
#if QT_CONFIG(tooltip)
        self.progressLabel.setToolTip(QCoreApplication.translate("ContentItemWidget", u"Progress", None))
#endif // QT_CONFIG(tooltip)
        self.progressLabel.setText(QCoreApplication.translate("ContentItemWidget", u"Progress", None))
#if QT_CONFIG(tooltip)
        self.deleteButton.setToolTip(QCoreApplication.translate("ContentItemWidget", u"Delete", None))
#endif // QT_CONFIG(tooltip)
#if QT_CONFIG(tooltip)
        self.thumbnailLabel.setToolTip(QCoreApplication.translate("ContentItemWidget", u"Thumbnail", None))
#endif // QT_CONFIG(tooltip)
        self.thumbnailLabel.setText(QCoreApplication.translate("ContentItemWidget", u"Thumbnail", None))
#if QT_CONFIG(tooltip)
        self.titleLabel.setToolTip(QCoreApplication.translate("ContentItemWidget", u"Title", None))
#endif // QT_CONFIG(tooltip)
        self.titleLabel.setText(QCoreApplication.translate("ContentItemWidget", u"Title", None))
#if QT_CONFIG(tooltip)
        self.fileSizeLabel.setToolTip(QCoreApplication.translate("ContentItemWidget", u"File size", None))
#endif // QT_CONFIG(tooltip)
        self.fileSizeLabel.setText(QCoreApplication.translate("ContentItemWidget", u"File size", None))
#if QT_CONFIG(tooltip)
        self.directoryLabel.setToolTip(QCoreApplication.translate("ContentItemWidget", u"Directory", None))
#endif // QT_CONFIG(tooltip)
        self.directoryLabel.setText(QCoreApplication.translate("ContentItemWidget", u"Directory", None))
#if QT_CONFIG(tooltip)
        self.openDirectoryButton.setToolTip(QCoreApplication.translate("ContentItemWidget", u"Open directory", None))
#endif // QT_CONFIG(tooltip)
    # retranslateUi
