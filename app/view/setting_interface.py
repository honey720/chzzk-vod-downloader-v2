# coding:utf-8
from qfluentwidgets import ScrollArea, TitleLabel, ExpandLayout, setTheme, setThemeColor, SettingCardGroup, SwitchSettingCard, CustomColorSettingCard, OptionsSettingCard, ComboBoxSettingCard, PushSettingCard
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import InfoBar

from PySide6.QtCore import Qt, Signal, QUrl, QStandardPaths
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QWidget, QLabel, QFileDialog

from ..components.custom_cookie_setting_card import CustomCookieSettingCard
from ..common.config import cfg, FEEDBACK_URL, isWin11
from ..common.signal_bus import signalBus
from ..common.style_sheet import StyleSheet
from ..common.icon import Icon


class SettingInterface(ScrollArea):
    """ Setting interface """

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.scrollWidget = QWidget()
        self.expandLayout = ExpandLayout(self.scrollWidget)

        # setting label
        self.settingLabel = TitleLabel(self.tr("Settings"), self)

        # personalization
        self.personalGroup = SettingCardGroup(
            self.tr('Personalization'), self.scrollWidget)
        self.micaCard = SwitchSettingCard(
            FIF.TRANSPARENT,
            self.tr('Mica effect'),
            self.tr('Apply semi transparent to windows and surfaces'),
            cfg.micaEnabled,
            self.personalGroup
        )
        self.themeCard = OptionsSettingCard(
            cfg.themeMode,
            FIF.BRUSH,
            self.tr('Application theme'),
            self.tr('Change the appearance of your application'),
            texts=[
                self.tr('Light'), self.tr('Dark'),
                self.tr('Use system setting')
            ],
            parent=self.personalGroup
        )
        self.themeColorCard = CustomColorSettingCard(
            cfg.themeColor,
            FIF.PALETTE,
            self.tr('Theme color'),
            self.tr('Change the theme color of your application'),
            self.personalGroup
        )
        self.zoomCard = OptionsSettingCard(
            cfg.dpiScale,
            FIF.ZOOM,
            self.tr('Interface zoom'),
            self.tr('Change the size of widgets and fonts'),
            texts=[
                '100%', '125%', '150%', '175%', '200%',
                self.tr('Use system setting')
            ],
            parent=self.personalGroup
        )
        self.languageCard = ComboBoxSettingCard(
            cfg.language,
            FIF.LANGUAGE,
            self.tr('Language'),
            self.tr('Set your preferred language for UI'),
            texts=['English', '한국어', self.tr('Use system setting')],
            parent=self.personalGroup
        )

        # download
        self.downloadGroup = SettingCardGroup(
            self.tr('Download'), self.scrollWidget)
        self.downloadFolderCard = PushSettingCard(
            self.tr('Choose folder'),
            FIF.DOWNLOAD,
            self.tr('Download directory'),
            cfg.get(cfg.downloadFolder),
            self.downloadGroup
        )
        self.cookiesInputCard = CustomCookieSettingCard(
            cfg.cookieChzzkNidAut,
            cfg.cookieChzzkNidSes,
            Icon.COOKIE,
            self.tr('Cookies'),
            self.tr('Set your cookies for downloading'),
            self.downloadGroup
        )
        self.downloadAfterActionCard = OptionsSettingCard(
            cfg.downloadAfterAction,
            FIF.COMPLETED,
            self.tr('After Download Action'),
            self.tr('Choose the action after downloading completed'),
            texts=[
                self.tr('Nothing'),
                self.tr('Sleep'),
                self.tr('Shutdown')
            ],
            parent=self.downloadGroup
        )

        self.__initWidget()

    def __initWidget(self):
        self.resize(600, 700)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setViewportMargins(0, 80, 0, 20)
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.setObjectName('settingInterface')

        # initialize style sheet
        self.scrollWidget.setObjectName('scrollWidget')
        self.settingLabel.setObjectName('settingLabel')
        StyleSheet.SETTING_INTERFACE.apply(self)

        # initialize layout
        self.__initLayout()
        self.__connectSignalToSlot()

    def __initLayout(self):
        self.settingLabel.move(36, 30)

        self.personalGroup.addSettingCard(self.micaCard)
        self.personalGroup.addSettingCard(self.themeCard)
        self.personalGroup.addSettingCard(self.themeColorCard)
        self.personalGroup.addSettingCard(self.zoomCard)
        self.personalGroup.addSettingCard(self.languageCard)

        self.downloadGroup.addSettingCard(self.downloadFolderCard)
        self.downloadGroup.addSettingCard(self.cookiesInputCard)
        self.downloadGroup.addSettingCard(self.downloadAfterActionCard)

        # add cards to group
        self.expandLayout.setSpacing(28)
        self.expandLayout.setContentsMargins(36, 10, 36, 0)
        self.expandLayout.addWidget(self.personalGroup)
        self.expandLayout.addWidget(self.downloadGroup)

    def __showRestartTooltip(self):
        """ show restart tooltip """
        InfoBar.success(
            self.tr('Updated successfully'),
            self.tr('Configuration takes effect after restart'),
            duration=1500,
            parent=self
        )
    
    def __onDownloadFolderCardClicked(self):
        """ download folder card clicked slot """
        folder = QFileDialog.getExistingDirectory(
            self, self.tr("Choose folder"), "./")
        if not folder or cfg.get(cfg.downloadFolder) == folder:
            return

        cfg.set(cfg.downloadFolder, folder)
        self.downloadFolderCard.setContent(folder)

    def __connectSignalToSlot(self):
        """ connect signal to slot """
        cfg.appRestartSig.connect(self.__showRestartTooltip)

        # download
        self.downloadFolderCard.clicked.connect(
            self.__onDownloadFolderCardClicked)

        # personalization
        cfg.themeChanged.connect(setTheme)
        self.themeColorCard.colorChanged.connect(lambda c: setThemeColor(c))
        self.micaCard.checkedChanged.connect(signalBus.micaEnableChanged)

        # about
    #    self.feedbackCard.clicked.connect(
    #        lambda: QDesktopServices.openUrl(QUrl(FEEDBACK_URL)))

