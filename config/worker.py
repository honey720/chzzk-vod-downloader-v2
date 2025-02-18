import speedtest
from PySide6.QtCore import QThread, Signal

class SpeedTestWorker(QThread):
    # 측정 결과와 오류 메시지를 전달하기 위한 시그널 정의
    result_ready = Signal(dict)
    error_occurred = Signal(str)

    def run(self):
        try:
            self.st = speedtest.Speedtest(secure = 1)
            self.st.get_best_server()

            download_speed = self.st.download()

            result = {
                'download': download_speed
            }

            self.result_ready.emit(result)
        except Exception as e:
            self.error_occurred.emit(str(e))