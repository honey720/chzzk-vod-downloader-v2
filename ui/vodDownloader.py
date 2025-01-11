import os
import threading
import config.config as config

from PyQt5.QtWidgets import QWidget, QVBoxLayout, QFrame, QSizePolicy, QGridLayout, QLineEdit, QPushButton, QLabel, QScrollArea, QHBoxLayout, QDialog, QMessageBox, QFileDialog, QApplication
from PyQt5.QtCore import Qt

from threads import DownloadThread
from ui.metadataCard import MetadataCard
from ui.metadataCardManager import MetadataCardManager
from ui.settingDialog import SettingDialog
from utils import extract_video_no, get_video_info, get_dash_manifest


class VodDownloader(QWidget):
    """
    치지직 VOD 다운로더 메인 UI 클래스.
    """
    def __init__(self):
        super().__init__()
        self.downloadThread = None
        # 메타데이터 카드 관리 객체 생성
        self.cardManager = MetadataCardManager()

        # 쿠키 값 저장 변수
        self.nidaut_value = config.NID_AUT
        self.nidses_value = config.NID_SES

        # 카드 갯수 추적 변수 초기화
        self.total_downloads = 0
        self.completed_downloads = 0

        self._initUI()
        self._setupSignals()

    def _initUI(self):
        """
        UI 레이아웃 및 위젯을 초기화. (레이아웃마다 QFrame으로 테두리를 표시)
        """
        self.setWindowTitle('치지직 VOD 다운로더')

        screen = QApplication.primaryScreen()
        screen_size = screen.size()  # QSize 객체 반환
        screen_width = screen_size.width()
        screen_height = screen_size.height()


        width_ratio = 0.5
        height_ratio = 0.5
        min_width = int(screen_width * width_ratio)
        min_height = int(screen_height * height_ratio)
        
        self.setMinimumSize(min_width, min_height)
        self.resize(min_width, min_height)

        # 메인 레이아웃
        self.mainLayout = QVBoxLayout()

        # ───────────────────────────────────────────
        # 1) 상단: URL 입력 / 다운로드 버튼
        # ───────────────────────────────────────────

        headerFrame = QFrame()
        headerFrame.setFrameShape(QFrame.Box)      # 테두리 모양(Box, Panel 등)
        headerFrame.setFrameShadow(QFrame.Raised)  # Raised, Sunken 등
        headerFrame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.mainLayout.addWidget(headerFrame)

        headerFrameLayout = QGridLayout(headerFrame)
        headerFrameLayout.setContentsMargins(5, 5, 5, 5)
        headerFrameLayout.setSpacing(5)

        self.urlInputLabel = QLabel('치지직 VOD URL:')  # 초기값 설정
        self.urlInputLabel.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        headerFrameLayout.addWidget(self.urlInputLabel, 0, 0, Qt.AlignLeft)

        self.urlInput = QLineEdit()
        self.urlInput.setPlaceholderText("치지직 URL 입력")
        headerFrameLayout.addWidget(self.urlInput, 0, 1)

        self.fetchButton = QPushButton('Add VOD')
        self.fetchButton.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        headerFrameLayout.addWidget(self.fetchButton, 0, 2, Qt.AlignRight)

        headerFrameLayout.setColumnStretch(1, 10)  # URL 입력 창 확장

        self.downloadPathLabel = QLabel('다운로드 경로:')  # 초기값 설정
        self.downloadPathLabel.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        headerFrameLayout.addWidget(self.downloadPathLabel, 1, 0, Qt.AlignLeft)

        self.downloadPathInput = QLineEdit()
        self.downloadPathInput.setPlaceholderText("다운로드 경로 입력")
        self.downloadPathInput.setText(os.getcwd())  # 초기 경로 설정
        headerFrameLayout.addWidget(self.downloadPathInput, 1, 1)

        self.downloadPathButton = QPushButton('경로 찾기')
        self.downloadPathButton.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        headerFrameLayout.addWidget(self.downloadPathButton, 1, 2, Qt.AlignRight)

        self.linkStatusLabel = QLabel('')
        headerFrameLayout.addWidget(self.linkStatusLabel, 2, 0, 1, 2)

        self.downloadButton = QPushButton("Download")
        self.downloadButton.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        headerFrameLayout.addWidget(self.downloadButton, 2, 2, Qt.AlignRight)

        headerFrameLayout.setColumnStretch(1, 10)  # 다운로드 경로 입력 창 확장
        headerFrameLayout.setColumnStretch(0, 1)
        headerFrameLayout.setColumnStretch(2, 1)

        # ───────────────────────────────────────────
        # 2) 메타데이터 영역
        # ───────────────────────────────────────────

        # 스크롤 영역
        scrollArea = QScrollArea()
        scrollArea.setFrameShape(QFrame.Box)      # 테두리 모양(Box, Panel 등)
        scrollArea.setFrameShadow(QFrame.Raised)  # Raised, Sunken 등
        scrollArea.setWidgetResizable(True)  # 내부 위젯 크기에 따라 스크롤 가능
        scrollArea.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # 시그널 연결
        self.cardManager.downloadRequested.connect(self.startDownload)
        self.cardManager.stopRequested.connect(self.onStop)
        self.cardManager.deleteCardRequested.connect(self.onDeleteCard)
        self.cardManager.downloadFinishedRequested.connect(self.onDownloadFinishedCard)
        self.cardManager.downloadAllFinishedRequested.connect(self.onDownloadAllFinished)

        # 스크롤 영역에 메타 데이터 카드 관리 객체 설정
        scrollArea.setWidget(self.cardManager)

        # 스크롤 영역을 메인 레이아웃에 추가
        self.mainLayout.addWidget(scrollArea)

        # ───────────────────────────────────────────
        # 3) 추가 정보(다운로드 현황)
        # ───────────────────────────────────────────
        
        infoFrame = QFrame()
        infoFrame.setFrameShape(QFrame.Box)
        infoFrame.setFrameShadow(QFrame.Raised)
        infoFrame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        infoLayout = QHBoxLayout()
        infoFrame.setLayout(infoLayout)

        # 다운로드 갯수 표시 QLabel 추가
        self.downloadCountLabel = QLabel('Downloads: 0/0')  # 초기값 설정
        infoLayout.addWidget(self.downloadCountLabel)

        self.settingButton = QPushButton("Settings")
        self.settingButton.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        infoLayout.addWidget(self.settingButton)

        self.mainLayout.addWidget(infoFrame)

        # 메인 레이아웃 설정
        self.setLayout(self.mainLayout)
        self.show()

    def _setupSignals(self):
        """
        각종 버튼 클릭 시그널 및 UI 내 이벤트를 핸들링할 슬롯을 연결한다.
        """
        self.fetchButton.clicked.connect(self.onFetch)
        self.downloadButton.clicked.connect(self.onDownloadPause)
        self.settingButton.clicked.connect(self.onSetting)
        self.downloadPathButton.clicked.connect(self.onFindPath)

    def onFetch(self):
        """
        VOD URL을 입력받아 메타데이터를 가져오고, 메타데이터 카드를 생성합니다.
        """
        vod_url = self.urlInput.text()
        if not vod_url:
            QMessageBox.warning(self, "경고", "VOD URL을 입력하세요.")
            return
        
        if not vod_url:
            QMessageBox.warning(self, "경고", "VOD URL을 입력하세요.")
            return
        
        data = config.load_config().get("cookies", {})
        cookies = {
            'NID_AUT': data.get("NID_AUT", ""),
            'NID_SES': data.get("NID_SES", "")
        }
        try:
            self.linkStatusLabel.setText('Fetching resolutions...')
            video_no = extract_video_no(vod_url)
            if not video_no:
                raise ValueError("Invalid VOD URL")

            # 비디오 정보 가져오기
            video_id, in_key, metadata = get_video_info(video_no, cookies)
            if not video_id or not in_key:
                raise ValueError("Invalid cookies value")

            # DASH 매니페스트 가져오기
            unique_reps = get_dash_manifest(video_id, in_key)
            if not unique_reps:
                raise ValueError("Failed to get DASH manifest")

            # 결과 처리
            downloadPath = self.downloadPathInput.text()
            if downloadPath == '':
                downloadPath = os.getcwd()
            self.cardManager.addCard(metadata, unique_reps, downloadPath)
            self.total_downloads += 1
            self.updateDownloadCountLabel()

            self.linkStatusLabel.setText('Resolutions fetched successfully.')
        except Exception as e:
            self.linkStatusLabel.setText(f'오류 발생: {e}')
    
    def onDownloadPause(self):
        """
        추가한 동영상에 대한 다운로드 버튼.
        """

        if self.downloadThread:
            if self.downloadButton.text() == 'Pause':
                self.downloadThread.pause()
                self.downloadButton.setText('Download')
            else:
                self.downloadThread.resume()
                self.downloadButton.setText('Pause')
        else:
            if not self.cardManager.metadataCards:
                QMessageBox.warning(self, "경고", "추가된 VOD 가 없습니다.")
                return
            self.cardManager.downloadCard()
            self.downloadButton.setText('Pause')
    
    def onSetting(self):
        """
        설정 파일에 저장된 값 불러오기 버튼.
        """
        data = config.load_config()
        dialog = SettingDialog(parent=self)
        if dialog.exec_() == QDialog.Accepted:
            data['cookies'] = {"NID_AUT": self.nidaut_value, "NID_SES": self.nidses_value}

    def onFindPath(self):
        """
        다운로드 경로를 찾기위한 버튼.
        """
        downloadPath = QFileDialog.getExistingDirectory()
        self.downloadPathInput.setText(downloadPath)

    def onStop(self, card:MetadataCard):
        """
        중지 버튼 콜백.
        """
        if self.downloadThread:
            self.downloadThread.stop()
            self.downloadThread.wait()  # 스레드가 완전히 종료될 때까지 대기
            self.downloadButton.setText('Download')
            card.stopButton.setEnabled(False)
            self.downloadThread = None  # 스레드 객체를 삭제

    def onDeleteCard(self, card:MetadataCard):
        """
        메타데이터 카드 삭제 버튼 콜백.
        """
        if card.isFinish:
            self.completed_downloads -= 1
        self.total_downloads -= 1
        self.updateDownloadCountLabel()
    
    def onDownloadFinishedCard(self, card:MetadataCard):
        """
        메타데이터 카드 다운로드 완료 콜백.
        """
        if card.isFinish:
            self.completed_downloads += 1
            self.updateDownloadCountLabel()
            # print("download2")
            # self.cardManager.downloadCard()

    def onDownloadAllFinished(self):
        """
        모든 메타데이터 카드 다운로드 완료 콜백.
        """
        self.downloadButton.setText('Download')
        QMessageBox.information(self, "완료", "다운로드를 완료했습니다.")
        self.downloadThread = None  # 스레드 객체를 삭제


    # ============ 다운로드 진행 준비 및 상태 업데이트 ============

    def startDownload(self, card:MetadataCard, base_url, output_path, height):
        """
        특정 해상도에 대한 다운로드 스레드를 생성 및 시작하기 전 UI 상태 업데이트를 수행한다.
        """

        self.downloadThread = DownloadThread(base_url, output_path, height)
        self._connectThreadSignals(card)
        self.downloadThread.start()
        
        card.set_resolution_buttons_enabled(False)
        card.stopButton.setEnabled(True)
        card.deleteButton.setEnabled(False)

    def _connectThreadSignals(self, card: MetadataCard):
        """
        다운로드 스레드와 UI를 연결하는 시그널 슬롯 설정.
        """

        # MetadataCard 업데이트
        self.downloadThread.progress.connect(lambda value, status_message: card.updateProgress(value, status_message))
        self.downloadThread.paused.connect(lambda: card.updateStatus("다운로드 정지"))
        self.downloadThread.resumed.connect(lambda: card.updateStatus("다운로드 재개"))
        self.downloadThread.stopped.connect(lambda: card.updateStatus("다운로드 중단"))
        self.downloadThread.completed.connect(lambda: card.updateStatus("다운로드 완료"))

        # 추가적인 상태 업데이트
        self.downloadThread.update_threads.connect(lambda completed, total, failed, restart: card.updateThreadStatus(completed, total, failed, restart))
        self.downloadThread.update_time.connect(lambda elapsed, remaining: card.updateTimeStatus(elapsed, remaining))
        self.downloadThread.update_active_threads.connect(lambda active_threads: card.updateActiveThreads(active_threads))
        self.downloadThread.update_avg_speed.connect(lambda avg_speed: card.updateAvgSpeed(avg_speed))

    def updateDownloadCountLabel(self):
        """
        다운로드 갯수 라벨을 업데이트한다.
        """
        self.downloadCountLabel.setText(f'Downloads: {self.completed_downloads}/{self.total_downloads}')