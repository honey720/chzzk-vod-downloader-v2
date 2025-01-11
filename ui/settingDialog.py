import config.config as config
from PyQt5.QtWidgets import QDialog, QFormLayout, QLineEdit, QPushButton, QMessageBox

class SettingDialog(QDialog):
    """
    쿠키 설정을 위한 팝업창 예시.
    이전에 저장된 쿠키값을 인자로 받아, QLineEdit에 미리 세팅한다.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")

        self.initUI()

    def initUI(self):
        layout = QFormLayout()

        self.nidaut = QLineEdit()
        self.nidses = QLineEdit()
        self.nidaut.setText(config.NID_AUT)
        self.nidses.setText(config.NID_SES)

        layout.addRow("NID_AUT", self.nidaut)
        layout.addRow("NID_SES", self.nidses)

        self.helpButton = QPushButton("도움말")
        self.helpButton.clicked.connect(self.showHelp)
        layout.addWidget(self.helpButton)

        self.closeButton = QPushButton("적용")
        self.closeButton.clicked.connect(self.onApply)
        layout.addWidget(self.closeButton)

        self.setLayout(layout)

    def showHelp(self):
        """
        쿠키를 얻는 방법 안내 메시지.
        """
        link = "https://chzzk.naver.com"
        msg = (
            "치지직 쿠키 얻는 방법<br><br>"
            f"1. <a href='{link}'>치지직</a>에 로그인 하세요. <br>"
            "2. F12를 눌러 개발자 도구를 열어주세요. <br>"
            "3. Application 탭에서 Cookies > https://chzzk.naver.com을 클릭하세요. <br>"
            "4. 'NID_AUT', 'NID_SES' Name의 Value 값을 붙여 넣으세요."
        )
        QMessageBox.information(self, "Helper", msg)

    def getCookies(self):
        """
        호출 측에서 다이얼로그가 닫힌 후, 입력한 쿠키값을 받아갈 수 있도록 하는 헬퍼 함수.
        """
        return self.nidaut.text(), self.nidses.text()
    
    def onApply(self):
        """
        '적용' 버튼을 클릭하면 설정 값을 저장하고 다이얼로그를 닫는다.
        """
        config.set_cookies(self.nidaut.text(), self.nidses.text())
        self.accept()