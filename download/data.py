import os
import threading
import config.config as config

class DownloadData:
    def __init__(self, video_url, output_path, resolution, content_type):
        self.total_downloaded_size = 0
        self.total_size = 0
        
        # 공유 변수
        self._pause_event = threading.Event()
        self._pause_event.set()
        #TODO: Task로 옮길지에 대해 논의

        # CPU 개수를 바탕으로 초깃값 지정
        self.adjust_threads = 4
        self.max_threads = self.adjust_threads

        self.start_time = 0
        self.end_time = 0
        self.future_count = 0

        self.total_ranges = 0
        self.threads_progress = []

        self.remaining_ranges = []

        self.speed_mb = 0

        # DownloadThread 내부 변수
        self.video_url = video_url
        self.output_path = output_path
        self.resolution = resolution
        self.content_type = content_type
        
        # 진행도/실패/재시작/스레드 관련 변수
        self.completed_threads = 0
        self.failed_threads = 0
        self.restart_threads = 0
        self.completed_progress = 0

        # MonitorThread 내부 변수
        self.prev_size = 0