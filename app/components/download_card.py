# coding: utf-8
from PySide6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel, QLineEdit
from PySide6.QtCore import Qt, Signal, QSize

from qfluentwidgets import (CardWidget, BodyLabel, CaptionLabel,
                            TransparentToolButton, ToggleButton,
                            ProgressRing, IconWidget, FluentIcon as FIF)

from ..common.download_state import DownloadState


class DownloadCardWidget(CardWidget):
    """VOD 다운로드 카드 위젯

    레이아웃:
        [썸네일] | Row1: 채널 프로필 · 채널명 · 타입 ─── 상태아이콘 · 삭제
                  Row2: 제목 (클릭 시 인라인 편집)
                  Row3: 해상도 버튼들 (동적 생성)
                  Row4: 경로 (클릭 시 인라인 편집) · 폴더열기
                  Row5: ─── ProgressRing · 상태텍스트 · 파일크기
    """

    deleteRequested = Signal()
    openFolderRequested = Signal()
    titleChanged = Signal(str)
    pathChanged = Signal(str)
    resolutionChanged = Signal(int)   # 선택된 해상도 인덱스

    def __init__(self, parent=None):
        super().__init__(parent)
        self._initWidgets()
        self._initLayout()
        self._initSignals()

    # ──────────────────────────── 위젯 초기화 ────────────────────────────

    def _initWidgets(self):
        # 썸네일
        self.thumbnailLabel = QLabel(self)
        self.thumbnailLabel.setFixedSize(96, 64)
        self.thumbnailLabel.setAlignment(Qt.AlignCenter)
        self.thumbnailLabel.setObjectName('thumbnailLabel')

        # Row1
        self.channelImageLabel = QLabel(self)
        self.channelImageLabel.setFixedSize(18, 18)
        self.channelImageLabel.setObjectName('channelImageLabel')

        self.channelNameLabel = CaptionLabel('', self)
        self.contentTypeLabel = CaptionLabel('', self)

        self.stateIconWidget = IconWidget(FIF.PAUSE_BOLD, self)
        self.stateIconWidget.setFixedSize(16, 16)

        self.deleteButton = TransparentToolButton(FIF.CLOSE, self)
        self.deleteButton.setFixedSize(28, 28)
        self.deleteButton.setToolTip(self.tr('Remove'))

        # Row2: 제목
        self.titleLabel = BodyLabel('', self)
        self.titleLabel.setElideMode(Qt.ElideRight)
        self.titleLabel.setCursor(Qt.IBeamCursor)
        self.titleLabel.setToolTip(self.tr('Click to edit title'))

        self.titleEdit = QLineEdit(self)
        self.titleEdit.setVisible(False)

        # Row3: 해상도 버튼 (동적 추가)
        self.resolutionButtons: list[ToggleButton] = []

        # Row4: 경로
        self.pathLabel = CaptionLabel('', self)
        self.pathLabel.setElideMode(Qt.ElideMiddle)
        self.pathLabel.setCursor(Qt.IBeamCursor)
        self.pathLabel.setToolTip(self.tr('Click to edit path'))

        self.pathEdit = QLineEdit(self)
        self.pathEdit.setVisible(False)

        self.folderButton = TransparentToolButton(FIF.FOLDER, self)
        self.folderButton.setFixedSize(28, 28)
        self.folderButton.setToolTip(self.tr('Open folder'))

        # Row5: 진행률
        self.progressRing = ProgressRing(self)
        self.progressRing.setFixedSize(36, 36)
        self.progressRing.setStrokeWidth(4)
        self.progressRing.setRange(0, 100)
        self.progressRing.setValue(0)
        self.progressRing.setVisible(False)

        self.statusLabel = CaptionLabel('', self)
        self.fileSizeLabel = CaptionLabel('', self)

    # ──────────────────────────── 레이아웃 ────────────────────────────

    def _initLayout(self):
        # Row1
        self.row1 = QHBoxLayout()
        self.row1.setContentsMargins(0, 0, 0, 0)
        self.row1.setSpacing(4)
        self.row1.addWidget(self.channelImageLabel)
        self.row1.addWidget(self.channelNameLabel)
        self.row1.addSpacing(4)
        self.row1.addWidget(self.contentTypeLabel)
        self.row1.addStretch(1)
        self.row1.addWidget(self.stateIconWidget)
        self.row1.addWidget(self.deleteButton)

        # Row2: 제목
        self.row2 = QHBoxLayout()
        self.row2.setContentsMargins(0, 0, 0, 0)
        self.row2.setSpacing(0)
        self.row2.addWidget(self.titleLabel, 1)
        self.row2.addWidget(self.titleEdit, 1)

        # Row3: 해상도 (동적)
        self.row3 = QHBoxLayout()
        self.row3.setContentsMargins(0, 0, 0, 0)
        self.row3.setSpacing(4)
        self.row3.addStretch(1)

        # Row4: 경로
        self.row4 = QHBoxLayout()
        self.row4.setContentsMargins(0, 0, 0, 0)
        self.row4.setSpacing(4)
        self.row4.addWidget(self.pathLabel, 1)
        self.row4.addWidget(self.pathEdit, 1)
        self.row4.addWidget(self.folderButton)

        # Row5: 진행
        self.row5 = QHBoxLayout()
        self.row5.setContentsMargins(0, 0, 0, 0)
        self.row5.setSpacing(6)
        self.row5.addStretch(1)
        self.row5.addWidget(self.progressRing)
        self.row5.addWidget(self.statusLabel)
        self.row5.addWidget(self.fileSizeLabel)

        # 우측 VBox
        self.rightLayout = QVBoxLayout()
        self.rightLayout.setSpacing(4)
        self.rightLayout.setContentsMargins(0, 0, 0, 0)
        self.rightLayout.addLayout(self.row1)
        self.rightLayout.addLayout(self.row2)
        self.rightLayout.addLayout(self.row3)
        self.rightLayout.addLayout(self.row4)
        self.rightLayout.addLayout(self.row5)

        # 메인
        mainLayout = QHBoxLayout(self)
        mainLayout.setContentsMargins(10, 8, 10, 8)
        mainLayout.setSpacing(10)
        mainLayout.addWidget(self.thumbnailLabel)
        mainLayout.addLayout(self.rightLayout, 1)

    # ──────────────────────────── 시그널 ────────────────────────────

    def _initSignals(self):
        self.deleteButton.clicked.connect(self.deleteRequested)
        self.folderButton.clicked.connect(self.openFolderRequested)
        self.titleLabel.mousePressEvent = lambda _: self._startTitleEdit()
        self.titleEdit.editingFinished.connect(self._finishTitleEdit)
        self.pathLabel.mousePressEvent = lambda _: self._startPathEdit()
        self.pathEdit.editingFinished.connect(self._finishPathEdit)

    # ──────────────────────────── 공개 API ────────────────────────────

    def setData(self, title: str = '', channelName: str = '', contentType: str = '',
                path: str = '', fileSize: str = ''):
        """카드 기본 정보 설정"""
        self.titleLabel.setText(title)
        self.titleEdit.setText(title)
        self.channelNameLabel.setText(channelName)
        self.contentTypeLabel.setText(f'[{contentType}]' if contentType else '')
        self.pathLabel.setText(path)
        self.pathEdit.setText(path)
        self.fileSizeLabel.setText(fileSize)

    def setThumbnail(self, pixmap):
        """썸네일 이미지 설정 (비동기 로드 후 메인 스레드에서 호출)"""
        scaled = pixmap.scaledToHeight(64, Qt.SmoothTransformation)
        self.thumbnailLabel.setPixmap(scaled)

    def setChannelImage(self, pixmap):
        """채널 프로필 이미지 설정"""
        scaled = pixmap.scaled(18, 18, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.channelImageLabel.setPixmap(scaled)

    def setResolutions(self, resolutions: list[str]):
        """해상도 버튼 동적 생성. resolutions = ['144', '360', '720', '1080'] 형식"""
        for btn in self.resolutionButtons:
            self.row3.removeWidget(btn)
            btn.deleteLater()
        self.resolutionButtons.clear()

        for i, res in enumerate(resolutions):
            btn = ToggleButton(f'{res}p', self)
            btn.setFixedHeight(24)
            btn.clicked.connect(lambda _, idx=i: self._onResolutionClicked(idx))
            self.row3.insertWidget(i, btn)
            self.resolutionButtons.append(btn)

        if self.resolutionButtons:
            # 기본값: 마지막(최고화질) 선택
            self.resolutionButtons[-1].setChecked(True)

    def setState(self, state: DownloadState, statusText: str = ''):
        """상태 전환 - 아이콘·ProgressRing·버튼 활성화 일괄 처리"""
        is_waiting = state == DownloadState.WAITING
        is_active  = state in (DownloadState.RUNNING, DownloadState.PAUSED)
        is_running = state == DownloadState.RUNNING

        self.progressRing.setVisible(is_active)
        self.deleteButton.setEnabled(not is_active)

        for btn in self.resolutionButtons:
            btn.setEnabled(is_waiting)

        icon_map = {
            DownloadState.WAITING:  FIF.PAUSE_BOLD,
            DownloadState.RUNNING:  FIF.SYNC,
            DownloadState.PAUSED:   FIF.PAUSE,
            DownloadState.FINISHED: FIF.ACCEPT,
            DownloadState.ERROR:    FIF.CLOSE,
        }
        self.stateIconWidget.setIcon(icon_map[state])
        self.statusLabel.setText(statusText)

    def setProgress(self, value: int, statusText: str = ''):
        """진행률(0~100) 및 상태 텍스트 업데이트"""
        self.progressRing.setValue(value)
        if statusText:
            self.statusLabel.setText(statusText)

    def setFileSize(self, text: str):
        self.fileSizeLabel.setText(text)

    def sizeHint(self):
        return QSize(-1, 116)

    # ──────────────────────────── 인라인 편집 ────────────────────────────

    def _startTitleEdit(self):
        self.titleLabel.setVisible(False)
        self.titleEdit.setVisible(True)
        self.titleEdit.setFocus()

    def _finishTitleEdit(self):
        self.titleEdit.setVisible(False)
        self.titleLabel.setVisible(True)
        text = self.titleEdit.text().strip()
        if text and text != self.titleLabel.text():
            self.titleLabel.setText(text)
            self.titleChanged.emit(text)

    def _startPathEdit(self):
        self.pathLabel.setVisible(False)
        self.pathEdit.setVisible(True)
        self.pathEdit.setFocus()

    def _finishPathEdit(self):
        self.pathEdit.setVisible(False)
        self.pathLabel.setVisible(True)
        text = self.pathEdit.text().strip()
        if text and text != self.pathLabel.text():
            self.pathLabel.setText(text)
            self.pathChanged.emit(text)

    # ──────────────────────────── 내부 ────────────────────────────

    def _onResolutionClicked(self, index: int):
        for i, btn in enumerate(self.resolutionButtons):
            btn.setChecked(i == index)
        self.resolutionChanged.emit(index)
