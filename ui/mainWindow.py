# -*- coding: utf-8 -*-

################################################################################
## Form generated from reading UI file 'mainWindow.ui'
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
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QPushButton, QSizePolicy, QSpacerItem, QVBoxLayout,
    QWidget)

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
        self.headerFrameLayout = QGridLayout(self.headerFrame)
        self.headerFrameLayout.setObjectName(u"headerFrameLayout")
        self.settingButton = QPushButton(self.headerFrame)
        self.settingButton.setObjectName(u"settingButton")

        self.headerFrameLayout.addWidget(self.settingButton, 4, 2, 1, 1)

        self.urlInput = QLineEdit(self.headerFrame)
        self.urlInput.setObjectName(u"urlInput")
        self.urlInput.setClearButtonEnabled(True)

        self.headerFrameLayout.addWidget(self.urlInput, 0, 1, 1, 1)

        self.urlInputLabel = QLabel(self.headerFrame)
        self.urlInputLabel.setObjectName(u"urlInputLabel")

        self.headerFrameLayout.addWidget(self.urlInputLabel, 0, 0, 1, 1)

        self.linkStatusLabel = QLabel(self.headerFrame)
        self.linkStatusLabel.setObjectName(u"linkStatusLabel")

        self.headerFrameLayout.addWidget(self.linkStatusLabel, 4, 0, 1, 2)

        self.downloadPathLabel = QLabel(self.headerFrame)
        self.downloadPathLabel.setObjectName(u"downloadPathLabel")

        self.headerFrameLayout.addWidget(self.downloadPathLabel, 1, 0, 1, 1)

        self.downloadPathInput = QLineEdit(self.headerFrame)
        self.downloadPathInput.setObjectName(u"downloadPathInput")
        self.downloadPathInput.setClearButtonEnabled(True)

        self.headerFrameLayout.addWidget(self.downloadPathInput, 1, 1, 1, 1)

        self.fetchButton = QPushButton(self.headerFrame)
        self.fetchButton.setObjectName(u"fetchButton")

        self.headerFrameLayout.addWidget(self.fetchButton, 0, 2, 1, 1)

        self.downloadPathButton = QPushButton(self.headerFrame)
        self.downloadPathButton.setObjectName(u"downloadPathButton")

        self.headerFrameLayout.addWidget(self.downloadPathButton, 1, 2, 1, 1)


        self.centralWidgetLayout.addWidget(self.headerFrame)

        self.listView = ContentListView(self.centralwidget)
        self.listView.setObjectName(u"listView")
        self.listView.setAcceptDrops(True)
        self.listView.setDragEnabled(True)
        self.listView.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)

        self.centralWidgetLayout.addWidget(self.listView)

        self.infoFrame = QFrame(self.centralwidget)
        self.infoFrame.setObjectName(u"infoFrame")
        self.infoFrame.setFrameShape(QFrame.Shape.Box)
        self.infoFrame.setFrameShadow(QFrame.Shadow.Sunken)
        self.infoLayout = QHBoxLayout(self.infoFrame)
        self.infoLayout.setObjectName(u"infoLayout")
        self.downloadCountLabel = QLabel(self.infoFrame)
        self.downloadCountLabel.setObjectName(u"downloadCountLabel")

        self.infoLayout.addWidget(self.downloadCountLabel)

        self.clearFinishedButton = QPushButton(self.infoFrame)
        self.clearFinishedButton.setObjectName(u"clearFinishedButton")

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
        self.settingButton.setText(QCoreApplication.translate("VodDownloader", u"Settings", None))
        self.urlInput.setPlaceholderText(QCoreApplication.translate("VodDownloader", u"Enter Chzzk URL", None))
        self.urlInputLabel.setText(QCoreApplication.translate("VodDownloader", u"Chzzk VOD URL:", None))
        self.downloadPathLabel.setText(QCoreApplication.translate("VodDownloader", u"Download Path:", None))
        self.downloadPathInput.setPlaceholderText(QCoreApplication.translate("VodDownloader", u"Enter download path", None))
        self.fetchButton.setText(QCoreApplication.translate("VodDownloader", u"Add VOD", None))
        self.downloadPathButton.setText(QCoreApplication.translate("VodDownloader", u"Find path", None))
        self.downloadCountLabel.setText(QCoreApplication.translate("VodDownloader", u"Downloads: {}/{}", None))
        self.clearFinishedButton.setText(QCoreApplication.translate("VodDownloader", u"Clear Finished", None))
        self.downloadButton.setText(QCoreApplication.translate("VodDownloader", u"Download", None))
        self.stopButton.setText(QCoreApplication.translate("VodDownloader", u"Stop", None))
    # retranslateUi

