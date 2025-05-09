import os
import config.config as config
from PySide6.QtWidgets import QDialog, QMessageBox
from PySide6.QtCore import Signal

from ui.settingDialog import Ui_SettingDialog

class SettingDialog(QDialog, Ui_SettingDialog):
    """
    쿠키 설정을 위한 팝업창 예시.
    이전에 저장된 쿠키값을 인자로 받아, QLineEdit에 미리 세팅한다.
    """

    def __init__(self, parent=None):
        super().__init__(parent)

        self.config = config.load_config()
        self.initial_threads = self.config.get("threads")
        self.worker = None

        self.setupUi(self)
        self.setupDynamicUi()

    def setupDynamicUi(self):
        self.nidaut.setText(self.config.get("cookies", {}).get("NID_AUT", "")) # 쿠키값을 불러와서 QLineEdit에 세팅
        self.nidses.setText(self.config.get("cookies", {}).get("NID_SES", ""))

        self.helpButton.clicked.connect(self.showHelp) # 도움말 버튼 클릭 시 showHelp 메소드 호출

        self.afterDownload.addItem(self.tr("none"), "none") # 다운로드 완료 후 동작을 선택할 수 있는 QComboBox 생성
        self.afterDownload.addItem(self.tr("sleep"), "sleep")
        self.afterDownload.addItem(self.tr("shutdown"), "shutdown")

        currentAfterDownload = self.config.get("afterDownload", "none") # 현재 설정된 afterDownload 값을 불러옴
        index = self.afterDownload.findData(currentAfterDownload)
        if index != -1:
            self.afterDownload.setCurrentIndex(index)

        self.language.addItem("English", "en_US") # 언어 선택을 위한 QComboBox 생성 TODO: 언어 리스트는 project.pro에서 관리
        self.language.addItem("한국어", "ko_KR")

        currentLang = self.config.get("language", "en_US") # 현재 설정된 언어에 맞는 인덱스 찾기
        index = self.language.findData(currentLang)
        if index != -1:
            self.language.setCurrentIndex(index)
        
        self.logsFolder.clicked.connect(self.openLogsFolder) # 로그 폴더 열기 버튼 클릭 시 openLogsFolder 메소드 호출

    def accept(self):
        self.config['cookies'] = {"NID_AUT": self.nidaut.text(), "NID_SES": self.nidses.text()}
        self.config['afterDownload'] = self.afterDownload.currentData()
        self.config['language'] = self.language.currentData()  # 선택된 언어 코드 저장
        config.save_config(self.config)
        return super().accept()
    
    def reject(self):
        return super().reject()

    def showHelp(self):
        """
        쿠키를 얻는 방법 안내 메시지.
        """
        link = "https://chzzk.naver.com"
        msg = self.tr(
            "How to get a Chzzk cookie<br>"
            "1. Log in to <a href='{}'>Chzzk</a>.<br>"
            "2. Press F12 to open the developer tool. <br>"
            "3. Click Cookies > https://chzzk.naver.com on the Application tab. <br>"
            "4. Add the values of 'NID_AUT' and 'NID_SES'."
            ).format(link, link)
        QMessageBox.information(self, self.tr("Helper"), msg)

    def openLogsFolder(self):
        os.startfile(os.path.join(config.CONFIG_DIR, "logs"))

    def getCookies(self):
        """
        호출 측에서 다이얼로그가 닫힌 후, 입력한 쿠키값을 받아갈 수 있도록 하는 헬퍼 함수.
        """
        return self.nidaut.text(), self.nidses.text()
    
    def onApply(self):
        """
        '적용' 버튼을 클릭하면 설정 값을 저장하고 다이얼로그를 닫는다.
        """