import threading

from content.item.base import ContentItem
from download.logger import DownloadLogger
from download.state import DownloadState

class DownloadTask:
    def __init__(self, item: ContentItem, logger: DownloadLogger):
        self.item = item
        self.state = DownloadState.WAITING  # 초기 상태로 설정
        self.logger = logger

        self.total_downloaded_size = 0
        self.total_size = 0

        self._pause_event = threading.Event()
        self._pause_event.set()
        self.adjust_threads = 4
        self.max_threads = self.adjust_threads
        self.start_time = 0
        self.end_time = 0
        self.future_count = 0
        self.total_ranges = 0
        self.threads_progress = []
        self.remaining_ranges = []
        self.completed_threads = 0
        self.failed_threads = 0
        self.restart_threads = 0
        self.completed_progress = 0
        self.speed_mb = 0
        self.prev_size = 0


    def start(self):
        self.state = DownloadState.RUNNING
        self.item.setDownloadState(self.state)
        self.logger.log_download_info(self.item)
        # 필요 시, ContentItem에 상태를 전달하여 업데이트

    def pause(self):
        self.state = DownloadState.PAUSED
        self._pause_event.clear() # 대기 상태로 들어감
        self.item.setDownloadState(self.state)
        # ContentItem에 상태 전달

    def resume(self):
        self.state = DownloadState.RUNNING
        self._pause_event.set()
        self.item.setDownloadState(self.state)
        # 상태 업데이트 전달

    def stop(self):
        self.state = DownloadState.WAITING
        self._pause_event.set()
        self.item.setDownloadState(self.state)
        # 상태 업데이트 전달

    def finish(self):
        self.state = DownloadState.FINISHED
        self.item.setDownloadState(self.state)
        # 상태 업데이트 전달

    def isRunning(self):
        return self.state == DownloadState.RUNNING