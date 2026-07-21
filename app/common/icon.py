# coding: utf-8
from enum import Enum

from qfluentwidgets import FluentIconBase, getIconColor, Theme


class Icon(FluentIconBase, Enum):

    RECORD = "Record"
    COOKIE = "Cookie"

    def path(self, theme=Theme.AUTO):
        return f":/CVDv2/images/icons/{self.value}_{getIconColor(theme)}.svg"
