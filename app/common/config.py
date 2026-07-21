# coding:utf-8
import sys
from enum import Enum
from platformdirs import user_config_dir

from PySide6.QtCore import QLocale
from qfluentwidgets import (qconfig, QConfig, ConfigItem, OptionsConfigItem, BoolValidator,
                            OptionsValidator, RangeConfigItem, RangeValidator,
                            FolderListValidator, Theme, FolderValidator, ConfigSerializer, __version__)


class Language(Enum):
    """ Language enumeration """

    ENGLISH = QLocale(QLocale.English)
    KOREAN = QLocale(QLocale.Korean)
    AUTO = QLocale()


class LanguageSerializer(ConfigSerializer):
    """ Language serializer """

    def serialize(self, language):
        return language.value.name() if language != Language.AUTO else "Auto"

    def deserialize(self, value: str):
        return Language(QLocale(value)) if value != "Auto" else Language.AUTO


def isWin11():
    return sys.platform == 'win32' and sys.getwindowsversion().build >= 22000


class Config(QConfig):
    """ Config of application """

    # download
    downloadFolder = ConfigItem(
        "Download", "Folder", "app/download", FolderValidator())
    downloadAfterAction = OptionsConfigItem(
        "Download", "AfterAction", "Nothing", OptionsValidator(["Nothing", "Sleep", "Shutdown"]))

    # cookie
    cookieChzzkNidAut = ConfigItem(
        "Cookies", "NidAut", "")
    cookieChzzkNidSes = ConfigItem(
        "Cookies", "NidSes", "")

    # main window
    micaEnabled = ConfigItem("MainWindow", "MicaEnabled", isWin11(), BoolValidator())
    dpiScale = OptionsConfigItem(
        "MainWindow", "DpiScale", "Auto", OptionsValidator([1, 1.25, 1.5, 1.75, 2, "Auto"]), restart=True)
    language = OptionsConfigItem(
        "MainWindow", "Language", Language.AUTO, OptionsValidator(Language), LanguageSerializer(), restart=True)

    # Material
    blurRadius  = RangeConfigItem("Material", "AcrylicBlurRadius", 15, RangeValidator(0, 40))

    # software update
    checkUpdateAtStartUp = ConfigItem("Update", "CheckUpdateAtStartUp", True, BoolValidator())


YEAR = 2026
AUTHOR = "honey720"
VERSION = "2.8.0-dev-01"
HELP_URL = "https://qfluentwidgets.com"
REPO_URL = "https://github.com/honey720/chzzk-vod-downloader-v2"
FEEDBACK_URL = "https://github.com/honey720/chzzk-vod-downloader-v2/issues"
RELEASE_URL = "https://github.com/honey720/chzzk-vod-downloader-v2/releases/latest"


cfg = Config()
cfg.themeMode.value = Theme.AUTO
qconfig.load('app/config/config.json', cfg)