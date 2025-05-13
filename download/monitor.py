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
            if not self.task._pause_event.is_set():
                self.task._pause_event.wait()
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

        avg_active_speed = (
            self.task.speed_mb / self.task.future_count if self.task.future_count > 0 else 0
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
            self.task.adjust_threads = min(self.task.max_threads, self.task.adjust_threads + 4)
            self.logger.log_thread_adjust(self.task.adjust_threads, self.task.speed_mb) # 스레드 조정 로그
            self.adjust_count = 0
        elif self.adjust_count < -4:
            self.task.adjust_threads = max(1, self.task.adjust_threads // 2)
            self.logger.log_thread_adjust(self.task.adjust_threads, self.task.speed_mb) # 스레드 조정 로그
            self.adjust_count = 0

    def measure_speed(self):
            current_size = self.task.total_downloaded_size
            speed = current_size - self.task.prev_size
            self.task.prev_size = current_size

            # MB/s로 변환
            self.task.speed_mb = speed / (1024*1024)
            avg_speed = self.task.speed_mb / self.task.future_count if self.task.future_count > 0 else 0
            self.logger.log_thread_debug(self.task.future_count, self.task.speed_mb, avg_speed)

    def update_progress(self):
        """
        진행률, 다운로드 속도, 예상 남은 시간 등 정보를 계산 후 시그널로 전송한다.
        """
        active_downloaded_size = sum(self.task.threads_progress)
        self.task.total_downloaded_size = self.task.completed_progress + active_downloaded_size
        # elapsed_time = time() - self.data.start_time

        progress = int((self.task.total_downloaded_size / self.task.total_size) * 100) if self.task.total_size > 0 else 0

        if self.task.speed_mb > 0:
            remaining_time = (
                (self.task.total_size - self.task.total_downloaded_size)
                / (self.task.speed_mb * 1024 * 1024)
            )
            #completion_time = elapsed_time + remaining_time
            #completion_time_str = strftime('%H:%M:%S', gmtime(completion_time))
            remaining_time_str = strftime('%H:%M:%S', gmtime(remaining_time))
        else:
            #completion_time_str = "N/A"
            remaining_time_str = "N/A"
        
        # 시그널 전송
        self.progress.emit(remaining_time_str, str(self.task.total_downloaded_size), f"{self.task.speed_mb:.1f} MB/s", progress)

    def get_download_time(self):
        download_time = self.task.end_time - self.task.start_time
        download_time_str = strftime('%H:%M:%S', gmtime(download_time))
        return download_time_str
