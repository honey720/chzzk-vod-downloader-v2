"""DownloadTask 어댑터 — 순수 상태 머신은 core/models/download_task.py로 이주 (#60).

기존 호출부(download manager·worker·monitor)의 인터페이스(state 속성, lock,
start/pause/resume/stop/finish/isRunning)를 그대로 유지하면서, 상태 보유·전이는
core의 DownloadTaskModel에 위임한다. Qt 계층과의 연결(ContentItem 상태 반영,
다운로드 로깅)은 이 어댑터에 남는다.
"""

import threading

from content.data import ContentItem
from core.models.download_state import DownloadState
from core.models.download_task import InvalidStateTransitionError
from download.data import DownloadData
from download.logger import DownloadLogger


class DownloadTask:
    """core 상태 머신을 감싸 기존 다운로드 엔진과 UI 계층을 연결하는 어댑터."""

    def __init__(self, data: DownloadData, item: ContentItem, logger: DownloadLogger):
        self.data = data
        self.item = item
        self.logger = logger
        # 엔진(worker/monitor)이 future_count 등 공유 변수 동기화에 쓰는 락.
        # 모델 내부의 상태 전이 락과는 별개다 (엔진 무변경 유지 — Phase 3에서 정리).
        self.lock = threading.Lock()
        # 상태·진행률의 소유처는 DownloadData가 가진 core 모델 하나다 (#61).
        # 별도 모델을 만들지 않고 같은 모델에 UI 반영 콜백만 연결한다
        self.model = data.model
        self.model.set_on_state_change(item.setDownloadState)
        # core 다운로드 엔진(FileDownloader) 공유 슬롯 (#73).
        # DownloadThread 어댑터가 생성 시 채우고, MonitorThread 어댑터가
        # 진행 콜백을 연결할 때 읽는다 (manager의 생성 순서가 이를 보장한다)
        self.engine = None

    @property
    def state(self) -> DownloadState:
        """현재 다운로드 상태 (core 모델에 위임)."""
        return self.model.state

    def _try_transition(self, transition_name: str) -> bool:
        """모델의 전이 메서드를 호출하되, 허용되지 않는 전이는 크래시 대신 경고 로그로 남긴다.

        기존 코드는 어떤 상태에서든 전이를 허용했으므로, UI 이벤트 타이밍 문제로
        어긋난 호출이 와도 앱이 죽지 않도록 어댑터에서 흡수한다.
        """
        try:
            return getattr(self.model, transition_name)()
        except InvalidStateTransitionError as e:
            self.logger.warning(f"상태 전이 무시: {e}")
            return False

    def start(self):
        """다운로드 시작. 성공 시 다운로드 정보를 로그로 남긴다."""
        if self._try_transition("start"):
            self.logger.log_download_info(self.item)

    def pause(self):
        """다운로드 일시정지."""
        self._try_transition("pause")

    def resume(self):
        """다운로드 재개."""
        self._try_transition("resume")

    def stop(self):
        """다운로드 취소(대기 상태로 복귀)."""
        self._try_transition("stop")

    def finish(self):
        """다운로드 완료 처리."""
        self._try_transition("finish")

    def isRunning(self) -> bool:
        """현재 다운로드가 실행 중인지 여부 (기존 메서드명 유지)."""
        return self.model.is_running()
