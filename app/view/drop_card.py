from PySide6.QtGui import QDragEnterEvent, QDropEvent
from qfluentwidgets import CardWidget


class DropCard(CardWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setMinimumHeight(200)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            self._setHighlight(True)
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self._setHighlight(False)

    def dropEvent(self, event: QDropEvent):
        self._setHighlight(False)
        url = event.mimeData().urls()
        files = [u.toLocalFile() for u in event.mimeData().urls()]
        print(url)

    def _setHighlight(self, on: bool):
        self.setProperty("dragging", on)
        self.style().polish(self)
