# -*- coding: utf-8 -*-

################################################################################
## Form generated from reading UI file 'contentItemWidget.ui'
##
## Created by: Qt User Interface Compiler version 6.8.1
##
## WARNING! All changes made in this file will be lost when recompiling UI file!
################################################################################

from PySide6.QtCore import (QCoreApplication, QDate, QDateTime, QLocale,
    QMetaObject, QObject, QPoint, QRect,
    QSize, QTime, QUrl, Qt)
from PySide6.QtGui import (QBrush, QColor, QConicalGradient, QCursor,
    QFont, QFontDatabase, QGradient, QIcon,
    QImage, QKeySequence, QLinearGradient, QPainter,
    QPalette, QPixmap, QRadialGradient, QTransform)
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QSizePolicy, QSpacerItem,
    QProgressBar, QVBoxLayout, QWidget)
from content.eliding_label import ElidingLabel

class Ui_ContentItemWidget(object):
    def setupUi(self, ContentItemWidget):
        if not ContentItemWidget.objectName():
            ContentItemWidget.setObjectName(u"ContentItemWidget")
        ContentItemWidget.resize(600, 130)
        self.contentItemLayout = QVBoxLayout(ContentItemWidget)
        self.contentItemLayout.setObjectName(u"contentItemLayout")
        self.contentItemLayout.setContentsMargins(9, 6, 9, 0)
        self.contentFrame = QFrame(ContentItemWidget)
        self.contentFrame.setObjectName(u"contentFrame")
        self.contentFrame.setFrameShape(QFrame.Shape.StyledPanel)
        self.contentFrame.setFrameShadow(QFrame.Shadow.Raised)
        self.contentFrameLayout = QVBoxLayout(self.contentFrame)
        self.contentFrameLayout.setObjectName(u"contentFrameLayout")
        # 카드 압축(#244) — 프레임 자체의 레이아웃 여백은 0으로 두고 QSS의
        # #contentFrame padding(theme.cardPaddingV)만 안쪽 여백으로 쓴다.
        # 기본 스타일 여백(Fusion 기준 대략 9px)이 QSS padding 위에 또
        # 얹히던 게 156px 실측 높이의 상당 부분을 차지했다(실측: 이 값을
        # 0으로 두기 전/후 카드 실제 높이 156px→130px).
        self.contentFrameLayout.setContentsMargins(0, 0, 0, 0)
        self.contentFrameLayout.setSpacing(4)
        self.topLayout = QHBoxLayout()
        self.topLayout.setObjectName(u"topLayout")
        self.indexLabel = QLabel(self.contentFrame)
        self.indexLabel.setObjectName(u"indexLabel")
        self.indexLabel.setMinimumSize(QSize(26, 26))
        self.indexLabel.setMaximumSize(QSize(26, 26))
        self.indexLabel.setStyleSheet(u"font-size: 14px;")

        self.topLayout.addWidget(self.indexLabel)

        self.contentTypeLabel = QLabel(self.contentFrame)
        self.contentTypeLabel.setObjectName(u"contentTypeLabel")
        self.contentTypeLabel.setStyleSheet(u"font-size: 14px;")

        self.topLayout.addWidget(self.contentTypeLabel)

        self.channelImageLabel = QLabel(self.contentFrame)
        self.channelImageLabel.setObjectName(u"channelImageLabel")
        self.channelImageLabel.setMinimumSize(QSize(26, 26))
        self.channelImageLabel.setMaximumSize(QSize(26, 26))
        self.channelImageLabel.setStyleSheet(u"font-size: 14px;")
        self.channelImageLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.topLayout.addWidget(self.channelImageLabel)

        self.channelNameLabel = ElidingLabel(self.contentFrame)
        self.channelNameLabel.setObjectName(u"channelNameLabel")
        self.channelNameLabel.setStyleSheet(u"font-size: 14px;")
        # 채널명은 길이가 임의라 폭이 빠듯할 때 가장 먼저 줄어야 한다(PR #229
        # 후속 — 오너 지시: "파일 크기는 안 줄고 채널명이 줄어야"). stretch를
        # 높여 압박받을 때 이 라벨이 우선 줄게 하되, 그대로 두면 여유가
        # 있을 때도 stretch가 커서 스페이서 몫까지 욕심내 폭이 과하게
        # 넓어진다 — setMaximumWidth로 그 욕심에 상한을 둔다.
        # 최소폭(#244, #239 흡수) — 좁은 창에서 압박이 심하면 ElidingLabel이
        # "..." 하나만 남기고 채널명을 통째로 지워버렸다(실측 확인). 최소 폭을
        # 예약해 최소한 몇 글자는 항상 보이게 한다.
        self.channelNameLabel.setMinimumWidth(64)
        self.channelNameLabel.setMaximumWidth(150)

        self.topLayout.addWidget(self.channelNameLabel, 100)

        self.topSpacer = QSpacerItem(40, 20, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        self.topLayout.addItem(self.topSpacer)

        self.statusLabel = ElidingLabel(self.contentFrame)
        self.statusLabel.setObjectName(u"statusLabel")
        self.statusLabel.setStyleSheet(u"font-size: 14px;")

        self.topLayout.addWidget(self.statusLabel)

        self.progressLabel = QLabel(self.contentFrame)
        self.progressLabel.setObjectName(u"progressLabel")
        self.progressLabel.setStyleSheet(u"font-size: 14px;")

        self.topLayout.addWidget(self.progressLabel)

        self.fileSizeLabel = ElidingLabel(self.contentFrame)
        self.fileSizeLabel.setObjectName(u"fileSizeLabel")
        self.fileSizeLabel.setStyleSheet(u"font-size: 14px;")

        self.topLayout.addWidget(self.fileSizeLabel)

        # 상태 아이콘(#244) — 카드 테두리색이 하던 상태 신호 역할을 이 아이콘
        # 하나로 옮긴다("테두리색·진행바색·텍스트" 3중 반복을 "아이콘·진행바색"
        # 2가지로 줄이는 오너 확정 결정). 글리프·색은 content/widget.py의
        # STATE_ICON과 전역 QSS `#stateIconLabel[state="..."]`이 정한다.
        self.stateIconLabel = QLabel(self.contentFrame)
        self.stateIconLabel.setObjectName(u"stateIconLabel")
        self.stateIconLabel.setMinimumSize(QSize(20, 20))
        self.stateIconLabel.setMaximumSize(QSize(20, 20))
        self.stateIconLabel.setStyleSheet(u"font-size: 14px;")
        self.stateIconLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.topLayout.addWidget(self.stateIconLabel)

        self.deleteButton = QPushButton(self.contentFrame)
        self.deleteButton.setObjectName(u"deleteButton")
        self.deleteButton.setMinimumSize(QSize(26, 26))
        self.deleteButton.setMaximumSize(QSize(26, 26))
        self.deleteButton.setProperty("role", u"icon")

        self.topLayout.addWidget(self.deleteButton)


        self.contentFrameLayout.addLayout(self.topLayout)

        self.centerLayout = QHBoxLayout()
        self.centerLayout.setObjectName(u"centerLayout")
        self.thumbnailLabel = QLabel(self.contentFrame)
        self.thumbnailLabel.setObjectName(u"thumbnailLabel")
        self.thumbnailLabel.setMinimumSize(QSize(64, 64))
        self.thumbnailLabel.setMaximumSize(QSize(64, 64))
        self.thumbnailLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.centerLayout.addWidget(self.thumbnailLabel)

        self.contentLayout = QVBoxLayout()
        self.contentLayout.setSpacing(2)
        self.contentLayout.setObjectName(u"contentLayout")
        self.titleLayout = QHBoxLayout()
        self.titleLayout.setObjectName(u"titleLayout")
        self.titleLabel = ElidingLabel(self.contentFrame)
        self.titleLabel.setObjectName(u"titleLabel")
        self.titleLabel.setStyleSheet(u"font-size: 14px;")

        self.titleLayout.addWidget(self.titleLabel)

        self.titleEdit = QLineEdit(self.contentFrame)
        self.titleEdit.setObjectName(u"titleEdit")
        self.titleEdit.setStyleSheet(u"font-size: 14px;")
        self.titleEdit.setClearButtonEnabled(True)

        self.titleLayout.addWidget(self.titleEdit)


        self.contentLayout.addLayout(self.titleLayout)

        # 해상도 버튼 전용 줄(#244) — 이전엔 titleLayout에 얹혀 제목과 한 줄을
        # 공유해 "제목과 무관한 정보가 같은 줄에 섞인다"는 문제였다
        # (content/widget.py::addRepresentationButton이 여기 채운다).
        self.resolutionLayout = QHBoxLayout()
        self.resolutionLayout.setObjectName(u"resolutionLayout")
        self.resolutionLayout.setSpacing(4)

        self.contentLayout.addLayout(self.resolutionLayout)

        self.directoryLayout = QHBoxLayout()
        self.directoryLayout.setSpacing(0)
        self.directoryLayout.setObjectName(u"directoryLayout")
        self.directoryLabel = ElidingLabel(self.contentFrame, elide_mode=Qt.TextElideMode.ElideMiddle)
        self.directoryLabel.setObjectName(u"directoryLabel")
        self.directoryLabel.setStyleSheet(u"font-size: 14px;")

        self.directoryLayout.addWidget(self.directoryLabel)

        self.directoryEdit = QLineEdit(self.contentFrame)
        self.directoryEdit.setObjectName(u"directoryEdit")
        self.directoryEdit.setStyleSheet(u"font-size: 14px;")
        self.directoryEdit.setClearButtonEnabled(True)

        self.directoryLayout.addWidget(self.directoryEdit)

        self.openDirectoryButton = QPushButton(self.contentFrame)
        self.openDirectoryButton.setObjectName(u"openDirectoryButton")
        self.openDirectoryButton.setMinimumSize(QSize(26, 26))
        self.openDirectoryButton.setMaximumSize(QSize(26, 26))
        self.openDirectoryButton.setProperty("role", u"icon")

        self.directoryLayout.addWidget(self.openDirectoryButton)


        self.contentLayout.addLayout(self.directoryLayout)


        self.centerLayout.addLayout(self.contentLayout)


        self.contentFrameLayout.addLayout(self.centerLayout)

        self.progressBar = QProgressBar(self.contentFrame)
        self.progressBar.setObjectName(u"progressBar")
        self.progressBar.setMinimumSize(QSize(0, 6))
        self.progressBar.setMaximumSize(QSize(16777215, 6))
        self.progressBar.setMaximum(100)
        self.progressBar.setValue(0)
        self.progressBar.setTextVisible(False)
        self.progressBar.setProperty("state", u"waiting")

        self.contentFrameLayout.addWidget(self.progressBar)


        self.contentItemLayout.addWidget(self.contentFrame)


        self.retranslateUi(ContentItemWidget)

        QMetaObject.connectSlotsByName(ContentItemWidget)
    # setupUi

    def retranslateUi(self, ContentItemWidget):
        ContentItemWidget.setWindowTitle(QCoreApplication.translate("ContentItemWidget", u"ContentItemWidget", None))
#if QT_CONFIG(tooltip)
        self.indexLabel.setToolTip(QCoreApplication.translate("ContentItemWidget", u"Queue number", None))
#endif // QT_CONFIG(tooltip)
        self.indexLabel.setText(QCoreApplication.translate("ContentItemWidget", u"Queue number", None))
#if QT_CONFIG(tooltip)
        self.contentTypeLabel.setToolTip(QCoreApplication.translate("ContentItemWidget", u"Content type", None))
#endif // QT_CONFIG(tooltip)
        self.contentTypeLabel.setText(QCoreApplication.translate("ContentItemWidget", u"Content type", None))
#if QT_CONFIG(tooltip)
        self.channelImageLabel.setToolTip(QCoreApplication.translate("ContentItemWidget", u"Channel image", None))
#endif // QT_CONFIG(tooltip)
        self.channelImageLabel.setText(QCoreApplication.translate("ContentItemWidget", u"Channel image", None))
#if QT_CONFIG(tooltip)
        self.channelNameLabel.setToolTip(QCoreApplication.translate("ContentItemWidget", u"Channel name", None))
#endif // QT_CONFIG(tooltip)
        self.channelNameLabel.setText(QCoreApplication.translate("ContentItemWidget", u"Channel name", None))
#if QT_CONFIG(tooltip)
        self.statusLabel.setToolTip(QCoreApplication.translate("ContentItemWidget", u"Status", None))
#endif // QT_CONFIG(tooltip)
        self.statusLabel.setText(QCoreApplication.translate("ContentItemWidget", u"Status", None))
#if QT_CONFIG(tooltip)
        self.progressLabel.setToolTip(QCoreApplication.translate("ContentItemWidget", u"Progress", None))
#endif // QT_CONFIG(tooltip)
        self.progressLabel.setText(QCoreApplication.translate("ContentItemWidget", u"Progress", None))
#if QT_CONFIG(tooltip)
        self.fileSizeLabel.setToolTip(QCoreApplication.translate("ContentItemWidget", u"File size", None))
#endif // QT_CONFIG(tooltip)
        self.fileSizeLabel.setText(QCoreApplication.translate("ContentItemWidget", u"File size", None))
#if QT_CONFIG(tooltip)
        self.stateIconLabel.setToolTip(QCoreApplication.translate("ContentItemWidget", u"Status", None))
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
        self.directoryLabel.setToolTip(QCoreApplication.translate("ContentItemWidget", u"Directory", None))
#endif // QT_CONFIG(tooltip)
        self.directoryLabel.setText(QCoreApplication.translate("ContentItemWidget", u"Directory", None))
#if QT_CONFIG(tooltip)
        self.openDirectoryButton.setToolTip(QCoreApplication.translate("ContentItemWidget", u"Open directory", None))
#endif // QT_CONFIG(tooltip)
    # retranslateUi

