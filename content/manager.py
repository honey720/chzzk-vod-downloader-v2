import os, re, platform

from PySide6.QtCore import Qt, Signal, QThreadPool, QObject
from content.model import ContentListModel
from content.view import ContentListView
from content.delegate import ContentListDelegate
from content.data import ContentItem
from download.state import DownloadState
from content.worker import ContentWorker

class ContentManager(QObject):
    # 메타데이터 매니저 UI
    downloadRequested = Signal(object)
    stopRequested = Signal(object)
    insertItemRequested = Signal(int)
    deleteItemRequested = Signal(object, int)
    finishedRequested = Signal(object)
    finishedAllRequested = Signal()
    fetchRequested = Signal(str)
    contentError = Signal(str)  # ✅ UI에서 오류를 처리할 수 있도록 signal 추가

    def __init__(self, view: ContentListView, parent = None):
        super().__init__(parent)
        
        self.view = view
        self.model = ContentListModel()
        self.view.setModel(self.model)
        self.view.setItemDelegate(ContentListDelegate())
        self.view.deleteRequest.connect(self.removeItem)
        self.view.fetchRequested.connect(self.fetchReuest)

        self.downloadPath = ""
        self.threadpool = QThreadPool()

    def fetchReuest(self, urls):
        self.fetchRequested.emit(urls)

    def fetchContent(self, vod_url: str, cookies: dict, downloadPath: str) -> None:
        worker = ContentWorker(vod_url, cookies, downloadPath)

        # 시그널/슬롯 연결
        worker.finished.connect(self.onWorkerFinished)  # 결과 처리 슬롯
        worker.error.connect(self.onWorkerError)           # 에러 처리 슬롯
        worker.finished.connect(worker.deleteLater)

        self.threadpool.start(lambda: worker.run())

    def onWorkerFinished(self, result, content_type):
        # result는 (vod_url, metadata, unique_reps, resolution, base_url, downloadPath, liveRewindPlaybackJson) 형식
        vod_url, metadata, unique_reps, resolution, base_url, downloadPath, liveRewindPlaybackJson = result
        self.downloadPath = downloadPath
        self.addItem(vod_url, metadata, unique_reps, resolution, base_url, downloadPath, content_type, liveRewindPlaybackJson)

    def onWorkerError(self, error_message):
        self.contentError.emit(error_message)

    def addItem(self, vod_url, metadata, unique_reps, resolution, base_url, downloadPath, content_type, liveRewindPlaybackJson):
        item = ContentItem(vod_url, metadata, unique_reps, resolution, base_url, downloadPath, content_type, liveRewindPlaybackJson)
        self.model.addItem(item)
        row = self.model.rowCount()
        self.insertItemRequested.emit(row)

    def clrearFinishedItems(self):
        if not self.model.isEmpty():
            for row in reversed(range(self.model.rowCount())):
                index = self.model.index(row, 0)
                item: ContentItem = self.model.data(index, Qt.ItemDataRole.UserRole)
                # 아이템이 완료 상태이면 삭제
                if item.downloadState == DownloadState.FINISHED:
                    self.removeItem(item)


    def removeItem(self, item: ContentItem):
        row = self.model.getRow(item)  # ✅ 객체의 row 찾기
        if row is not None:
            self.model.removeRows(row, 1)  # ✅ 올바른 삭제 요청
            index = self.model.rowCount()
            self.deleteItemRequested.emit(item, index)

    def downloadItem(self):
        found, item, index = self.findItem()
        if found:
            try:
                if not os.path.exists(item.download_path):
                    raise ValueError(self.tr("Invalid file path"))
                self.onDownload(item)
            except Exception as e:
                item.stateMessage = self.tr("Error occurred: {e}").format(e=e)
                self.model.dataChanged.emit(index, index)
                self.fail(item)
        else:
            self.finishedAllRequested.emit()

    def onDownload(self, item: ContentItem):
        """
        해상도 버튼 클릭 시 다운로드 진행.
        """
        if item:
            title = item.title
            resolution = item.resolution
            default_filename = f"{title} {resolution}p.mp4"
        else:
            default_filename = "video.mp4"

        if platform.system() == "Windows":
            item.output_path = item.download_path + '\\' + default_filename

        elif platform.system() == "Linux":
            item.output_path = item.download_path + '/' + default_filename

        if item.output_path:
            # 다운로드 요청 시그널 발행
            self.downloadRequested.emit(item)

    def update_progress(self, rem, size, spd, prog, item: ContentItem):
        item.download_remain_time = rem
        item.download_size = size
        item.download_speed = spd
        item.download_progress = prog
        
        row = self.model.getRow(item)
        index = self.model.index(row, 0)
        self.model.dataChanged.emit(index, index)

    def start(self, item):
        self.view.onDownloadStarted(item)

    def stop(self, item):
        self.view.onDownloadStoped(item)

    def pause(self, item):
        self.view.onDownloadPaused(item)

    def resume(self, item):
        self.view.onDownloadResumed(item)

    def finish(self, item: ContentItem, download_time):
        item.download_time = download_time
        self.view.onDownloadFinished(item, True)
        self.emitFinishedRequest(item)
    
    def fail(self, item: ContentItem):
        item.downloadState = DownloadState.FAILED
        self.view.onDownloadFinished(item, False)
        self.emitFinishedRequest(item)

    def emitStopRequested(self, item: ContentItem):
        self.stopRequested.emit(item)
    
    def emitFinishedRequest(self, item: ContentItem):
        self.finishedRequested.emit(item)
        self.downloadItem()

    def findItem(self):
        row_count = self.model.rowCount()
        for row in range(row_count):
            index = self.model.index(row, 0)
            item: ContentItem = self.model.data(index, Qt.ItemDataRole.UserRole)
            if not item.downloadState in [DownloadState.FINISHED, DownloadState.FAILED]:
                return True, item, index
        return False, None, None