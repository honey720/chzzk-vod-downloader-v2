import os
import config.config as config
from PySide6.QtWidgets import QDialog, QMessageBox
from PySide6.QtCore import Signal

from config.worker import SpeedTestWorker
from ui.settingDialog import Ui_SettingDialog

class SettingDialog(QDialog, Ui_SettingDialog):
    """
    쿠키 설정을 위한 팝업창 예시.
    이전에 저장된 쿠키값을 인자로 받아, QLineEdit에 미리 세팅한다.
    """

    requestTest = Signal()

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

        self.threads.setText(str(self.initial_threads)) # 초기 스레드 수를 QLabel에 세팅

        self.testButton.clicked.connect(self.onTestStop) # 스피드 테스트 버튼 클릭 시 onTest 메소드 호출

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
        if self.checkStopAndClose():
            self.onApply()
            return super().accept()
        return False
    
    def reject(self):
        if self.checkStopAndClose():
            return super().reject()
        return False

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

    def onStartTest(self):
        self.requestTest.emit()

    def startSpeedTest(self, flag):
        if flag:
            # 버튼 클릭 시 상태 업데이트 및 버튼 비활성화(중복 실행 방지)
            self.threads.setText(self.tr("Testing..."))
            
            # 스레드 생성 및 시그널 연결
            self.worker = SpeedTestWorker()
            self.worker.result_ready.connect(self.on_result)
            self.worker.error_occurred.connect(self.on_error)
            self.worker.finished.connect(self.on_finished)
            self.worker.start()
        else:
            QMessageBox.warning(self, self.tr("Warning"), self.tr("Download is in progress. Please stop the download and try again."))

    def onTestStop(self):
        if self.testButton.text() == self.tr('Stop'):
            self.onStopTest()
            self.testButton.setText(self.tr('Speed Test'))
        else:
            self.onStartTest()
            self.testButton.setText(self.tr('Stop'))


    def onStopTest(self):
        self.worker.stop()  # 스레드 중지
        self.threads.setText(str(self.initial_threads))

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
        self.testButton.setText(self.tr('Speed Test'))

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
        self.config['afterDownload'] = self.afterDownload.currentData()
        self.config['language'] = self.language.currentData()  # 선택된 언어 코드 저장
        config.save_config(self.config)
        
    def closeEvent(self, event):
        """
        창을 닫을 때 실행되는 이벤트
        """
        if self.checkStopAndClose():
            event.accept()  # 창 닫기 진행
        else:
            event.ignore()  # 창 닫기 취소
    
    def checkStopAndClose(self):
        """
        테스트가 진행 중일 때, 사용자가 창을 닫으려 할 경우 확인 메시지를 띄운다.
        """
        if self.worker and self.worker.isRunning() and not self.worker.tester.is_interrupted():
            reply = QMessageBox.warning(
                self,
                self.tr("Testing"),
                self.tr("Test is in progress. Do you want to stop it?"),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply == QMessageBox.No:
                return False
            self.onStopTest()
        return True