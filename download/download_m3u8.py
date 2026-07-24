"""m3u8 다운로드 워커 — core M3U8Downloader를 실행하고 Signal로 중계하는 얇은 어댑터 (#74).

다운로드·병합 실행 로직은 core/downloaders/m3u8_downloader.py로 이동했다.
이 클래스는 다음만 담당한다:
- base_url 해석: 쿠키 로드·치지직 API 조회(NetworkManager)는 네트워크 계층이
  아직 앱 영역이므로 어댑터가 run() 시작 시 수행한다 (#72와 같은 이유)
- QThread에서 엔진 run()을 실행 (DownloadManager 호출부 무변경)
- 엔진의 완료·실패·병합 시작 콜백을 기존 completed/stopped Signal과
  item.post_process 플래그로 변환
- 실패 메시지 번역(tr) — i18n 키 "Download failed"는 여기서 유지한다
"""

import threading

from PySide6.QtCore import QThread, Signal

import config.config as config
from content.network import NetworkManager
from core.downloaders.m3u8_downloader import M3U8Downloader
from core.models.download_state import DownloadState
from download.task import DownloadTask


class DownloadM3U8Thread(QThread):
    """
    m3u8 파일을 multi-thread로 다운로드하는 작업 스레드 클래스 (core 엔진 어댑터)
    """

    completed = Signal()
    stopped = Signal(str)

    def __init__(self, task: DownloadTask):
        super().__init__()
        self.task = task
        self.engine = M3U8Downloader(
            data=task.data,
            logger=task.logger,
            on_finished=self._relay_finished,
            on_failed=self._relay_failed,
            on_merge_start=self._relay_merge_start,
        )
        # MonitorM3U8Thread 어댑터가 진행 콜백을 연결할 수 있도록 태스크에 공유한다 (#73)
        task.engine = self.engine

    def run(self):
        """QThread 진입점 — base_url을 해석한 뒤 core 엔진의 파이프라인을 실행한다."""
        threading.current_thread().name = "DownloadM3U8Thread"  # 스레드 시작 시 이름 재설정
        try:
            self._resolve_base_url()
        except Exception as e:
            # 조회 실패는 엔진 실패와 같은 형식으로 보고한다 (구 코드의 단일 except와 동일)
            self._relay_failed(e)
            self.task.logger.log_exception("Download failed", e)
            self.task.logger.save_and_close()
            return
        self.engine.run()
        # 사용자가 강제로 중단한 경우 병합 표시 해제 (구 코드의 WAITING 정리와 동일)
        if self.task.state == DownloadState.WAITING:
            self.task.item.post_process = False

    def _resolve_base_url(self):
        """쿠키를 읽고 치지직 API로 선택 해상도의 m3u8 플레이리스트 URL을 해석한다."""
        data = config.load_config().get("cookies", {})
        cookies = {
            "NID_AUT": data.get("NID_AUT", ""),
            "NID_SES": data.get("NID_SES", ""),
        }
        s = self.task.data
        content_type, content_no = NetworkManager.extract_content_no(s.vod_url)
        info = NetworkManager.get_video_info(content_no, cookies)
        s.base_url = NetworkManager.get_video_m3u8_base_url(
            info.live_rewind_playback_json, s.resolution, cookies
        )

    def _relay_finished(self):
        """엔진 완료 콜백 → completed Signal (emit은 스레드 세이프)."""
        self.completed.emit()

    def _relay_failed(self, exc: BaseException):
        """엔진 실패 콜백 → stopped Signal. 병합 표시 해제와 메시지 번역은 여기서 한다."""
        self.task.item.post_process = False
        self.stopped.emit(self.tr("Download failed"))

    def _relay_merge_start(self):
        """엔진 병합 시작 콜백 → UI 병합 단계 플래그 (구 코드의 post_process = True)."""
        self.task.item.post_process = True
