"""다운로드 매니저 파사드 — 실행은 core DownloadService, 배선은 QtDownloadBridge (#75).

태스크 큐·동시 실행 수 제한·다운로더 선택·완료 처리는
core/services/download_service.py로 이주했고, 콜백→Signal 변환은
download/qt_bridge.py가 단독으로 담당한다. 이 클래스는 기존 UI 호출부
(application/mainWindow.py)의 인터페이스를 유지하는 얇은 파사드다:

- Signal(progress/paused/resumed/stopped/finished)은 브리지의 것을 그대로
  노출한다 — emit은 qt_bridge 모듈만 수행하며 이 파일에는 Signal이 없다.
- d_thread/m_thread/task는 활성 다운로드 존재 여부 판정(truthiness)에 쓰이는
  기존 속성명이다. 스레드 소유가 서비스로 넘어갔으므로 현재 핸들/태스크를
  돌려준다 (호출부 무변경).
"""

from content.data import ContentItem
from download.qt_bridge import QtDownloadBridge


class DownloadManager:
    """다운로드 시작·제어를 UI에 제공하는 파사드 (구 인터페이스 유지)."""

    def __init__(self):
        self._bridge = QtDownloadBridge()
        # 기존 시그널 인터페이스 노출 — 바운드 시그널이므로 connect는 그대로 동작한다
        self.progress = self._bridge.progress
        self.paused = self._bridge.paused
        self.resumed = self._bridge.resumed
        self.stopped = self._bridge.stopped
        self.finished = self._bridge.finished

    @property
    def d_thread(self):
        """활성 다운로드 핸들 (기존 QThread 속성명 호환 — 존재 여부 판정용)."""
        return self._bridge.handle

    @property
    def m_thread(self):
        """활성 다운로드 핸들 (기존 QThread 속성명 호환 — 존재 여부 판정용)."""
        return self._bridge.handle

    @property
    def task(self):
        """현재 다운로드 태스크 어댑터 (없으면 None)."""
        return self._bridge.task

    def start(self, item: ContentItem) -> None:
        """다운로드를 시작한다."""
        self._bridge.start(item)

    def pause(self) -> None:
        """다운로드를 일시정지한다."""
        self._bridge.pause()

    def resume(self) -> None:
        """다운로드를 재개한다."""
        self._bridge.resume()

    def stop(self) -> None:
        """다운로드를 중지한다."""
        self._bridge.stop()

    def removeThreads(self) -> None:
        """실행 중인 워커가 끝나기를 기다린 뒤 참조를 정리한다."""
        self._bridge.removeThreads()
