"""다운로드 viewmodel — QtDownloadBridge를 소유하고 content 쪽에 직결한다 (#170).

구 DownloadManager 파사드(1줄 위임 + 레거시 별칭 d_thread/m_thread)와
mainWindow의 순수 릴레이 슬롯 6개(_onProgress~_onFailed)를 흡수했다.
브리지의 Signal을 content 반영 메서드에 직접 연결하므로 mainWindow는 더
이상 다운로드 이벤트의 중계자가 아니다.

"유일한 Signal emit 지점은 qt_bridge" 불변식(#72~#75)은 그대로다 — 이
클래스는 Signal을 선언하지 않고, 브리지의 모듈 경로·계약도 무접촉이다.
"""

from PySide6.QtCore import QObject

from content.data import ContentItem
from download.qt_bridge import QtDownloadBridge


class DownloadViewModel(QObject):
    def __init__(self, content, parent=None):
        """content: 다운로드 이벤트를 반영할 상대 — update_progress/pause/resume/
        stop/finish/fail을 가진 객체(ContentManager 바인더)를 받는다."""
        super().__init__(parent)
        self._bridge = QtDownloadBridge()
        # 구 mainWindow.setupThreadSignals의 다운로드 릴레이 6개 — 위임 없이 직결
        self._bridge.progress.connect(content.update_progress)
        self._bridge.paused.connect(content.pause)
        self._bridge.resumed.connect(content.resume)
        self._bridge.stopped.connect(content.stop)
        self._bridge.finished.connect(content.finish)
        self._bridge.failed.connect(content.fail)

    def isDownloading(self) -> bool:
        """활성 다운로드 핸들 존재 여부 — 구 d_thread/m_thread truthiness 폴링 대체."""
        return self._bridge.handle is not None

    @property
    def task(self):
        """현재 다운로드 태스크 어댑터 (없으면 None) — 중지 확인 분기용."""
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
