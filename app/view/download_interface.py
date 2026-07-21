# coding: utf-8
from PySide6.QtWidgets import QWidget, QHBoxLayout, QListWidgetItem
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QPainter, QColor, QFont

from qfluentwidgets import isDarkTheme

from qfluentwidgets import (LineEdit, PrimaryPushButton, PushButton,
                            CaptionLabel, FluentIcon as FIF, ListWidget)

from .base_interface import BaseInterface
from ..components.download_card import DownloadCardWidget


class UrlBar(QWidget):
    """URL 입력 + Fetch 버튼"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.urlEdit = LineEdit(self)
        self.fetchButton = PrimaryPushButton(self.tr('Fetch'), self, FIF.SEARCH)

        self.__initWidget()
        self.__initLayout()

    def __initWidget(self):
        self.urlEdit.setPlaceholderText(self.tr('Enter VOD / Clip URL...'))
        self.urlEdit.setClearButtonEnabled(True)

    def __initLayout(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.urlEdit, 1)
        layout.addWidget(self.fetchButton)


class CardListWidget(ListWidget):
    """VOD 카드 리스트

    - 내부 드래그: 카드 순서 변경 (InternalMove)
    - 외부 드롭: URL 텍스트를 받아 urlDropped 시그널 발행
    """

    urlDropped = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._isExternalDrag = False
        self.__initWidget()

    def __initWidget(self):
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDragDropMode(ListWidget.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setSelectionMode(ListWidget.SelectionMode.NoSelection)
        self.setSpacing(4)

    # ──────────────────────────── 카드 관리 API ────────────────────────────

    def addCard(self, card: DownloadCardWidget) -> QListWidgetItem:
        """카드를 리스트 맨 아래에 추가하고 QListWidgetItem 반환"""
        item = QListWidgetItem(self)
        item.setSizeHint(QSize(self.viewport().width(), 116))
        self.addItem(item)
        self.setItemWidget(item, card)
        return item

    def removeCard(self, item: QListWidgetItem):
        self.takeItem(self.row(item))

    def clearFinished(self):
        """FINISHED / ERROR 상태 카드 일괄 제거"""
        # TODO: card에 _state 속성 저장 후 실제 상태 확인
        # from ..common.download_state import DownloadState
        # finished = {DownloadState.FINISHED, DownloadState.ERROR}
        # for row in range(self.count() - 1, -1, -1):
        #     card: DownloadCardWidget = self.itemWidget(self.item(row))
        #     if card._state in finished:
        #         self.takeItem(row)
        pass

    # ──────────────────────────── 드래그 앤 드롭 ────────────────────────────

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.source() is self:
            super().dragEnterEvent(event)
        elif event.mimeData().hasText():
            self._isExternalDrag = True
            self.viewport().update()
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.source() is self:
            super().dragMoveEvent(event)
        elif event.mimeData().hasText():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._isExternalDrag = False
        self.viewport().update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent):
        self._isExternalDrag = False
        self.viewport().update()
        if event.source() is self:
            super().dropEvent(event)
        elif event.mimeData().hasText():
            url = event.mimeData().text().strip()
            if url:
                self.urlDropped.emit(url)
            event.acceptProposedAction()
        else:
            event.ignore()

    def paintEvent(self, event):
        super().paintEvent(event)

        if self._isExternalDrag:
            painter = QPainter(self.viewport())
            painter.setRenderHint(QPainter.Antialiasing)
            painter.fillRect(self.viewport().rect(), QColor(0, 0, 0, 80))
            painter.setPen(QColor(255, 255, 255, 220))
            font = QFont(self.font())
            font.setPointSize(13)
            painter.setFont(font)
            painter.drawText(
                self.viewport().rect(),
                Qt.AlignCenter,
                self.tr('Drop URL here')
            )
            painter.end()

        elif self.count() == 0:
            painter = QPainter(self.viewport())
            painter.setRenderHint(QPainter.Antialiasing)
            color = QColor(180, 180, 180) if isDarkTheme() else QColor(160, 160, 160)
            painter.setPen(color)
            font = QFont(self.font())
            font.setPointSize(12)
            painter.setFont(font)
            painter.drawText(
                self.viewport().rect(),
                Qt.AlignCenter,
                self.tr('Enter a URL above or drop it here')
            )
            painter.end()


class FooterBar(QWidget):
    """하단 컨트롤 바 (다운로드/중지/완료 지우기 + 카운터)"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.downloadButton = PrimaryPushButton(self.tr('Download'), self, FIF.PLAY)
        self.stopButton = PushButton(self.tr('Stop'), self, FIF.CLOSE)
        self.clearButton = PushButton(self.tr('Clear Finished'), self, FIF.DELETE)
        self.countLabel = CaptionLabel('0 / 0', self)

        self.__initLayout()
        self.__initWidget()

    def __initWidget(self):
        self.stopButton.setEnabled(False)

    def __initLayout(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.downloadButton)
        layout.addWidget(self.stopButton)
        layout.addWidget(self.clearButton)
        layout.addStretch(1)
        layout.addWidget(self.countLabel)

    def setCount(self, completed: int, total: int):
        self.countLabel.setText(f'{completed} / {total}')


class DownloadInterface(BaseInterface):
    """다운로드 인터페이스

    구조:
        TitleLabel "Downloads"      [고정]
        UrlBar                      [고정]
        CardListWidget (스크롤)     [stretch]
        FooterBar                   [고정]
    """

    def __init__(self, parent=None):
        super().__init__(title='Downloads', parent=parent)
        self.urlBar = UrlBar(self)
        self.cardList = CardListWidget(self)
        self.footerBar = FooterBar(self)

        self.__initLayout()
        self.__connectSignalToSlot()

    def __initLayout(self):
        self.contentLayout.setSpacing(12)
        self.contentLayout.setContentsMargins(0, 0, 0, 0)
        self.contentLayout.addWidget(self.urlBar)
        self.contentLayout.addWidget(self.cardList, 1)
        self.contentLayout.addWidget(self.footerBar)

    def __connectSignalToSlot(self):
        self.urlBar.urlEdit.returnPressed.connect(self.__onFetch)
        self.urlBar.fetchButton.clicked.connect(self.__onFetch)
        self.cardList.urlDropped.connect(self.__onFetch)
        self.footerBar.downloadButton.clicked.connect(self.__onDownloadToggle)
        self.footerBar.stopButton.clicked.connect(self.__onStop)
        self.footerBar.clearButton.clicked.connect(self.__onClearFinished)

    # ──────────────────────────── 슬롯 ────────────────────────────

    def __onFetch(self, url: str = ''):
        if not url:
            url = self.urlBar.urlEdit.text().strip()
        if not url:
            return
        self.urlBar.urlEdit.clear()
        print(f'Fetch URL: {url}')
        # TODO: ContentManager.fetchContent(url, cookies, downloadPath) 연결

    def __onDownloadToggle(self):
        # TODO: 다운로드 시작 / 일시정지 토글
        pass

    def __onStop(self):
        # TODO: 확인 다이얼로그 후 DownloadManager.stop()
        pass

    def __onClearFinished(self):
        self.cardList.clearFinished()
