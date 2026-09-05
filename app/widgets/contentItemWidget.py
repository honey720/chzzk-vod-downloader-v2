# -*- coding: utf-8 -*-

################################################################################
## contentItemWidget.ui의 수동 유지 구현 (#244·#245 — 상태별 슬롯 카드)
##
## ⚠️ 이 파일은 pyside6-uic 재생성 대상이 아니다 — 설치된 uic가 .ui의
## `<item stretch>` 문법을 거부하고, stretch·theme.METRICS 연동 같은 수동
## 튜닝이 재생성 시 사라진다. 구조 변경 시 .ui(설계 기록)와 이 파일을
## 함께 손으로 고칠 것.
##
## 구조(#245 오너 확정 — 상태별 슬롯): 썸네일(16:9 고정 상자) | 3행 컬럼
##   1행 topLayout:        채널이미지(원형)·채널명(굵게)
##                         [간격] 상태별 조작(일시정지/재개/폴더/재시도 중 하나) · [추가 간격] 삭제
##   2행 titleLayout:      제목(가장 크고 밝다, ElideRight, 클릭 편집)
##   3행 resolutionLayout: 상태별 슬롯 [간격] (경로: 전역과 다를 때만) · 확정 해상도 · 파일 크기
##     대기: 해상도 pill들 / 진행: %·속도·남은시간 / 일시정지: %·일시정지됨
##     / 완료: ✓ 완료 / 실패: ✕ 사유
##   하단 progressBar:     카드 바닥 전체 폭 — 진행분이 있을 때(진행·일시정지)
##
## 상태는 넷이 아니라 다섯이다(DownloadState.PAUSED 포함). 상태가 바뀌면
## 3행 내용·조작 버튼이 바뀌지만 행 높이·카드 높이는 불변이다
## (목록이 들썩이면 안 된다 — tests/unit/test_card_layout.py·
## tests/unit/test_card_state_matrix.py 게이트).
## 조작·삭제 아이콘은 폰트 글리프가 아니라 content/icons.py가 그리는 도형이다.
## 좌측 기준선은 둘: 썸네일 왼쪽(=cardPadding), 컨텐츠 열 왼쪽(3행 공통).
## 우측 끝은 하나: 삭제(1행)·파일 크기(3행) — 조작이 3개로 늘어도 유지
## (#178 구간 버튼 자리, 게이트로 고정). 크기·간격은 theme.METRICS, 글자
## 위계·색은 전역 QSS(theme.FONTS 토큰)가 정한다.
################################################################################

from PySide6.QtCore import (QCoreApplication, QMetaObject, QSize, Qt)
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel,
    QLineEdit, QProgressBar, QVBoxLayout)

import theme
from app.widgets.eliding_label import ElidingLabel, PathLabel
from app.widgets.icons import IconButton


class Ui_ContentItemWidget(object):
    def setupUi(self, ContentItemWidget):
        if not ContentItemWidget.objectName():
            ContentItemWidget.setObjectName(u"ContentItemWidget")
        ContentItemWidget.resize(600, 110)

        pad = theme.METRICS["cardPadding"]
        icon = theme.METRICS["iconSize"]
        row_gap = theme.METRICS["cardRowSpacing"]
        bar_h = theme.METRICS["barHeight"]
        pill_h = theme.METRICS["pillHeight"]

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

        # 썸네일 — 상자는 16:9 고정(카드마다 폭이 달라지면 컨텐츠 열
        # 기준선이 무너진다). 크기는 우측 컨텐츠 열의 실제 높이에 맞춰
        # ContentItemWidget이 런타임에 계산한다. 세로 이미지(클립)는 상자
        # 안에서 비율 유지 + letterbox를 이미지 평균색으로 채운다
        # (content/widget.py::_composeThumbnail).
        self.thumbnailLabel = QLabel(self.contentFrame)
        self.thumbnailLabel.setObjectName(u"thumbnailLabel")
        self.thumbnailLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.bodyLayout.addWidget(self.thumbnailLabel)

        self.contentLayout = QVBoxLayout()
        self.contentLayout.setObjectName(u"contentLayout")
        self.contentLayout.setSpacing(row_gap)

        # ---- 1행: 채널 식별 ··· 상태별 조작 · 삭제 ----
        # 남는 공간은 가운데 스트레치 하나만 흡수한다. 우측은 조작만 —
        # 상태 아이콘·텍스트는 3행 슬롯으로 갔다(#245). 조작 버튼 셋
        # (⏸/📁/↻)은 항상 존재하고 상태에 맞는 것만 보인다 — 버튼이 있고
        # 없고가 행 높이를 흔들지 않게 한다.
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
        # 최소폭은 content/widget.py::_clampChannelMinWidth가 이름 길이에
        # 맞춰 조인다(짧은 이름이 빈 폭을 예약하지 않게). 최대폭은 아주
        # 긴 채널명이 우측 조작을 밀어내는 것을 막는다.
        self.channelNameLabel.setMinimumWidth(64)
        self.channelNameLabel.setMaximumWidth(150)

        self.topLayout.addWidget(self.channelNameLabel)

        self.topLayout.addStretch(1)

        # 상태별 조작 — 진행: 일시정지 / 일시정지: 재개 / 완료: 폴더 열기 /
        # 실패: 재시도. 한 아이콘은 한 가지 일만 한다. 도형 이름은
        # content/widget.py가 상태에 맞춰 넣는다(pauseButton은 pause↔resume).
        self.pauseButton = IconButton(self.contentFrame)
        self.pauseButton.setObjectName(u"pauseButton")
        self.pauseButton.setMinimumSize(QSize(icon, icon))
        self.pauseButton.setMaximumSize(QSize(icon, icon))
        self.pauseButton.setProperty("role", u"icon")
        self.pauseButton.setVisible(False)

        self.topLayout.addWidget(self.pauseButton)

        self.openDirectoryButton = IconButton(self.contentFrame)
        self.openDirectoryButton.setObjectName(u"openDirectoryButton")
        self.openDirectoryButton.setMinimumSize(QSize(icon, icon))
        self.openDirectoryButton.setMaximumSize(QSize(icon, icon))
        self.openDirectoryButton.setProperty("role", u"icon")
        self.openDirectoryButton.setVisible(False)

        self.topLayout.addWidget(self.openDirectoryButton)

        self.retryButton = IconButton(self.contentFrame)
        self.retryButton.setObjectName(u"retryButton")
        self.retryButton.setMinimumSize(QSize(icon, icon))
        self.retryButton.setMaximumSize(QSize(icon, icon))
        self.retryButton.setProperty("role", u"icon")
        self.retryButton.setVisible(False)

        self.topLayout.addWidget(self.retryButton)

        # 삭제는 파괴적 조작 — 나머지 조작과 같은 무게로 붙어 있으면 실수로
        # 누른다. 고정 간격을 더 줘 떨어뜨린다(스트레치가 아니라 고정값 —
        # 남는 공간 흡수처는 행마다 한 곳뿐이어야 한다).
        self.topLayout.addSpacing(8)

        self.deleteButton = IconButton(self.contentFrame)
        self.deleteButton.setObjectName(u"deleteButton")
        self.deleteButton.setMinimumSize(QSize(icon, icon))
        self.deleteButton.setMaximumSize(QSize(icon, icon))
        self.deleteButton.setProperty("role", u"icon")

        self.topLayout.addWidget(self.deleteButton)

        self.contentLayout.addLayout(self.topLayout)

        # ---- 2행: 제목 (클릭하면 편집 — 호버 힌트는 전역 QSS) ----
        self.titleLayout = QHBoxLayout()
        self.titleLayout.setObjectName(u"titleLayout")
        self.titleLabel = ElidingLabel(self.contentFrame)
        self.titleLabel.setObjectName(u"titleLabel")
        self.titleLabel.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.titleLabel.setCursor(Qt.CursorShape.IBeamCursor)

        self.titleLayout.addWidget(self.titleLabel, 1)

        self.titleEdit = QLineEdit(self.contentFrame)
        self.titleEdit.setObjectName(u"titleEdit")
        self.titleEdit.setClearButtonEnabled(True)

        self.titleLayout.addWidget(self.titleEdit, 1)

        self.contentLayout.addLayout(self.titleLayout)

        # ---- 3행: 상태별 슬롯 ··· (경로) · 파일 크기 ----
        # 대기: 해상도 pill들(content/widget.py가 왼쪽부터 삽입).
        # 그 외: statusLabel 하나가 슬롯 텍스트(진행 %·속도·남은시간 /
        # ✓ 완료 / ✕ 사유)를 상태색으로 보여준다. 어느 쪽이 보이든 행
        # 높이가 같도록 statusLabel 최소 높이를 pill 높이에 맞춘다.
        # 경로는 전역 설정 경로와 다를 때만 보인다 — 같은 값을 카드마다
        # 반복하는 것이 정보 과다의 큰 몫이었다(#245).
        self.resolutionLayout = QHBoxLayout()
        self.resolutionLayout.setObjectName(u"resolutionLayout")
        self.resolutionLayout.setSpacing(4)

        self.statusLabel = ElidingLabel(self.contentFrame)
        self.statusLabel.setObjectName(u"statusLabel")
        self.statusLabel.setMinimumHeight(pill_h)
        self.statusLabel.setVisible(False)

        self.resolutionLayout.addWidget(self.statusLabel)

        self.resolutionLayout.addStretch(1)

        # 경로 — 표시는 축약형("~/…/폴더"), 전문은 툴팁. 클릭하면 폴더 선택
        # 대화상자(content/widget.py::choosePath)가 이 카드의 경로를 바꾼다 —
        # 인라인 QLineEdit 편집은 #245에서 폴더 선택으로 교체됐다(존재하는
        # 폴더만 고르므로 검증·거부 안내가 필요 없다). 대기에서는 항상 보이고
        # 그 외에는 전역 경로와 다를 때만 보인다.
        # 경로는 줄어드는 순서가 정해진 PathLabel(#245): 전체 → 중간 폴더 접기 →
        # 마지막 폴더만 ElideMiddle. 파일이 들어가는 마지막 폴더가 가장 늦게 잘린다.
        self.directoryLabel = PathLabel(self.contentFrame)
        self.directoryLabel.setObjectName(u"directoryLabel")
        self.directoryLabel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.directoryLabel.setVisible(False)

        self.resolutionLayout.addWidget(self.directoryLabel)

        # 경로 아이콘 — 3행 폭이 모자라 경로 텍스트가 최소치 아래로 내려가면
        # 라벨 대신 이 버튼만 남는다(#245). 텍스트가 사라져도 **클릭 대상(폴더
        # 선택 진입점)은 남아야 한다.** 전역 경로와 다르면 folder_dot(점 표시).
        self.pathIconButton = IconButton(self.contentFrame)
        self.pathIconButton.setObjectName(u"pathIconButton")
        self.pathIconButton.setMinimumSize(QSize(icon, icon))
        self.pathIconButton.setMaximumSize(QSize(icon, icon))
        self.pathIconButton.setProperty("role", u"icon")
        self.pathIconButton.setVisible(False)

        self.resolutionLayout.addWidget(self.pathIconButton)

        # 파일 크기·재생 시간 — **어떤 폭에서도 말줄임하지 않는다**(#245). 폭은
        # content/widget.py::_reserveFileSizeWidth가 "가장 긴 경우"(재생 시간
        # 또는 해상도 접두 + 크기)로 먼저 확보하고, 3행에서 줄어드는 것은 경로
        # 하나뿐이다. 오른쪽 정렬이라 확보 폭이 남아도 우측 끝선이 유지된다.
        self.fileSizeLabel = ElidingLabel(self.contentFrame)
        self.fileSizeLabel.setObjectName(u"fileSizeLabel")
        self.fileSizeLabel.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.resolutionLayout.addWidget(self.fileSizeLabel)

        self.contentLayout.addLayout(self.resolutionLayout)

        self.bodyLayout.addLayout(self.contentLayout)

        self.contentFrameLayout.addLayout(self.bodyLayout)

        # ---- 하단 진행바 — 카드 아래 가장자리에 딱 붙는 전체 폭 ----
        # 진행분이 있을 때(진행·일시정지)만 보인다(applyStateStyle) — 빈 막대는
        # 정보가 없다. 일시정지에서는 [state="paused"] muted 색이다.
        # ⚠️ 레이아웃 행이 아니라 **오버레이**다(부모만 지정, 지오메트리는
        # ContentItemWidget.resizeEvent→_placeProgressBar가 수동 배치) —
        # 레이아웃 행으로 넣으면 보일 때만 카드가 barHeight만큼 자라
        # "상태가 바뀌어도 카드 높이는 불변" 규칙(#245, 목록 들썩임 금지)이
        # 깨진다(실측 95→99px). 바닥 여백(cardPadding 8px)이 바(4px)보다
        # 커서 겹쳐도 글과 안 부딪힌다.
        self.progressBar = QProgressBar(self.contentFrame)
        self.progressBar.setObjectName(u"progressBar")
        self.progressBar.setMinimumSize(QSize(0, bar_h))
        self.progressBar.setMaximumSize(QSize(16777215, bar_h))
        self.progressBar.setMaximum(100)
        self.progressBar.setValue(0)
        self.progressBar.setTextVisible(False)
        self.progressBar.setProperty("state", u"waiting")
        self.progressBar.setVisible(False)

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
        self.pauseButton.setToolTip(QCoreApplication.translate("ContentItemWidget", u"Pause", None))
#endif // QT_CONFIG(tooltip)
#if QT_CONFIG(tooltip)
        self.openDirectoryButton.setToolTip(QCoreApplication.translate("ContentItemWidget", u"Open directory", None))
#endif // QT_CONFIG(tooltip)
#if QT_CONFIG(tooltip)
        self.retryButton.setToolTip(QCoreApplication.translate("ContentItemWidget", u"Retry", None))
#endif // QT_CONFIG(tooltip)
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
        self.statusLabel.setToolTip(QCoreApplication.translate("ContentItemWidget", u"Status", None))
#endif // QT_CONFIG(tooltip)
        self.statusLabel.setText(QCoreApplication.translate("ContentItemWidget", u"Status", None))
#if QT_CONFIG(tooltip)
        self.directoryLabel.setToolTip(QCoreApplication.translate("ContentItemWidget", u"Directory", None))
#endif // QT_CONFIG(tooltip)
        self.directoryLabel.setText(QCoreApplication.translate("ContentItemWidget", u"Directory", None))
#if QT_CONFIG(tooltip)
        self.fileSizeLabel.setToolTip(QCoreApplication.translate("ContentItemWidget", u"File size", None))
#endif // QT_CONFIG(tooltip)
        self.fileSizeLabel.setText(QCoreApplication.translate("ContentItemWidget", u"File size", None))
    # retranslateUi
