# -*- coding: utf-8 -*-

################################################################################
## Form generated from reading UI file 'mainWindow.ui'
##
## Created by: Qt User Interface Compiler version 6.11.1
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
    QLineEdit, QMainWindow, QPushButton, QScrollArea, QSizePolicy,
    QSpacerItem, QVBoxLayout, QWidget)

from content.view import ContentListView

class Ui_VodDownloader(object):
    def setupUi(self, VodDownloader):
        if not VodDownloader.objectName():
            VodDownloader.setObjectName(u"VodDownloader")
        VodDownloader.resize(600, 800)
        # 창 전체 셸(#249):
        #   VodDownloader
        #   └ windowScrollArea  — 창 폭이 콘텐츠 최소폭보다 좁아졌을 때만 좌우 스크롤이
        #     │                   뜨는 안전망(접근성 배율). 세로 스크롤은 카드 목록 것
        #     │                   하나뿐이라 여기서는 항상 끈다.
        #     └ contentColumn   — 상단바·카드 목록·하단바를 담는 컨테이너. QScrollArea는
        #                         자식 위젯을 하나만 받으므로 셋을 스크롤 영역에 넣으려면
        #                         이 컨테이너가 필요하다. 그 밖의 역할(폭 제한·정렬)은 없다
        #                         — 창 폭을 그대로 채운다(오너 확정, #249).
        #     여백 수치는 application/mainWindow.py::_applyLayoutMetrics가
        #     theme.METRICS로 런타임에 건다.
        self.windowScrollArea = QScrollArea(VodDownloader)
        self.windowScrollArea.setObjectName(u"windowScrollArea")
        self.windowScrollArea.setFrameShape(QFrame.Shape.NoFrame)
        self.windowScrollArea.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.windowScrollArea.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.windowScrollArea.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.windowScrollArea.setWidgetResizable(True)
        self.contentColumn = QWidget()
        self.contentColumn.setObjectName(u"contentColumn")
        self.centralWidgetLayout = QVBoxLayout(self.contentColumn)
        self.centralWidgetLayout.setObjectName(u"centralWidgetLayout")
        self.headerFrame = QFrame(self.contentColumn)
        self.headerFrame.setObjectName(u"headerFrame")
        self.headerFrame.setFrameShape(QFrame.Shape.Box)
        self.headerFrame.setFrameShadow(QFrame.Shadow.Sunken)
        self.headerFrameLayout = QVBoxLayout(self.headerFrame)
        self.headerFrameLayout.setSpacing(8)
        self.headerFrameLayout.setObjectName(u"headerFrameLayout")

        # 상단 구조(#245 오너 확정):
        #   ┌ [URL 입력            ][VOD 추가 ] ┐
        #   │                                   │  ⚙
        #   └ [경로 입력          ✕][경로 찾기 ] ┘
        #   조회 상태 메시지 한 줄
        # 입력 블록(두 행)은 완결된 사각형이고 ⚙는 그 **밖**, 두 행 높이의
        # 세로 중앙에 하나다 — 설정은 입력과 성격이 달라 시각적으로도 갈린다.
        # 두 텍스트 버튼([VOD 추가]·[경로 찾기])은 같은 폭으로 좌우 끝을 맞춘다
        # (application/mainWindow.py::_equalizeHeaderButtons — 번역·폰트에 따라
        # 폭이 달라 런타임에 더 넓은 쪽으로 고정). ⚙를 어느 행에 붙이든 그 행의
        # 텍스트 버튼이 밀려 두 텍스트 버튼의 끝선이 어긋났다(실기 확인).
        self.headerRowsLayout = QHBoxLayout()
        self.headerRowsLayout.setSpacing(8)
        self.headerRowsLayout.setObjectName(u"headerRowsLayout")
        self.inputBlockLayout = QVBoxLayout()
        self.inputBlockLayout.setSpacing(8)
        self.inputBlockLayout.setObjectName(u"inputBlockLayout")

        self.urlRowLayout = QHBoxLayout()
        self.urlRowLayout.setSpacing(8)
        self.urlRowLayout.setObjectName(u"urlRowLayout")
        self.urlInput = QLineEdit(self.headerFrame)
        self.urlInput.setObjectName(u"urlInput")
        self.urlInput.setClearButtonEnabled(True)

        self.urlRowLayout.addWidget(self.urlInput)

        self.fetchButton = QPushButton(self.headerFrame)
        self.fetchButton.setObjectName(u"fetchButton")

        self.urlRowLayout.addWidget(self.fetchButton)


        self.inputBlockLayout.addLayout(self.urlRowLayout)

        self.pathRowLayout = QHBoxLayout()
        self.pathRowLayout.setSpacing(8)
        self.pathRowLayout.setObjectName(u"pathRowLayout")
        self.downloadPathInput = QLineEdit(self.headerFrame)
        self.downloadPathInput.setObjectName(u"downloadPathInput")
        self.downloadPathInput.setClearButtonEnabled(True)

        self.pathRowLayout.addWidget(self.downloadPathInput)

        self.downloadPathButton = QPushButton(self.headerFrame)
        self.downloadPathButton.setObjectName(u"downloadPathButton")

        self.pathRowLayout.addWidget(self.downloadPathButton)


        self.inputBlockLayout.addLayout(self.pathRowLayout)

        self.headerRowsLayout.addLayout(self.inputBlockLayout, 1)

        # 설정(⚙) — 입력 블록 밖, 두 행 높이의 세로 중앙(#245 오너 확정).
        # 하단으로 내리는 안은 채택하지 않는다 — 설정 안의 쿠키는 성인·멤버십
        # VOD를 받으려면 반드시 한 번은 찾아야 해서 하단 muted 묶음에 두면 못
        # 찾는다(설정 화면 .ui 재작성 때 재검토). 남는 공간은 각 행에서 입력창
        # 하나만 흡수한다(버튼들은 고정 폭) — tests/unit/test_header_layout.py.
        self.settingButton = QPushButton(self.headerFrame)
        self.settingButton.setObjectName(u"settingButton")
        self.settingButton.setMinimumSize(QSize(32, 32))
        self.settingButton.setMaximumSize(QSize(32, 32))
        self.settingButton.setText(u"⚙")
        self.settingButton.setProperty(u"role", u"icon")

        self.headerRowsLayout.addWidget(self.settingButton, 0, Qt.AlignmentFlag.AlignVCenter)

        self.headerFrameLayout.addLayout(self.headerRowsLayout)

        self.linkStatusLabel = QLabel(self.headerFrame)
        self.linkStatusLabel.setObjectName(u"linkStatusLabel")

        self.headerFrameLayout.addWidget(self.linkStatusLabel)


        self.centralWidgetLayout.addWidget(self.headerFrame)

        self.listView = ContentListView(self.contentColumn)
        self.listView.setObjectName(u"listView")
        self.listView.setAcceptDrops(True)

        self.centralWidgetLayout.addWidget(self.listView)

        self.infoFrame = QFrame(self.contentColumn)
        self.infoFrame.setObjectName(u"infoFrame")
        self.infoFrame.setFrameShape(QFrame.Shape.Box)
        self.infoFrame.setFrameShadow(QFrame.Shadow.Sunken)
        self.infoLayout = QHBoxLayout(self.infoFrame)
        self.infoLayout.setSpacing(8)
        self.infoLayout.setObjectName(u"infoLayout")
        self.downloadCountLabel = QLabel(self.infoFrame)
        self.downloadCountLabel.setObjectName(u"downloadCountLabel")

        self.infoLayout.addWidget(self.downloadCountLabel)

        self.clearFinishedButton = QPushButton(self.infoFrame)
        self.clearFinishedButton.setObjectName(u"clearFinishedButton")
        self.clearFinishedButton.setProperty(u"role", u"subtle")

        self.infoLayout.addWidget(self.clearFinishedButton)

        self.horizontalSpacer = QSpacerItem(0, 0, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        self.infoLayout.addItem(self.horizontalSpacer)

        self.downloadButton = QPushButton(self.infoFrame)
        self.downloadButton.setObjectName(u"downloadButton")

        self.infoLayout.addWidget(self.downloadButton)

        self.stopButton = QPushButton(self.infoFrame)
        self.stopButton.setObjectName(u"stopButton")
        self.stopButton.setEnabled(False)

        self.infoLayout.addWidget(self.stopButton)


        self.centralWidgetLayout.addWidget(self.infoFrame)

        self.windowScrollArea.setWidget(self.contentColumn)
        VodDownloader.setCentralWidget(self.windowScrollArea)
        QWidget.setTabOrder(self.urlInput, self.fetchButton)
        QWidget.setTabOrder(self.fetchButton, self.downloadPathInput)
        QWidget.setTabOrder(self.downloadPathInput, self.downloadPathButton)
        QWidget.setTabOrder(self.downloadPathButton, self.settingButton)
        QWidget.setTabOrder(self.settingButton, self.listView)
        QWidget.setTabOrder(self.listView, self.clearFinishedButton)
        QWidget.setTabOrder(self.clearFinishedButton, self.downloadButton)
        QWidget.setTabOrder(self.downloadButton, self.stopButton)

        self.retranslateUi(VodDownloader)

        QMetaObject.connectSlotsByName(VodDownloader)
    # setupUi

    def retranslateUi(self, VodDownloader):
        VodDownloader.setWindowTitle(QCoreApplication.translate("VodDownloader", u"Chzzk VOD Downloader", None))
        self.urlInput.setPlaceholderText(QCoreApplication.translate("VodDownloader", u"Enter Chzzk URL", None))
        self.fetchButton.setText(QCoreApplication.translate("VodDownloader", u"Add VOD", None))
#if QT_CONFIG(tooltip)
        self.settingButton.setToolTip(QCoreApplication.translate("VodDownloader", u"Settings", None))
#endif // QT_CONFIG(tooltip)
        self.downloadPathInput.setPlaceholderText(QCoreApplication.translate("VodDownloader", u"Enter download path", None))
        self.downloadPathButton.setText(QCoreApplication.translate("VodDownloader", u"Find path", None))
        self.downloadCountLabel.setText(QCoreApplication.translate("VodDownloader", u"Downloads: {}/{}", None))
        self.clearFinishedButton.setText(QCoreApplication.translate("VodDownloader", u"Clear Finished", None))
        self.downloadButton.setText(QCoreApplication.translate("VodDownloader", u"Download", None))
        self.stopButton.setText(QCoreApplication.translate("VodDownloader", u"Stop", None))
    # retranslateUi

