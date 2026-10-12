"""카드 목록의 영구 저장과 시작 시 대기 상태 복원을 연결한다."""

from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

import config.config as config
from app.queue_store import QueueStore, encode_item
from core.models.download_state import DownloadState


class QueueViewModel(QObject):
    """다운로드를 시작하지 않고 목록의 변경만 저장한다."""

    warning = Signal(str)

    def __init__(self, content, parent=None):
        super().__init__(parent)
        self.content = content
        self.store = QueueStore(Path(config.CONFIG_DIR))
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.save)
        self._snapshot = []
        self.available = True
        try:
            for item in self.store.load():
                content.model.addItem(item)
            self._snapshot = self._records()
        except OSError:
            self.available = False
            self.store.warning = "다른 앱이 대기열을 사용 중이거나 목록을 읽을 수 없습니다."
        content.model.itemInserted.connect(self.changed)
        content.model.itemRemoved.connect(self.changed)
        content.model.itemChanged.connect(self.changed)
        store = self.store
        self.destroyed.connect(lambda: store.close())

    def _records(self) -> list[dict]:
        return [
            encode_item(item)
            for item in self.content.model.items
            if item.downloadState != DownloadState.FINISHED
        ]

    def changed(self, _item=None) -> None:
        """속도·진행률 변화는 제외하고 영구 정보가 달라질 때만 저장한다."""
        if not self.available:
            return
        records = self._records()
        if records != self._snapshot:
            self._snapshot = records
            self.timer.start()

    def save(self) -> None:
        """현재 미완료 목록을 저장하고 오류는 화면에 알린다."""
        if not self.available:
            return
        self.timer.stop()
        try:
            self.store.save(self._records())
        except OSError:
            self.warning.emit("대기열 저장에 실패했습니다. 저장 공간과 권한을 확인해 주세요.")

    def close(self) -> None:
        """대기 중인 저장을 마친 뒤 잠금을 해제한다."""
        self.save()
        self.store.close()
        self.available = False
