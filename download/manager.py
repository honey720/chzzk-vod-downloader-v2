from PySide6.QtCore import QObject, Signal
from .download import DownloadThread
from .download_m3u8 import DownloadM3U8Thread
from .monitor import MonitorThread
from .monitor_m3u8 import MonitorM3U8Thread
from download.data import DownloadData
from content.data import ContentItem
from download.task import DownloadTask
from download.logger import DownloadLogger

class DownloadManager(QObject):
    """
    다운로드 스레드와 속도 모니터링 스레드를 관리하는 클래스.
    """
    progress = Signal(str, str, str, int, object)

    paused = Signal(object)
    resumed = Signal(object)
    stopped = Signal(object)
    finished = Signal(object, str)

    def __init__(self):
        super().__init__()
        self.d_thread = None
        self.m_thread = None
        self.task = None
        self.data = None
        self.item = None
        self.logger = None

    # ============ 일시정지/중지 메서드 ============

    def start(self, item:ContentItem):
        self.item = item
        self.data = DownloadData(item.base_url, item.vod_url, item.output_path, item.resolution, item.content_type)
        self.logger = DownloadLogger()
        self.task = DownloadTask(self.data, self.item, self.logger)
        
        if self.item.content_type == "m3u8":
            self.d_thread = DownloadM3U8Thread(self.task)
            self.m_thread = MonitorM3U8Thread(self.task)
        else:
            self.d_thread = DownloadThread(self.task)
            self.m_thread = MonitorThread(self.task)
        
        self.connectSignal()
        self.task.start()
        self.d_thread.start()
        self.m_thread.start()

    def removeThreads(self):
        if self.d_thread.isRunning():
            self.d_thread.wait()
        self.d_thread = None
        
        if self.m_thread.isRunning():
            self.m_thread.wait()
        self.m_thread = None
        self.task = None
        self.logger = None

    def connectSignal(self):
        self.d_thread.completed.connect(self.finish)
        self.d_thread.stopped.connect(self.stop)
        self.m_thread.progress.connect(self.onProgressFromThread)

    def onProgressFromThread(self, rem, size, spd, prog):
        self.progress.emit(rem, size, spd, prog, self.item)

    def pause(self):
        self.task.pause()
        self.paused.emit(self.item)

    def resume(self):
        self.task.resume()
        self.resumed.emit(self.item)

    def stop(self):
        if self.task is not None:
            self.task.stop()
        self.stopped.emit(self.item)

    def finish(self):
        if self.task is not None:
            self.task.finish()
            self.m_thread.update_progress()
            download_time = self.m_thread.get_download_time()
            self.removeThreads()
            self.finished.emit(self.item, download_time)