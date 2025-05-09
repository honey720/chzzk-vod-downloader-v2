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
    QVBoxLayout, QWidget)

class Ui_ContentItemWidget(object):
    def setupUi(self, ContentItemWidget):
        if not ContentItemWidget.objectName():
            ContentItemWidget.setObjectName(u"ContentItemWidget")
        ContentItemWidget.resize(600, 134)
        self.contentItemLayout = QVBoxLayout(ContentItemWidget)
        self.contentItemLayout.setObjectName(u"contentItemLayout")
        self.contentItemLayout.setContentsMargins(10, 10, 10, 0)
        self.contentFrame = QFrame(ContentItemWidget)
        self.contentFrame.setObjectName(u"contentFrame")
        self.contentFrame.setStyleSheet(u"            #contentFrame {\n"
"                background-color: #424242;\n"
"                border-radius: 8px;\n"
"                padding: 0px;\n"
"            }\n"
"\n"
"            QFrame {\n"
"                color: #ffffff;\n"
"            }")
        self.contentFrame.setFrameShape(QFrame.Shape.StyledPanel)
        self.contentFrame.setFrameShadow(QFrame.Shadow.Raised)
        self.contentFrameLayout = QVBoxLayout(self.contentFrame)
        self.contentFrameLayout.setObjectName(u"contentFrameLayout")
        self.topLayout = QHBoxLayout()
        self.topLayout.setObjectName(u"topLayout")
        self.indexLabel = QLabel(self.contentFrame)
        self.indexLabel.setObjectName(u"indexLabel")
        self.indexLabel.setMinimumSize(QSize(30, 30))
        self.indexLabel.setMaximumSize(QSize(30, 30))
        self.indexLabel.setStyleSheet(u"font-size: 14px;")

        self.topLayout.addWidget(self.indexLabel)

        self.contentTypeLabel = QLabel(self.contentFrame)
        self.contentTypeLabel.setObjectName(u"contentTypeLabel")
        self.contentTypeLabel.setStyleSheet(u"font-size: 14px;")

        self.topLayout.addWidget(self.contentTypeLabel)

        self.channelImageLabel = QLabel(self.contentFrame)
        self.channelImageLabel.setObjectName(u"channelImageLabel")
        self.channelImageLabel.setMinimumSize(QSize(30, 30))
        self.channelImageLabel.setMaximumSize(QSize(30, 30))
        self.channelImageLabel.setStyleSheet(u"font-size: 14px;")
        self.channelImageLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.topLayout.addWidget(self.channelImageLabel)

        self.channelNameLabel = QLabel(self.contentFrame)
        self.channelNameLabel.setObjectName(u"channelNameLabel")
        self.channelNameLabel.setStyleSheet(u"font-size: 14px;")

        self.topLayout.addWidget(self.channelNameLabel)

        self.topSpacer = QSpacerItem(40, 20, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        self.topLayout.addItem(self.topSpacer)

        self.statusLabel = QLabel(self.contentFrame)
        self.statusLabel.setObjectName(u"statusLabel")
        self.statusLabel.setStyleSheet(u"font-size: 14px;")

        self.topLayout.addWidget(self.statusLabel)

        self.progressLabel = QLabel(self.contentFrame)
        self.progressLabel.setObjectName(u"progressLabel")
        self.progressLabel.setStyleSheet(u"font-size: 14px;")

        self.topLayout.addWidget(self.progressLabel)

        self.fileSizeLabel = QLabel(self.contentFrame)
        self.fileSizeLabel.setObjectName(u"fileSizeLabel")
        self.fileSizeLabel.setStyleSheet(u"font-size: 14px;")

        self.topLayout.addWidget(self.fileSizeLabel)

        self.deleteButton = QPushButton(self.contentFrame)
        self.deleteButton.setObjectName(u"deleteButton")
        self.deleteButton.setMinimumSize(QSize(30, 30))
        self.deleteButton.setMaximumSize(QSize(30, 30))

        self.topLayout.addWidget(self.deleteButton)


        self.contentFrameLayout.addLayout(self.topLayout)

        self.centerLayout = QHBoxLayout()
        self.centerLayout.setObjectName(u"centerLayout")
        self.thumbnailLabel = QLabel(self.contentFrame)
        self.thumbnailLabel.setObjectName(u"thumbnailLabel")
        self.thumbnailLabel.setMinimumSize(QSize(116, 66))
        self.thumbnailLabel.setMaximumSize(QSize(105, 60))
        self.thumbnailLabel.setStyleSheet(u"            #thumbnailLabel {\n"
"                background-color: #333333;\n"
"            }")
        self.thumbnailLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.centerLayout.addWidget(self.thumbnailLabel)

        self.contentLayout = QVBoxLayout()
        self.contentLayout.setSpacing(0)
        self.contentLayout.setObjectName(u"contentLayout")
        self.titleLayout = QHBoxLayout()
        self.titleLayout.setObjectName(u"titleLayout")
        self.titleLabel = QLabel(self.contentFrame)
        self.titleLabel.setObjectName(u"titleLabel")
        self.titleLabel.setStyleSheet(u"font-size: 14px;")

        self.titleLayout.addWidget(self.titleLabel)

        self.titleEdit = QLineEdit(self.contentFrame)
        self.titleEdit.setObjectName(u"titleEdit")
        self.titleEdit.setStyleSheet(u"font-size: 14px;")
        self.titleEdit.setClearButtonEnabled(True)

        self.titleLayout.addWidget(self.titleEdit)


        self.contentLayout.addLayout(self.titleLayout)

        self.directoryLayout = QHBoxLayout()
        self.directoryLayout.setSpacing(0)
        self.directoryLayout.setObjectName(u"directoryLayout")
        self.directoryLabel = QLabel(self.contentFrame)
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
        self.openDirectoryButton.setMinimumSize(QSize(30, 30))
        self.openDirectoryButton.setMaximumSize(QSize(30, 30))

        self.directoryLayout.addWidget(self.openDirectoryButton)


        self.contentLayout.addLayout(self.directoryLayout)


        self.centerLayout.addLayout(self.contentLayout)


        self.contentFrameLayout.addLayout(self.centerLayout)


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

