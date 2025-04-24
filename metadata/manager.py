import os, re, platform

from PySide6.QtWidgets import QWidget, QVBoxLayout
from PySide6.QtCore import Qt, Signal, QThreadPool
from metadata.model import MetadataListModel
from metadata.view import MetadataListView
from metadata.delegate import MetadataListDelegate
from metadata.data import MetadataItem
from download.state import DownloadState
from metadata.worker import MetadataWorker

class MetadataManager(QWidget):
    # 메타데이터 매니저 UI
    downloadRequested = Signal(object)
    stopRequested = Signal(object)
    insertItemRequested = Signal(int)
    deleteItemRequested = Signal(object, int)
    finishedRequested = Signal(object)
    finishedAllRequested = Signal()
    fetchRequested = Signal(str)
    metadataError = Signal(str)  # ✅ UI에서 오류를 처리할 수 있도록 signal 추가

    def __init__(self, parent = None):
        super().__init__(parent)
        self.initUI()
        self.downloadPath = ""
        self.threadpool = QThreadPool()

    def initUI(self):
        """UI 초기화 및 View 설정"""
        self.metadataLayout = QVBoxLayout(self)

        self.model = MetadataListModel()
        
        self.view = MetadataListView()
        self.view.setModel(self.model)
        self.view.setItemDelegate(MetadataListDelegate())

        self.view.deleteRequest.connect(self.removeItem)
        self.view.fetchRequested.connect(self.fetchReuest)

        self.metadataLayout.addWidget(self.view)
        self.setLayout(self.metadataLayout)

    def fetchReuest(self, urls):
        self.fetchRequested.emit(urls)

    def fetchMetadata(self, vod_url: str, cookies: dict, downloadPath: str) -> None:
        worker = MetadataWorker(vod_url, cookies, downloadPath)

        # 시그널/슬롯 연결
        worker.finished.connect(self.onWorkerFinished)  # 결과 처리 슬롯
        worker.error.connect(self.onWorkerError)           # 에러 처리 슬롯
        worker.finished.connect(worker.deleteLater)

        self.threadpool.start(lambda: worker.run())

    def onWorkerFinished(self, result, content_type):
        # result는 (vod_url, metadata, unique_reps, resolution, base_url, downloadPath) 형식
        vod_url, metadata, unique_reps, resolution, base_url, downloadPath = result
        self.downloadPath = downloadPath
        self.addItem(vod_url, metadata, unique_reps, resolution, base_url, downloadPath, content_type)

    def onWorkerError(self, error_message):
        self.metadataError.emit(error_message)

    def addItem(self, vod_url, metadata, unique_reps, resolution, base_url, downloadPath, content_type):
        item = MetadataItem(vod_url, metadata, unique_reps, resolution, base_url, downloadPath, content_type)
        self.model.addItem(item)
        row = self.model.rowCount()
        self.insertItemRequested.emit(row)

    def clrearFinishedItems(self):
        if not self.model.isEmpty():
            for row in reversed(range(self.model.rowCount())):
                index = self.model.index(row, 0)
                item: MetadataItem = self.model.data(index, Qt.ItemDataRole.UserRole)
                # 아이템이 완료 상태이면 삭제
                if item.downloadState == DownloadState.FINISHED:
                    self.removeItem(item)


    def removeItem(self, item: MetadataItem):
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

    def onDownload(self, item: MetadataItem):
        """
        해상도 버튼 클릭 시 다운로드 진행.
        """
        if item:
            title = item.title
            resolution = item.resolution
            # 특수 문자 제거
            title = re.sub(r'[\\/:\*\?"<>|\n]', '', title)
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

    def update_progress(self, rem, size, spd, prog, item: MetadataItem):
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

    def finish(self, item: MetadataItem, download_time):
        item.download_time = download_time
        self.view.onDownloadFinished(item, True)
        self.emitFinishedRequest(item)
    
    def fail(self, item: MetadataItem):
        item.downloadState = DownloadState.FAILED
        self.view.onDownloadFinished(item, False)
        self.emitFinishedRequest(item)

    def emitStopRequested(self, item: MetadataItem):
        self.stopRequested.emit(item)
    
    def emitFinishedRequest(self, item: MetadataItem):
        self.finishedRequested.emit(item)
        self.downloadItem()

    def findItem(self):
        row_count = self.model.rowCount()
        for row in range(row_count):
            index = self.model.index(row, 0)
            item: MetadataItem = self.model.data(index, Qt.ItemDataRole.UserRole)
            if not item.downloadState in [DownloadState.FINISHED, DownloadState.FAILED]:
                return True, item, index
        return False, None, None