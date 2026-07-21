# codig:utf-8
from typing import Union
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon, QColor
from PySide6.QtWidgets import QWidget, QLabel, QButtonGroup, QVBoxLayout, QPushButton, QHBoxLayout, QGridLayout

from qfluentwidgets import ExpandGroupSettingCard, ConfigItem, FluentIconBase, PasswordLineEdit, LineEdit
from ..common.config import qconfig

class CustomCookieSettingCard(ExpandGroupSettingCard):
    """ Custom cookie setting card """

    def __init__(self, nidAutConfigItem: ConfigItem, nidSesConfigItem: ConfigItem, icon: Union[str, QIcon, FluentIconBase], title: str,
                 content=None, parent=None):
        """
        Parameters
        ----------
        configItem1: ConfigItem
            first config item

        configItem2: ConfigItem
            second config item

        icon: str | QIcon | FluentIconBase
            the icon to be drawn

        title: str
            the title of setting card

        content: str
            the content of setting card

        parent: QWidget
            parent window
        """
        super().__init__(icon, title, content, parent=parent)
        self.nidAutConfigItem = nidAutConfigItem
        self.nidSesConfigItem = nidSesConfigItem

        self.cookieWidget = QWidget(self.view)
        self.cookieLayout = QGridLayout(self.cookieWidget)

        self.nidAutLabel = QLabel(self.tr('NID_AUT'), self.cookieWidget)
        self.nidAutLineEdit = LineEdit(self.cookieWidget)
        
        self.nidSesLabel = QLabel(self.tr('NID_SES'), self.cookieWidget)
        self.nidSesLineEdit = LineEdit(self.cookieWidget)

        self.__initWidget()

    def __initWidget(self):
        self.__initLayout()

        self.nidAutLineEdit.setText(qconfig.get(self.nidAutConfigItem))
        self.nidSesLineEdit.setText(qconfig.get(self.nidSesConfigItem))

        self.nidAutLabel.setObjectName("titleLabel")
        self.nidSesLabel.setObjectName("titleLabel")

        self.nidAutLineEdit.editingFinished.connect(self.__onNidAutChanged)
        self.nidSesLineEdit.editingFinished.connect(self.__onNidSesChanged)

    def __initLayout(self):

        self.cookieLayout.setContentsMargins(48, 18, 16, 18)
        self.cookieLayout.setSpacing(19)

        self.cookieLayout.addWidget(self.nidAutLabel, 0, 0, Qt.AlignLeft)
        self.cookieLayout.addWidget(self.nidSesLabel, 1, 0, Qt.AlignLeft)

        self.cookieLayout.addWidget(self.nidAutLineEdit, 0, 1)
        self.cookieLayout.addWidget(self.nidSesLineEdit, 1, 1)

        self.cookieLayout.setColumnStretch(1, 1)

        self.viewLayout.setSpacing(0)
        self.viewLayout.setContentsMargins(0, 0, 0, 0)
        self.addGroupWidget(self.cookieWidget)

    def __onNidAutChanged(self):
        text = self.nidAutLineEdit.text()
        qconfig.set(self.nidAutConfigItem, text)

    def __onNidSesChanged(self):
        text = self.nidSesLineEdit.text()
        qconfig.set(self.nidSesConfigItem, text)