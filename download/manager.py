from PySide6.QtCore import QObject, Signal
from .download import DownloadThread
from .monitor import MonitorThread
from download.data import DownloadData
from metadata.data import MetadataItem
from download.task import DownloadTask

class DownloadManager(QObject):
    """
    다운로드 스레드와 속도 모니터링 스레드를 관리하는 클래스.
    """
    progress = Signal(str, str, str, int, object)

    paused = Signal(object)
    resumed = Signal(object)
    stopped = Signal(object, str)
    finished = Signal(object)

    update_threads = Signal(int, int, int, int)
    update_time = Signal(str, str)
    update_active_threads = Signal(int)
    update_avg_speed = Signal(float)

    def __init__(self):
        super().__init__()
        self.d_thread = None
        self.m_thread = None
        self.task = None
        self.data = None
        self.item = None

    # ============ 일시정지/중지 메서드 ============

    def start(self, item:MetadataItem):
        self.data = DownloadData(item.base_url, item.output_path, item.height)
        self.item = item
        self.task = DownloadTask(self.data, self.item)
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

    def stop(self, message: str):
        self.task.stop(message)
        self.stopped.emit(self.item, message)

    def finish(self):
        self.task.finish()
        self.m_thread.update_progress()
        self.removeThreads()
        self.finished.emit(self.item)