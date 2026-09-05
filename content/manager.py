"""ContentViewModel의 뷰 바인더 (#169 — Phase 5).

로직(모델 소유·워커 오케스트레이션·다운로드 게이트·배치 체인)은
app/viewmodels/content_viewmodel.py로 이동했다. 이 클래스는 기존 공개
계약(생성자·시그널·메서드·속성)을 그대로 유지한 채 viewmodel에 위임하고,
뷰 방향 시그널을 view.onDownload* 메서드에 연결한다.

이 모듈에 남는 것과 그 이유:
- probe_writable — OS 수준 쓰기 프로브(#137)의 정본. 테스트가
  `from content.manager import probe_writable`로 import하고 monkeypatch
  지점도 이 모듈이다
- ContentWorker 전역 참조(_make_worker) — 테스트의 monkeypatch 지점
- tr() 리터럴(실패 문구) — 번역 컨텍스트("ContentManager")와 lupdate 스캔
  대상(project.json sources)이 이 파일에 걸려 있다
"""

import logging
import os
import tempfile
import threading

from PySide6.QtCore import QObject

from app.viewmodels.content_viewmodel import ContentViewModel
from app.viewmodels.data import ContentItem
from app.widgets.view import ContentListView
from content.worker import ContentWorker

logger = logging.getLogger(__name__)

# 쓰기 프로브 대기 상한(초) (#137). 정상 디스크에서 프로브는 밀리초 수준이라
# 이 값은 병리 상황(무응답 마운트 — #136)에서만 발동한다. 발동 시 메인 스레드가
# 이 시간만큼 기다리는 대가가 있지만, 무한 정지(0B 침묵) 대신 유한 대기 후
# 명확한 실패가 목적이다
_WRITE_PROBE_TIMEOUT_S = 5.0


def probe_writable(directory: str, timeout_s: float = _WRITE_PROBE_TIMEOUT_S) -> tuple[bool, str]:
    """저장 경로의 존재·쓰기 가능 여부를 제물 스레드로 검사한다 (#137 — #136 제안 ②).

    존재 검사(os.path.isdir)조차 무응답 마운트에서는 매달릴 수 있어, 검사
    전체를 별도 스레드에서 수행하고 join(timeout)으로 포기한다 — 파이썬에
    파일 I/O 시간 제한 수단이 없다는 조사(#136)의 상한 적용이다. 갇힌
    스레드는 회수할 수 없지만(데몬), 프로브는 다운로드 시작 시점 1회뿐인
    작고 드문 지점이라 누수 비용이 유계다.

    Returns:
        (쓰기 가능 여부, 사유): 사유는 "" | "missing" | "denied" | "timeout"
    """
    outcome: dict[str, str] = {}

    def probe() -> None:
        try:
            if not os.path.isdir(directory):
                outcome["reason"] = "missing"
                return
            # 실제 파일 생성·삭제로 확인한다 — os.access는 네트워크 파일시스템의
            # 권한(예: SFTP 상 ZFS 풀 루트)을 신뢰할 수 없다
            fd, probe_path = tempfile.mkstemp(prefix=".cvdv2_probe_", dir=directory)
            os.close(fd)
            os.remove(probe_path)
            outcome["reason"] = ""
        except OSError:
            outcome["reason"] = "denied"

    worker = threading.Thread(target=probe, daemon=True, name="WriteProbe")
    worker.start()
    worker.join(timeout_s)
    reason = outcome.get("reason", "timeout")
    return reason == "", reason


def _make_worker(vod_url: str, cookies: dict, downloadPath: str):
    """모듈 전역 ContentWorker를 조회해 생성한다 — 테스트의 monkeypatch 지점."""
    return ContentWorker(vod_url, cookies, downloadPath)


class ContentManager(QObject):
    def __init__(self, view: ContentListView, parent=None):
        super().__init__(parent)
        self._vm = ContentViewModel(
            worker_factory=_make_worker,
            probe=lambda directory: probe_writable(directory),
            messages={
                "invalid_path": self._invalidPathMessage,
                "save_failed": self._saveFailedMessage,
            },
            parent=self,
        )
        # 바운드 시그널 재노출 — download/manager.py 파사드와 같은 방식.
        # 기존 소비자(mainWindow·view·테스트)의 connect가 무변경으로 동작한다
        self.downloadRequested = self._vm.downloadRequested
        self.stopRequested = self._vm.stopRequested
        self.insertItemRequested = self._vm.insertItemRequested
        self.deleteItemRequested = self._vm.deleteItemRequested
        self.finishedRequested = self._vm.finishedRequested
        self.finishedAllRequested = self._vm.finishedAllRequested
        self.fetchRequested = self._vm.fetchRequested
        self.contentError = self._vm.contentError

        self.view = view
        self.view.setModel(self.model)
        self.view.deleteRequest.connect(self.removeItem)
        self.view.fetchRequested.connect(self.fetchReuest)

        # 구 view 직접 호출 6곳의 반전 (#169): viewmodel은 시그널로 말하고,
        # 뷰 연결은 바인더가 담당한다
        self._vm.itemStarted.connect(self.view.onDownloadStarted)
        self._vm.itemStopped.connect(self.view.onDownloadStoped)
        self._vm.itemPaused.connect(self.view.onDownloadPaused)
        self._vm.itemResumed.connect(self.view.onDownloadResumed)
        self._vm.itemFinished.connect(self.view.onDownloadFinished)

    # ---- 실패 문구 — tr() 리터럴은 이 클래스에 남긴다 (컨텍스트·lupdate) ----

    def _invalidPathMessage(self) -> str:
        return self.tr("Invalid file path")

    def _saveFailedMessage(self) -> str:
        # 첫 줄=핵심 / 둘째 줄=상세 규약(#245, download/qt_bridge.py 참고) —
        # 다운로드 브리지의 같은 사유와 문구를 맞춘다
        return self.tr(
            "Failed to save file · check the path and disk space\n"
            "The file could not be saved. Check the download path and free disk space."
        )

    # ---- viewmodel 상태 재노출 ----

    @property
    def model(self):
        return self._vm.model

    @property
    def threadpool(self):
        return self._vm.threadpool

    @property
    def downloadPath(self):
        return self._vm.downloadPath

    @downloadPath.setter
    def downloadPath(self, value):
        self._vm.downloadPath = value

    @property
    def _pendingPlaceholders(self):
        return self._vm._pendingPlaceholders

    # ---- 공개 API 위임 ----

    def fetchReuest(self, urls):
        self.fetchRequested.emit(urls)

    def fetchContent(self, vod_url: str, cookies: dict, downloadPath: str) -> None:
        self._vm.fetchContent(vod_url, cookies, downloadPath)

    def clrearFinishedItems(self):
        self._vm.clrearFinishedItems()

    def removeItem(self, item: ContentItem):
        self._vm.removeItem(item)

    def downloadItem(self):
        self._vm.downloadItem()

    def onDownload(self, item: ContentItem):
        self._vm.onDownload(item)

    def update_progress(self, rem, size, spd, prog, item: ContentItem):
        self._vm.update_progress(rem, size, spd, prog, item)

    def start(self, item):
        self._vm.start(item)

    def stop(self, item):
        self._vm.stop(item)

    def pause(self, item):
        self._vm.pause(item)

    def resume(self, item):
        self._vm.resume(item)

    def finish(self, item: ContentItem, download_time):
        self._vm.finish(item, download_time)

    def fail(self, item: ContentItem, message: str = ""):
        self._vm.fail(item, message)

    def emitStopRequested(self, item: ContentItem):
        self._vm.emitStopRequested(item)

    def emitFinishedRequest(self, item: ContentItem):
        self._vm.emitFinishedRequest(item)

    def findItem(self):
        return self._vm.findItem()

    def hasLoadingItems(self):
        return self._vm.hasLoadingItems()

    def downloadResultCounts(self) -> tuple[int, int]:
        return self._vm.downloadResultCounts()
