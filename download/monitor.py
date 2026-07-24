"""진행 관측 어댑터 — core 엔진의 ProgressEvent를 progress Signal로 중계 (#73).

관측 루프(속도 측정·스레드 조정·주기 통지)는 core FileDownloader가 소유하는
일반 스레드로 흡수됐다. 이 클래스는 다음만 담당한다:
- 엔진의 ProgressEvent 콜백을 기존 progress Signal 형식(남은 시간·크기·속도·%)으로
  변환해 emit (DownloadManager 호출부 무변경)
- manager.finish가 쓰는 update_progress()/get_download_time() 인터페이스 유지
"""

from time import gmtime, strftime

from PySide6.QtCore import QThread, Signal

from core.models.events import ProgressEvent
from download.task import DownloadTask


class MonitorThread(QThread):
    """
    다운로드 진행 상황을 UI로 중계하는 어댑터 스레드 클래스
    """

    progress = Signal(str, str, str, int)

    def __init__(self, task: DownloadTask):
        super().__init__()
        self.task = task
        self.data = self.task.data
        # DownloadThread 어댑터가 먼저 생성되어 task.engine을 채운다 (manager의 생성 순서)
        self.engine = task.engine
        self.engine.set_on_progress(self._relay_progress)

    def run(self):
        """관측 루프는 core 엔진 내부로 이주했다 — QThread는 시작 즉시 종료한다."""

    def _relay_progress(self, event: ProgressEvent):
        """ProgressEvent를 기존 Signal 인자(남은 시간, 크기, 속도 문자열, %)로 변환한다.

        엔진의 관측 스레드에서 호출되므로 Signal emit까지만 수행한다 (스레드 규칙).
        계산식은 구 MonitorThread.update_progress와 동일하다.
        """
        total_size = event.total_size or 0
        speed_mb = event.speed or 0.0

        progress = int((event.downloaded_size / total_size) * 100) if total_size > 0 else 0

        if speed_mb > 0:
            remaining_time = (total_size - event.downloaded_size) / (speed_mb * 1024 * 1024)
            remaining_time_str = strftime("%H:%M:%S", gmtime(remaining_time))
        else:
            remaining_time_str = "N/A"

        # 시그널 전송
        self.progress.emit(
            remaining_time_str, str(event.downloaded_size), f"{speed_mb:.1f} MB/s", progress
        )

    def update_progress(self):
        """(manager.finish 호환) 최종 진행 상태를 집계해 progress Signal로 내보낸다."""
        self.engine.emit_progress()

    def get_download_time(self) -> str:
        """다운로드 소요 시간을 HH:MM:SS 문자열로 반환한다."""
        download_time = self.data.end_time - self.data.start_time
        return strftime("%H:%M:%S", gmtime(download_time))
