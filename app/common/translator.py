# coding: utf-8
from PySide6.QtCore import QObject


class Translator(QObject):

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.download = self.tr('Download')
        self.record = self.tr('Record')
        self.favorite = self.tr('Favorite')
        self.about = self.tr('About')
        self.settings = self.tr('Settings')