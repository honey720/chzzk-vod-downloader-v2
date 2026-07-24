"""m3u8 진행 관측 어댑터 — core 엔진의 ProgressEvent를 progress Signal로 중계 (#74).

관측 루프(속도 측정·해상도별 스레드 조정·주기 통지)는 core M3U8Downloader가
소유하는 일반 스레드로 흡수됐다. 이 클래스는 다음만 담당한다:
- 엔진의 ProgressEvent 콜백을 기존 progress Signal 형식(남은 시간·크기·속도·%)으로
  변환해 emit (DownloadManager 호출부 무변경). m3u8은 전체 크기를 미리 알 수 없어
  진행률·남은 시간을 세그먼트 수 기반으로 계산한다 — 계산식은 구 코드와 동일하다.
- manager.finish가 쓰는 update_progress()/get_download_time() 인터페이스 유지
"""

from time import gmtime, strftime

from PySide6.QtCore import QThread, Signal

from core.models.events import ProgressEvent
from download.task import DownloadTask


class MonitorM3U8Thread(QThread):
    """
    m3u8 다운로드 진행 상황을 UI로 중계하는 어댑터 스레드 클래스
    """

    progress = Signal(str, str, str, int)

    def __init__(self, task: DownloadTask):
        super().__init__()
        self.task = task
        self.data = self.task.data
        # DownloadM3U8Thread 어댑터가 먼저 생성되어 task.engine을 채운다 (manager의 생성 순서)
        self.engine = task.engine
        self.engine.set_on_progress(self._relay_progress)

    def run(self):
        """관측 루프는 core 엔진 내부로 이주했다 — QThread는 시작 즉시 종료한다."""

    def _relay_progress(self, event: ProgressEvent):
        """ProgressEvent를 기존 Signal 인자(남은 시간, 크기, 속도 문자열, %)로 변환한다.

        엔진의 관측 스레드에서 호출되므로 Signal emit까지만 수행한다 (스레드 규칙).
        계산식은 구 MonitorM3U8Thread.update_progress와 동일하다 — 진행률은
        병합 단계(post_process)에서는 병합된 세그먼트 수, 그 전에는 완료된
        세그먼트 수 기반이고, 남은 시간은 평균 세그먼트 크기로 추정한다.
        """
        speed_mb = event.speed or 0.0

        if self.task.item.post_process:
            progress = (
                int((self.data.merged_segments / (self.data.max_threads + 1)) * 100)
                if self.data.max_threads > 0
                else 0
            )
        else:
            progress = (
                int((self.data.completed_threads / self.data.max_threads) * 100)
                if self.data.max_threads > 0
                else 0
            )

        if speed_mb > 0 and self.data.completed_threads > 0:
            avg_segment_size = event.downloaded_size / self.data.completed_threads
            remaining_segments = self.data.max_threads - self.data.completed_threads
            remaining_time = (avg_segment_size * remaining_segments) / (speed_mb * 1024 * 1024)
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
