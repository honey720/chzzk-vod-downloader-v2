import os
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
        self.setWindowTitle(self.tr("Settings"))

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

        self.helpButton = QPushButton(self.tr("Help"))
        self.helpButton.clicked.connect(self.showHelp)
        layout.addWidget(self.helpButton)

        self.threads = QLabel()
        self.threads.setText(str(self.initial_threads))
        layout.addRow(self.tr("Threads"), self.threads)

        self.testButton = QPushButton(self.tr("Speed Test Start"))
        self.testButton.clicked.connect(self.onTest)
        layout.addWidget(self.testButton)

        self.afterDownloadComplete = QComboBox()
        self.afterDownloadComplete.addItem(self.tr("none"))
        self.afterDownloadComplete.addItem(self.tr("sleep"))
        self.afterDownloadComplete.addItem(self.tr("shutdown"))

        index = self.afterDownloadComplete.findText(self.config.get("afterDownloadComplete"), Qt.MatchFlag.MatchExactly)
        if index != -1:
            self.afterDownloadComplete.setCurrentIndex(index)

        layout.addRow(self.tr("After download complete"), self.afterDownloadComplete)

        self.language = QComboBox()
        self.language.addItem("English", "en_US")
        self.language.addItem("한국어", "ko_KR")

        # 현재 설정된 언어에 맞는 인덱스 찾기
        current_lang = self.config.get("language", "en_US")
        index = self.language.findData(current_lang)
        if index != -1:
            self.language.setCurrentIndex(index)

        layout.addRow(self.tr("Language"), self.language)

        self.logsFolder = QPushButton(self.tr("Open"))
        self.logsFolder.clicked.connect(self.openLogsFolder)
        layout.addRow(self.tr("Logs Folder"), self.logsFolder)

        self.closeButton = QPushButton(self.tr("Apply"))
        self.closeButton.clicked.connect(self.onApply)
        layout.addWidget(self.closeButton)

        self.setLayout(layout)

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

    def onTest(self):
        self.requestTest.emit()

    def start_speed_test(self, flag):
        if flag:
            # 버튼 클릭 시 상태 업데이트 및 버튼 비활성화(중복 실행 방지)
            self.threads.setText(self.tr("Testing..."))
            self.testButton.setEnabled(False)
            self.closeButton.setEnabled(False)
            
            # 스레드 생성 및 시그널 연결
            self.worker = SpeedTestWorker()
            self.worker.result_ready.connect(self.on_result)
            self.worker.error_occurred.connect(self.on_error)
            self.worker.finished.connect(self.on_finished)
            self.worker.start()
        else:
            QMessageBox.warning(self, self.tr("Warning"), self.tr("Download is in progress. Please stop the download and try again."))

    def on_result(self, result):
        download_speed = result['download'] / 8e6
        threads = int(download_speed // 8)
        self.initial_threads = max(threads, 4)
        self.threads.setText(
            self.tr("Download speed: {:.2f} MB/s\nThread count: {}").format(download_speed, self.initial_threads)
        )

    def on_error(self, error_message):
        # 오류 발생 시 메시지 박스로 사용자에게 알림
        QMessageBox.warning(self, self.tr("Error"), self.tr("Error occurred during test:\n{}").format(error_message))

    def on_finished(self):
        # 테스트가 완료되면 버튼을 다시 활성화
        self.testButton.setEnabled(True)
        self.closeButton.setEnabled(True)

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
        self.config['cookies'] = {"NID_AUT": self.nidaut.text(), "NID_SES": self.nidses.text()}
        self.config['threads'] = self.initial_threads
        self.config['afterDownloadComplete'] = self.afterDownloadComplete.currentText()
        self.config['language'] = self.language.currentData()  # 선택된 언어 코드 저장
        config.save_config(self.config)
        self.accept()
        
    def closeEvent(self, event):
        """
        창을 닫을 때 실행되는 이벤트
        """
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.warning(
                self,
                self.tr("Testing"),
                self.tr("Test is in progress."),
                QMessageBox.Ok
            )
            if reply == QMessageBox.Ok:
                event.ignore()  # 창 닫기 취소
                return
        event.accept()  # 창 닫기 진행