import os

from PyQt5.QtWidgets import QWidget, QVBoxLayout
from ui.metadataCard import MetadataCard
from PyQt5.QtCore import Qt, pyqtSignal

class MetadataCardManager(QWidget):
    """
    메타데이터 카드를 관리하는 클래스.
    """
    downloadRequested = pyqtSignal(object, str, str, str)  # (base_url, output_path, height)
    stopRequested = pyqtSignal(object)
    deleteCardRequested = pyqtSignal(object)
    downloadFinishedRequested = pyqtSignal(object)
    downloadAllFinishedRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.metadataCards = []
        self.endCards = []
        self.initUI()

    def initUI(self):
        self.metadataCardLayout = QVBoxLayout(self)
        self.metadataCardLayout.setAlignment(Qt.AlignTop)
        self.metadataCardLayout.setSpacing(10)
        self.setLayout(self.metadataCardLayout)

    def addCard(self, metadata, unique_reps, downloadPath):
        """
        새로운 메타데이터 카드를 추가하는 메서드.
        """
        card = MetadataCard(metadata, downloadPath)

        card.displayMetadata()
        card.addRepresentationButtons(unique_reps)

        # 시그널 연결
        card.deleteClicked.connect(self.removeCard)
        card.downloadClicked.connect(self.emitDownloadRequested)
        card.stopClicked.connect(self.emitStopRequested)
        card.downloadFinished.connect(self.emitDownloadFinishedRequest)

        self.metadataCardLayout.addWidget(card)
        self.metadataCards.append(card)

    def downloadCard(self):
        """
        다운로드 리스트에서 다운로드 기능 수행.
        """
        if not self.metadataCards:
            self.emitDownloadAllFinishedRequest()
        else:
            card: MetadataCard = self.metadataCards[0]
            try:
                if not os.path.exists(card.downloadPath):
                    self.emitDownloadFinishedRequest(card)
                    raise ValueError("Invalid file path")
                card.onDownload()
            except Exception as e:
                card.downloadStatusLabel.setText(f'오류 발생: {e}')

    def removeCard(self, card: MetadataCard):
        """
        특정 메타데이터 카드를 제거하는 메서드.
        """
        self.metadataCardLayout.removeWidget(card)
        self.deleteCardRequested.emit(card)

        if card in self.metadataCards:
            self.metadataCards.remove(card)
        elif card in self.endCards:
            self.endCards.remove(card)
            
        card.deleteLater()

    def emitDownloadRequested(self, card:MetadataCard, base_url, output_path, height):
        """
        다운로드 요청 시그널.
        """
        self.downloadRequested.emit(card, base_url, output_path, height)
    
    def emitStopRequested(self, card:MetadataCard):
        """
        다운로드 중단 시그널.
        """
        self.stopRequested.emit(card)
    
    def emitDownloadFinishedRequest(self, card:MetadataCard):
        """
        다운로드 완료 시그널.
        """
        card = self.metadataCards.pop(0)
        self.endCards.append(card)
        card.updateCardColor()
        self.downloadFinishedRequested.emit(card)
        self.downloadCard()
        
    def emitDownloadAllFinishedRequest(self):
        """
        다운로드 모두 완료 시그널.
        """
        self.downloadAllFinishedRequested.emit()