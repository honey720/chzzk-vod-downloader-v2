import config.config as config
from config.worker import SpeedTestWorker
from PySide6.QtWidgets import QDialog, QFormLayout, QLineEdit, QPushButton, QMessageBox, QLabel, QComboBox
from PySide6.QtCore import Signal, Qt

class SettingDialog(QDialog):
    """
    쿠키 설정을 위한 팝업창 예시.
    이전에 저장된 쿠키값을 인자로 받아, QLineEdit에 미리 세팅한다.
    """

    requestTest = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")

        self.load_and_update_config()
        self.initial_threads = self.config.get("threads")
        self.worker = None

        self.initUI()

    def load_and_update_config(self):
        # 설정 파일을 불러오고 누락된 항목 병합
        config_data = config.load_config()

        if config.merge_config(config.DEFAULT_CONFIG, config_data):
            config.save_config(config_data)
        
        self.config = config_data      

    def initUI(self):
        layout = QFormLayout()

        self.nidaut = QLineEdit()
        self.nidses = QLineEdit()
        self.nidaut.setText(self.config.get("cookies", {}).get("NID_AUT", ""))
        self.nidses.setText(self.config.get("cookies", {}).get("NID_SES", ""))

        layout.addRow("NID_AUT", self.nidaut)
        layout.addRow("NID_SES", self.nidses)

        self.helpButton = QPushButton("Help")
        self.helpButton.clicked.connect(self.showHelp)
        layout.addWidget(self.helpButton)

        self.threads = QLabel()
        self.threads.setText(str(self.initial_threads))
        layout.addRow("Threads", self.threads)

        self.testButton = QPushButton("Speed Test Start")
        self.testButton.clicked.connect(self.onTest)
        layout.addWidget(self.testButton)

        self.afterDownloadComplete = QComboBox()
        self.afterDownloadComplete.addItem("none")
        self.afterDownloadComplete.addItem("sleep")
        self.afterDownloadComplete.addItem("shutdown")

        index = self.afterDownloadComplete.findText(self.config.get("afterDownloadComplete"), Qt.MatchFlag.MatchExactly)
        if index != -1:
            self.afterDownloadComplete.setCurrentIndex(index)

        layout.addRow("After download complete", self.afterDownloadComplete)

        self.closeButton = QPushButton("Apply")
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
            "4. 'NID_AUT', 'NID_SES' Name의 Value 값을 붙여 넣으세요. <br>"
            "<br>"
            "How to get a sticky cookie<br>"
            f"1. Log in to <a href='{link}'>Chzzk</a>.<br>"
            "2. Press F12 to open the developer tool. <br>"
            "3. Click Cookies > https://chzzk.naver.com on the Application tab. <br>"
            "4. Add the values of 'NID_AUT' and 'NID_SES'."
        )
        QMessageBox.information(self, "Helper", msg)

    def onTest(self):
        self.requestTest.emit()

    def start_speed_test(self, flag):
        if flag:
            # 버튼 클릭 시 상태 업데이트 및 버튼 비활성화(중복 실행 방지)
            self.threads.setText("테스트 진행 중...")
            self.testButton.setEnabled(False)
            self.closeButton.setEnabled(False)
            
            # 스레드 생성 및 시그널 연결
            self.worker = SpeedTestWorker()
            self.worker.result_ready.connect(self.on_result)
            self.worker.error_occurred.connect(self.on_error)
            self.worker.finished.connect(self.on_finished)
            self.worker.start()
        else:
            QMessageBox.warning(self, "경고", "현재 다운로드 중입니다. 다운로드를 멈추고 시도하세요.")

    def on_result(self, result):
        download_speed = result['download'] / 8e6
        threads = int((download_speed // 8) * 4)
        self.initial_threads = max(threads, 4)
        self.threads.setText(
            f"다운로드 속도: {download_speed:.2f} MB/s\n스레드 수: {self.initial_threads}"
        )

    def on_error(self, error_message):
        # 오류 발생 시 메시지 박스로 사용자에게 알림
        QMessageBox.warning(self, "오류", f"테스트 중 오류 발생:\n{error_message}")

    def on_finished(self):
        # 테스트가 완료되면 버튼을 다시 활성화
        self.testButton.setEnabled(True)
        self.closeButton.setEnabled(True)

    def getCookies(self):
        """
        호출 측에서 다이얼로그가 닫힌 후, 입력한 쿠키값을 받아갈 수 있도록 하는 헬퍼 함수.
        """
        return self.nidaut.text(), self.nidses.text()
    
    def onApply(self):
        """
        '적용' 버튼을 클릭하면 설정 값을 저장하고 다이얼로그를 닫는다.
        """
        self.config['cookies'] = {"NID_AUT": self.nidaut.text(), "NID_SES": self.nidses.text()}
        self.config['threads'] = self.initial_threads
        self.config['afterDownloadComplete'] = self.afterDownloadComplete.currentText()
        config.save_config(self.config)
        self.accept()
        
    def closeEvent(self, event):
        """
        창을 닫을 때 실행되는 이벤트
        """
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.warning(
                self,
                "테스트 중",
                "테스트가 진행 중입니다.",
                QMessageBox.Ok
            )
            if reply == QMessageBox.Ok:
                event.ignore()  # 창 닫기 취소
                return
        event.accept()  # 창 닫기 진행