import threading
from speedtest import Speedtest

class InterruptibleSpeedtest(Speedtest):
    def __init__(self, *args, secure=1, **kwargs):
        super().__init__(*args, **kwargs)

        self.secure = secure

        # shutdown 이벤트를 외부에서 트리거할 수 있도록 노출
        self._shutdown_event = threading.Event()

    def interrupt(self):
        """외부에서 호출해서 다운로드나 업로드 테스트를 중단시킴"""
        self._shutdown_event.set()

    def is_interrupted(self):
        """현재 중단 상태인지 확인"""
        return self._shutdown_event.is_set()