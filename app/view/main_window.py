# coding: utf-8
from typing import List
from PySide6.QtCore import Qt, Signal, QEasingCurve, QUrl, QSize, QTimer
from PySide6.QtGui import QColor, QIcon, Qt
from PySide6.QtWidgets import QApplication, QHBoxLayout, QFrame, QWidget

from enum import Enum
from qfluentwidgets import (MSFluentWindow, FluentWindow, NavigationItemPosition, SubtitleLabel,
                            setFont, SystemThemeListener, isDarkTheme, setTheme,
                            Theme, qconfig, StyleSheetBase, SettingCard, SplashScreen)
from qfluentwidgets import FluentIcon as FIF

from ..common.config import cfg
from ..common.icon import Icon
from ..common.signal_bus import signalBus
from ..common.translator import Translator
from ..common import resource

from app.view.setting_interface import SettingInterface
from app.view.download_interface import DownloadInterface

class Widget(QFrame):
    def __init__(self, text: str, parent=None):
        super().__init__(parent=parent)
        self.label = SubtitleLabel(text, self)
        self.label.setTextColor(QColor(0, 0, 0), QColor(255, 255, 255)) 
        self.hBoxLayout = QHBoxLayout(self)

        setFont(self.label, 24)
        self.label.setAlignment(Qt.AlignCenter)
        self.hBoxLayout.addWidget(self.label, 1, Qt.AlignCenter)

        # Must set a globally unique object name for the sub-interface
        self.setObjectName(text.replace(' ', '-'))

class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.initWindow()

        # create system theme listener
        self.themeListener = SystemThemeListener(self)

        self.downloadInterface = DownloadInterface(self)
        self.recordInterface = Widget('Record Interface', self)
        self.favoriteInterface = Widget('Favorite Interface', self)
        self.settingInterface = SettingInterface(self)
        self.aboutInterface = Widget('About Interface', self)

        self.connectSignalToSlot()

        # add items to navigation interface
        self.initNavigation()
        self.splashScreen.finish()

        # start theme listener
        self.themeListener.start()

    def connectSignalToSlot(self):
        signalBus.micaEnableChanged.connect(self.setMicaEffectEnabled)
        #signalBus.switchToSampleCard.connect(self.switchToSample)
        #signalBus.supportSignal.connect(self.onSupport)

    def initNavigation(self):
        # add navigation items
        t = Translator()
        self.addSubInterface(self.downloadInterface, FIF.DOWNLOAD, t.download)
        self.addSubInterface(self.recordInterface, Icon.RECORD, t.record)
        self.addSubInterface(self.favoriteInterface, FIF.HEART, t.favorite)

        pos = NavigationItemPosition.BOTTOM
        self.addSubInterface(self.aboutInterface, FIF.INFO, t.about, pos)
        self.addSubInterface(self.settingInterface, FIF.SETTING, t.settings, pos)

    def initWindow(self):
        self.resize(600, 700)
        self.setMinimumWidth(600)
        self.setWindowIcon(QIcon(':/CVDv2/images/logo.png'))
        self.setWindowTitle("VOD Downloader")

        self.setMicaEffectEnabled(cfg.get(cfg.micaEnabled))

        # create splash screen
        self.splashScreen = SplashScreen(self.windowIcon(), self)
        self.splashScreen.setIconSize(QSize(102, 102))
        self.splashScreen.raise_()

        desktop = QApplication.screens()[0].availableGeometry()
        w, h = desktop.width(), desktop.height()
        self.move(w//2 - self.width()//2, h//2 - self.height()//2)
        self.show()
        QApplication.processEvents()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, 'splashScreen'):
            self.splashScreen.resize(self.size())

    def closeEvent(self, e):
        # Stop the listener thread
        self.themeListener.terminate()
        self.themeListener.deleteLater()
        super().closeEvent(e)

    def _onThemeChangedFinished(self):
        super()._onThemeChangedFinished()

        # Retry mechanism needed when mica effect is enabled
        if self.isMicaEffectEnabled():
            QTimer.singleShot(100, lambda: self.windowEffect.setMicaEffect(self.winId(), isDarkTheme()))
