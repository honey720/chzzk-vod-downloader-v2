from config.speedtest.InterruptibleSpeedtest import InterruptibleSpeedtest
from PySide6.QtCore import QThread, Signal

class SpeedTestWorker(QThread):
    # 측정 결과와 오류 메시지를 전달하기 위한 시그널 정의
    result_ready = Signal(dict)
    error_occurred = Signal(str)

    def __init__(self):
        super().__init__()
        self.tester = InterruptibleSpeedtest(secure=1)

    def run(self):
        try:
            self.tester.get_best_server()

            if self.tester.is_interrupted():
                return
            
            download_speed = self.tester.download()

            if self.tester.is_interrupted():
                return

            result = {'download': download_speed}
            self.result_ready.emit(result)

        except Exception as e:
            self.error_occurred.emit(str(e))

    def stop(self):
        self.tester.interrupt()