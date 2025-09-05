import time as t
import threading

from PySide6.QtCore import QThread, Signal
from download.task import DownloadTask
from download.state import DownloadState
from time import strftime, gmtime

class MonitorThread(QThread):
    """
    VOD 파일을 multi-thread로 다운로드하는 작업 스레드 클래스
    """
    progress = Signal(str, str, str, int)

    def __init__(self, task: DownloadTask):
        super().__init__()
        self.task = task
        self.data = self.task.data
        self.logger = self.task.logger # 로거 초기화
        self.adjust_count = 0

    def run(self):
        """
        스레드가 시작될 때 자동으로 호출되는 메서드.
        실제 다운로드 파이프라인이 여기서 진행된다.
        """
        threading.current_thread().name = "MonitorThread"  # 스레드 시작 시 이름 재설정
        t.sleep(1)
        while self.task.state in [DownloadState.RUNNING, DownloadState.PAUSED]:
            if not self.data._pause_event.is_set():
                self.data._pause_event.wait()
                self.measure_speed()
            else:
                self._adjust_threads()
                self.measure_speed()
                self.update_progress()
            total_sleep = 1.0    # 총 1초 대기
            interval = 0.1       # 0.1초씩 대기
            elapsed = 0.0
            while elapsed < total_sleep and self.task.state in [DownloadState.RUNNING, DownloadState.PAUSED]:
                t.sleep(interval)
                elapsed += interval

    # ============ 다운로드 조정 및 콜백 메서드 ============

    def _adjust_threads(self):
        """
        다운로드 진행 중, 속도 등에 따라 스레드 수를 동적으로 조정하는 예시 스레드.
        """
        with self.task.lock:
            future_count = self.data.future_count

        avg_active_speed = (
            self.data.speed_mb / future_count if future_count > 0 else 0
        )

        if avg_active_speed > 4:
            self.adjust_count += 1
        elif avg_active_speed < 2:
            self.adjust_count -= 1
        else:
            if self.adjust_count > 0:
                self.adjust_count -= 1
            elif self.adjust_count < 0:
                self.adjust_count += 1
        
        if self.adjust_count > 1:
            self.data.adjust_threads = min(self.data.max_threads, self.data.adjust_threads + 4)
            self.logger.log_thread_adjust(self.data.adjust_threads, self.data.speed_mb) # 스레드 조정 로그
            self.adjust_count = 0
        elif self.adjust_count < -4:
            self.data.adjust_threads = max(1, self.data.adjust_threads // 2)
            self.logger.log_thread_adjust(self.data.adjust_threads, self.data.speed_mb) # 스레드 조정 로그
            self.adjust_count = 0

    def measure_speed(self):
            current_size = self.data.total_downloaded_size
            speed = current_size - self.data.prev_size
            self.data.prev_size = current_size

            with self.task.lock:
                future_count = self.data.future_count
            # MB/s로 변환
            self.data.speed_mb = speed / (1024*1024)
            avg_speed = self.data.speed_mb / future_count if future_count > 0 else 0
            self.logger.log_thread_debug(future_count, self.data.speed_mb, avg_speed)

    def update_progress(self):
        """
        진행률, 다운로드 속도, 예상 남은 시간 등 정보를 계산 후 시그널로 전송한다.
        """
        active_downloaded_size = sum(self.data.threads_progress)
        self.data.total_downloaded_size = self.data.completed_progress + active_downloaded_size
        # elapsed_time = time() - self.data.start_time

        progress = int((self.data.total_downloaded_size / self.data.total_size) * 100) if self.data.total_size > 0 else 0

        if self.data.speed_mb > 0:
            remaining_time = (
                (self.data.total_size - self.data.total_downloaded_size)
                / (self.data.speed_mb * 1024 * 1024)
            )
            #completion_time = elapsed_time + remaining_time
            #completion_time_str = strftime('%H:%M:%S', gmtime(completion_time))
            remaining_time_str = strftime('%H:%M:%S', gmtime(remaining_time))
        else:
            #completion_time_str = "N/A"
            remaining_time_str = "N/A"
        
        # 시그널 전송
        self.progress.emit(remaining_time_str, str(self.data.total_downloaded_size), f"{self.data.speed_mb:.1f} MB/s", progress)

    def get_download_time(self):
        download_time = self.data.end_time - self.data.start_time
        download_time_str = strftime('%H:%M:%S', gmtime(download_time))
        return download_time_str
