import os
import logging
from datetime import datetime
from typing import Optional
import config.config as config
from content.item.base import ContentItem

class DownloadLogger:
    """
    다운로드 작업의 로깅을 담당하는 클래스
    """
    def __init__(self, log_level: int = logging.INFO):
        """
        Args:
            log_level: 로깅 레벨 (기본값: DEBUG)
        """
        self.log_level = log_level
        self.logger = None
        self.log_file = None
        self._setup_logging()

    def _setup_logging(self):
        """
        로깅 설정을 초기화합니다.
        """
        # 로그 디렉토리 생성
        log_dir = os.path.join(config.CONFIG_DIR, 'logs')
        os.makedirs(log_dir, exist_ok=True)
        
        # 로그 파일명 생성 (현재 시간 포함)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.log_file = os.path.join(log_dir, f'download_{timestamp}.log')
        
        # 로거 설정
        self.logger = logging.getLogger('DownloadLogger')
        self.logger.setLevel(self.log_level)
        
        # 기존 핸들러 제거 (중복 로깅 방지)
        for handler in self.logger.handlers[:]:
            self.logger.removeHandler(handler)
        
        # 파일 핸들러 설정
        file_handler = logging.FileHandler(self.log_file, encoding='utf-8')
        file_handler.setLevel(self.log_level)
        
        # 콘솔 핸들러 설정
        console_handler = logging.StreamHandler()
        console_handler.setLevel(self.log_level)
        
        # 포맷터 설정
        formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - [%(threadName)s] - %(message)s'
        )
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)
        
        # 핸들러 추가
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)

    def save_and_close(self):
        """
        로그를 저장하고 로깅을 종료합니다.
        """
        if self.logger:
            # 모든 핸들러 닫기
            for handler in self.logger.handlers[:]:
                handler.close()
                self.logger.removeHandler(handler)
            
            # 로그 파일 경로 로깅
            self.logger.info(f"Log saved to: {self.log_file}")
            
            # 로거 제거
            logging.getLogger('DownloadLogger').handlers = []
            self.logger = None

    def debug(self, message: str):
        """디버그 레벨 로그를 기록합니다."""
        if self.logger:
            self.logger.debug(message)

    def info(self, message: str):
        """정보 레벨 로그를 기록합니다."""
        if self.logger:
            self.logger.info(message)

    def warning(self, message: str):
        """경고 레벨 로그를 기록합니다."""
        if self.logger:
            self.logger.warning(message)

    def error(self, message: str):
        """에러 레벨 로그를 기록합니다."""
        if self.logger:
            self.logger.error(message)

    def critical(self, message: str):
        """치명적 에러 레벨 로그를 기록합니다."""
        if self.logger:
            self.logger.critical(message)
    
    def log_download_info(self, item: ContentItem):
        self.info(f"content_type: {item.content_type}")
        self.info(f"title: {item.title}")
        self.info(f"channel_name: {item.channel_name}")
        self.info(f"created_date: {item.live_open_date}")
        self.info(f"duration: {item.duration}")
        self.info(f"resolution: {item.resolution}")
        self.info(f"total_size: {item.total_size}")
        self.info(f"output_path: {item.output_path}")
        self.info(f"download_path: {item.download_path}")

    def log_download_start(self, total_size: int, part_size: int, segments: int, initial_threads: int):
        """다운로드 시작 정보를 로깅합니다."""
        self.info(f"Download started - Total size: {total_size} bytes")
        self.info(f"Part size: {part_size} bytes")
        self.info(f"Segments: {segments}")
        self.info(f"Initial threads: {initial_threads}")

    def log_thread_start(self, thread_id: int, start: int, end: int):
        """스레드 시작 정보를 로깅합니다."""
        self.debug(f"Thread {thread_id} started - Range: {start}-{end}")

    def log_thread_complete(self, thread_id: int, downloaded_size: int):
        """스레드 완료 정보를 로깅합니다."""
        self.debug(f"Thread {thread_id} completed - Downloaded: {downloaded_size} bytes")

    def log_download_progress(self, total_downloaded: int, total_size: int, speed: float):
        """다운로드 진행 상황을 로깅합니다."""
        progress = (total_downloaded / total_size) * 100
        self.debug(f"Progress: {progress:.2f}% - {total_downloaded}/{total_size} bytes - Speed: {speed:.2f} MB/s")

    def log_download_complete(self, total_time: float):
        """다운로드 완료 정보를 로깅합니다."""
        self.info(f"Download completed in {total_time:.2f} seconds")

    def log_error(self, error_message: str, exception: Optional[Exception] = None):
        """에러 정보를 로깅합니다."""
        if exception:
            self.error(f"{error_message} - Exception: {str(exception)}")
        else:
            self.error(error_message)

    def log_thread_adjust(self, active_threads: int, avg_speed: float):
        """스레드 조정 정보를 로깅합니다."""
        self.info(f"Active threads: {active_threads} - Download speed: {avg_speed:.2f} MB/s")

    def log_thread_debug(self, active_threads: int, download_speed: float, avg_speed: float):
        """스레드 속도 정보를 로깅합니다."""
        self.debug(f"Active threads: {active_threads} - Download speed: {download_speed:.2f} MB/s - Avg speed: {avg_speed:.2f} MB/s")
