"""파일 다운로드 워커 — core FileDownloader를 실행하고 Signal로 중계하는 얇은 어댑터 (#73).

다운로드 실행·관측 로직은 core/downloaders/file_downloader.py로 이동했다.
이 클래스는 다음만 담당한다:
- QThread에서 엔진 run()을 실행 (DownloadManager 호출부 무변경)
- 엔진의 완료·실패 콜백을 기존 completed/stopped Signal로 변환
- 실패 메시지 번역(tr) — i18n 키 "Download failed"는 여기서 유지한다
"""

import threading

from PySide6.QtCore import QThread, Signal

from core.downloaders.file_downloader import FileDownloader
from download.task import DownloadTask

# 구현은 core/downloaders/ranges.py로 이동했다 (#50). 기존 호출부 호환용 re-export.
from core.downloaders.ranges import decide_part_size, split_ranges  # noqa: F401


class DownloadThread(QThread):
    """
    VOD 파일을 multi-thread로 다운로드하는 작업 스레드 클래스 (core 엔진 어댑터)
    """

    completed = Signal()
    stopped = Signal(str)

    def __init__(self, task: DownloadTask):
        super().__init__()
        self.task = task
        self.engine = FileDownloader(
            data=task.data,
            logger=task.logger,
            on_finished=self._relay_finished,
            on_failed=self._relay_failed,
        )
        # MonitorThread 어댑터가 진행 콜백을 연결할 수 있도록 태스크에 공유한다 (#73)
        task.engine = self.engine

    def run(self):
        """QThread 진입점 — core 엔진의 다운로드 파이프라인을 실행한다."""
        threading.current_thread().name = "DownloadThread"  # 스레드 시작 시 이름 재설정
        self.engine.run()

    def _relay_finished(self):
        """엔진 완료 콜백 → completed Signal (emit은 스레드 세이프)."""
        self.completed.emit()

    def _relay_failed(self, exc: BaseException):
        """엔진 실패 콜백 → stopped Signal. 사용자 메시지 번역은 여기서 한다."""
        self.stopped.emit(self.tr("Download failed"))
