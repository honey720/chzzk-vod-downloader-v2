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
    QLineEdit, QMainWindow, QPushButton, QSizePolicy,
    QSpacerItem, QVBoxLayout, QWidget)

from content.view import ContentListView

class Ui_VodDownloader(object):
    def setupUi(self, VodDownloader):
        if not VodDownloader.objectName():
            VodDownloader.setObjectName(u"VodDownloader")
        VodDownloader.resize(600, 800)
        self.centralwidget = QWidget(VodDownloader)
        self.centralwidget.setObjectName(u"centralwidget")
        self.centralWidgetLayout = QVBoxLayout(self.centralwidget)
        self.centralWidgetLayout.setObjectName(u"centralWidgetLayout")
        self.headerFrame = QFrame(self.centralwidget)
        self.headerFrame.setObjectName(u"headerFrame")
        self.headerFrame.setFrameShape(QFrame.Shape.Box)
        self.headerFrame.setFrameShadow(QFrame.Shadow.Sunken)
        self.headerFrameLayout = QVBoxLayout(self.headerFrame)
        self.headerFrameLayout.setSpacing(8)
        self.headerFrameLayout.setObjectName(u"headerFrameLayout")
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

        self.settingButton = QPushButton(self.headerFrame)
        self.settingButton.setObjectName(u"settingButton")
        self.settingButton.setMinimumSize(QSize(32, 32))
        self.settingButton.setMaximumSize(QSize(32, 32))
        self.settingButton.setText(u"\u2699")
        self.settingButton.setProperty(u"role", u"icon")

        self.urlRowLayout.addWidget(self.settingButton)


        self.headerFrameLayout.addLayout(self.urlRowLayout)

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


        self.headerFrameLayout.addLayout(self.pathRowLayout)

        self.linkStatusLabel = QLabel(self.headerFrame)
        self.linkStatusLabel.setObjectName(u"linkStatusLabel")

        self.headerFrameLayout.addWidget(self.linkStatusLabel)


        self.centralWidgetLayout.addWidget(self.headerFrame)

        self.listView = ContentListView(self.centralwidget)
        self.listView.setObjectName(u"listView")
        self.listView.setAcceptDrops(True)

        self.centralWidgetLayout.addWidget(self.listView)

        self.infoFrame = QFrame(self.centralwidget)
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

        VodDownloader.setCentralWidget(self.centralwidget)
        QWidget.setTabOrder(self.urlInput, self.fetchButton)
        QWidget.setTabOrder(self.fetchButton, self.settingButton)
        QWidget.setTabOrder(self.settingButton, self.downloadPathInput)
        QWidget.setTabOrder(self.downloadPathInput, self.downloadPathButton)
        QWidget.setTabOrder(self.downloadPathButton, self.listView)
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

