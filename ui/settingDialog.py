# -*- coding: utf-8 -*-

################################################################################
## Form generated from reading UI file 'settingDialog.ui'
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
from PySide6.QtWidgets import (QAbstractButton, QApplication, QComboBox, QDialog,
    QDialogButtonBox, QFormLayout, QGroupBox, QLabel,
    QLineEdit, QPushButton, QSizePolicy, QSpacerItem,
    QVBoxLayout, QWidget)

class Ui_SettingDialog(object):
    def setupUi(self, SettingDialog):
        if not SettingDialog.objectName():
            SettingDialog.setObjectName(u"SettingDialog")
        SettingDialog.resize(400, 400)
        self.settingLayout = QVBoxLayout(SettingDialog)
        self.settingLayout.setObjectName(u"settingLayout")
        self.dialogLayout = QVBoxLayout()
        self.dialogLayout.setObjectName(u"dialogLayout")
        self.cookiesBox = QGroupBox(SettingDialog)
        self.cookiesBox.setObjectName(u"cookiesBox")
        self.cookiesFormLayout = QFormLayout(self.cookiesBox)
        self.cookiesFormLayout.setObjectName(u"cookiesFormLayout")
        self.nidautLabel = QLabel(self.cookiesBox)
        self.nidautLabel.setObjectName(u"nidautLabel")

        self.cookiesFormLayout.setWidget(0, QFormLayout.LabelRole, self.nidautLabel)

        self.nidaut = QLineEdit(self.cookiesBox)
        self.nidaut.setObjectName(u"nidaut")
        self.nidaut.setClearButtonEnabled(True)

        self.cookiesFormLayout.setWidget(0, QFormLayout.FieldRole, self.nidaut)

        self.nidsesLabel = QLabel(self.cookiesBox)
        self.nidsesLabel.setObjectName(u"nidsesLabel")

        self.cookiesFormLayout.setWidget(1, QFormLayout.LabelRole, self.nidsesLabel)

        self.nidses = QLineEdit(self.cookiesBox)
        self.nidses.setObjectName(u"nidses")
        self.nidses.setClearButtonEnabled(True)

        self.cookiesFormLayout.setWidget(1, QFormLayout.FieldRole, self.nidses)

        self.helpButton = QPushButton(self.cookiesBox)
        self.helpButton.setObjectName(u"helpButton")

        self.cookiesFormLayout.setWidget(2, QFormLayout.LabelRole, self.helpButton)

        self.cookieSpacer = QSpacerItem(0, 0, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        self.cookiesFormLayout.setItem(2, QFormLayout.FieldRole, self.cookieSpacer)


        self.dialogLayout.addWidget(self.cookiesBox)

        self.downloadBox = QGroupBox(SettingDialog)
        self.downloadBox.setObjectName(u"downloadBox")
        self.formLayout = QFormLayout(self.downloadBox)
        self.formLayout.setObjectName(u"formLayout")
        self.threadsLabel = QLabel(self.downloadBox)
        self.threadsLabel.setObjectName(u"threadsLabel")

        self.formLayout.setWidget(0, QFormLayout.LabelRole, self.threadsLabel)

        self.threads = QLabel(self.downloadBox)
        self.threads.setObjectName(u"threads")

        self.formLayout.setWidget(0, QFormLayout.FieldRole, self.threads)

        self.testButton = QPushButton(self.downloadBox)
        self.testButton.setObjectName(u"testButton")

        self.formLayout.setWidget(1, QFormLayout.LabelRole, self.testButton)

        self.downloadSpacer = QSpacerItem(0, 0, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        self.formLayout.setItem(1, QFormLayout.FieldRole, self.downloadSpacer)

        self.afterDownloadLabel = QLabel(self.downloadBox)
        self.afterDownloadLabel.setObjectName(u"afterDownloadLabel")

        self.formLayout.setWidget(2, QFormLayout.LabelRole, self.afterDownloadLabel)

        self.afterDownload = QComboBox(self.downloadBox)
        self.afterDownload.setObjectName(u"afterDownload")

        self.formLayout.setWidget(2, QFormLayout.FieldRole, self.afterDownload)


        self.dialogLayout.addWidget(self.downloadBox)

        self.commonBox = QGroupBox(SettingDialog)
        self.commonBox.setObjectName(u"commonBox")
        self.commonFormLayout = QFormLayout(self.commonBox)
        self.commonFormLayout.setObjectName(u"commonFormLayout")
        self.languageLabel = QLabel(self.commonBox)
        self.languageLabel.setObjectName(u"languageLabel")

        self.commonFormLayout.setWidget(0, QFormLayout.LabelRole, self.languageLabel)

        self.language = QComboBox(self.commonBox)
        self.language.setObjectName(u"language")

        self.commonFormLayout.setWidget(0, QFormLayout.FieldRole, self.language)

        self.logsFolderLabel = QLabel(self.commonBox)
        self.logsFolderLabel.setObjectName(u"logsFolderLabel")

        self.commonFormLayout.setWidget(1, QFormLayout.LabelRole, self.logsFolderLabel)

        self.logsFolder = QPushButton(self.commonBox)
        self.logsFolder.setObjectName(u"logsFolder")

        self.commonFormLayout.setWidget(1, QFormLayout.FieldRole, self.logsFolder)


        self.dialogLayout.addWidget(self.commonBox)


        self.settingLayout.addLayout(self.dialogLayout)

        self.dialogButtonBox = QDialogButtonBox(SettingDialog)
        self.dialogButtonBox.setObjectName(u"dialogButtonBox")
        self.dialogButtonBox.setStandardButtons(QDialogButtonBox.StandardButton.Cancel|QDialogButtonBox.StandardButton.Ok)

        self.settingLayout.addWidget(self.dialogButtonBox)


        self.retranslateUi(SettingDialog)
        self.dialogButtonBox.accepted.connect(SettingDialog.accept)
        self.dialogButtonBox.rejected.connect(SettingDialog.reject)

        QMetaObject.connectSlotsByName(SettingDialog)
    # setupUi

    def retranslateUi(self, SettingDialog):
        SettingDialog.setWindowTitle(QCoreApplication.translate("SettingDialog", u"Settings", None))
#if QT_CONFIG(whatsthis)
        self.cookiesBox.setWhatsThis("")
#endif // QT_CONFIG(whatsthis)
        self.cookiesBox.setTitle(QCoreApplication.translate("SettingDialog", u"Cookies", None))
        self.nidautLabel.setText(QCoreApplication.translate("SettingDialog", u"NID_AUT", None))
        self.nidsesLabel.setText(QCoreApplication.translate("SettingDialog", u"NID_SES", None))
        self.helpButton.setText(QCoreApplication.translate("SettingDialog", u"Help", None))
        self.downloadBox.setTitle(QCoreApplication.translate("SettingDialog", u"Download", None))
        self.threadsLabel.setText(QCoreApplication.translate("SettingDialog", u"Threads", None))
        self.testButton.setText(QCoreApplication.translate("SettingDialog", u"Speed Test", None))
        self.afterDownloadLabel.setText(QCoreApplication.translate("SettingDialog", u"After Download", None))
        self.commonBox.setTitle(QCoreApplication.translate("SettingDialog", u"Common", None))
        self.languageLabel.setText(QCoreApplication.translate("SettingDialog", u"Language", None))
        self.logsFolderLabel.setText(QCoreApplication.translate("SettingDialog", u"Logs Folder", None))
        self.logsFolder.setText(QCoreApplication.translate("SettingDialog", u"Open", None))
    # retranslateUi

