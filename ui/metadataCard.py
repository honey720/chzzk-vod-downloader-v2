import re
import requests
import threading

from PyQt5.QtWidgets import QFrame, QVBoxLayout, QHBoxLayout, QLabel, QSizePolicy, QPushButton, QProgressBar
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QPixmap
from io import BytesIO


class MetadataCard(QFrame):
    """
    메타데이터를 표시하는 카드 클래스.
    """
    deleteClicked = pyqtSignal(object)                      # 삭제 버튼 클릭 시 카드 객체를 전달
    downloadClicked = pyqtSignal(object, str, str, str)     # 다운로드 버튼 클릭 시 (base_url, height) 전달
    stopClicked = pyqtSignal(object)                        # Stop 버튼 클릭 시 카드 객체를 전달
    downloadFinished = pyqtSignal(object)

    def __init__(self, metadata, downloadPath, parent=None):
        super().__init__(parent)
        self.metadata = metadata
        self.setFrameShape(QFrame.Box)
        self.setFrameShadow(QFrame.Plain)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.initUI()

        self.isFinish = False  # 다운로드 완료 상태 플래그
        self.downloadPath = downloadPath
        self.contents_height = None
        self.base_url = None

    def initUI(self):
        # 전체 레이아웃
        self.metadataCardLayout = QVBoxLayout(self)
        self.metadataCardLayout.setContentsMargins(5, 5, 5, 5)
        self.metadataCardLayout.setSpacing(10)

        # --- 상단 섹션 ---
        self.metaTopSection, self.metaTopLayout = self.create_frame("H", margins=(0, 0, 0, 0), spacing=5)

        # 상단 좌측: 채널 이미지
        self.channelImageLabel = QLabel()
        self.channelImageLabel.setScaledContents(True)
        self.channelImageLabel.setFixedSize(30, 30)
        self.metaTopLayout.addWidget(self.channelImageLabel)

        # 상단 좌측: 채널 이름
        self.channelNameLabel = QLabel('')
        self.metaTopLayout.addWidget(self.channelNameLabel)

        # 상단 우측에 삭제 버튼 추가
        self.deleteButton = QPushButton("삭제")
        self.deleteButton.setFixedSize(50, 30)
        self.metaTopLayout.addWidget(self.deleteButton)

        self.deleteButton.clicked.connect(lambda: self.onDelete())

        # 신호 연결
        self.metadataCardLayout.addWidget(self.metaTopSection)
        
        # --- 하단 섹션 ---
        self.metaBottomSection, self.metaBottomLayout = self.create_frame("H", margins=(0, 0, 0, 0), spacing=5)

        # 하단 좌측: 썸네일
        self.metaBottomLeftSection, self.metaBottomLeftLayout = self.create_frame("V", margins=(0, 0, 0, 0), spacing=5)
        
        self.thumbnailLabel = QLabel()
        self.thumbnailLabel.setScaledContents(True)
        self.thumbnailLabel.setFixedSize(256, 144)
        self.metaBottomLeftLayout.addWidget(self.thumbnailLabel)

        self.metaBottomLayout.addWidget(self.metaBottomLeftSection)
        
        # 하단 우측: 제목, 카테고리, 시간 정보
        self.vodInfoSection, self.vodInfoLayout = self.create_frame("V", margins=(0, 0, 0, 0), spacing=5)

        self.titleLabel = QLabel('')
        self.categoryLabel = QLabel('')
        self.liveOpenDateLabel = QLabel('')
        self.durationLabel = QLabel('')
        self.downloadPathLabel = QLabel('')

        self.vodInfoLayout.addWidget(self.titleLabel)
        self.vodInfoLayout.addWidget(self.categoryLabel)
        self.vodInfoLayout.addWidget(self.liveOpenDateLabel)
        self.vodInfoLayout.addWidget(self.durationLabel)
        self.vodInfoLayout.addWidget(self.downloadPathLabel)
        
        # 하단 우측: 해상도 버튼
        self.resolutionSection, self.resolutionLayout = self.create_frame("V", margins=(0, 0, 0, 0), spacing=5)
        
        self.resolutionButtonsLayout = QHBoxLayout()
        self.resolutionLayout.addLayout(self.resolutionButtonsLayout)

        self.vodInfoLayout.addWidget(self.resolutionSection)

        # 하단 우측: 다운로드 진행 바, 진행 정보, 스레드, 시간, 속도
        self.progressSection, self.progressLayout = self.create_frame("V", margins=(0, 0, 0, 0), spacing=5)

        self.progressFuncLayout = QHBoxLayout()
        
        self.progressBar = QProgressBar()
        self.progressFuncLayout.addWidget(self.progressBar)

        self.stopButton = QPushButton('Stop')
        self.stopButton.setEnabled(False)
        self.stopButton.clicked.connect(self.onStop)

        self.progressFuncLayout.addWidget(self.stopButton)

        self.progressLayout.addLayout(self.progressFuncLayout)

        self.downloadStatusLabel = QLabel('다운로드 대기 중...')
        self.progressLayout.addWidget(self.downloadStatusLabel)

        self.progressInfoLayout = QHBoxLayout()

        self.maxThreadsLabel = QLabel('')
        self.avgSpeedLabel = QLabel('')
        self.timeLabel = QLabel('')
        self.threadStatusLabel = QLabel('')

        self.progressInfoLayout.addWidget(self.maxThreadsLabel)
        self.progressInfoLayout.addWidget(self.avgSpeedLabel)
        self.progressInfoLayout.addWidget(self.timeLabel)
        self.progressInfoLayout.addWidget(self.threadStatusLabel)

        self.progressLayout.addLayout(self.progressInfoLayout)

        self.vodInfoLayout.addWidget(self.progressSection)

        # 오른쪽 하단 레이아웃을 하단 섹션에 추가
        self.metaBottomLayout.addWidget(self.vodInfoSection)

        # 하단 섹션을 메인 레이아웃에 추가
        self.metadataCardLayout.addWidget(self.metaBottomSection)

    def create_frame(self, layout_type, parent=None, margins=(0, 0, 0, 0), spacing=5):
        """
        레이아웃 헬퍼 메서드
        """
        frame = QFrame(parent)
        if layout_type == "H":
            layout = QHBoxLayout(frame)
        elif layout_type == "V":
            layout = QVBoxLayout(frame)
        else:
            raise ValueError("Invalid layout type. Use 'H' or 'V'.")
        layout.setContentsMargins(*margins)
        layout.setSpacing(spacing)
        return frame, layout

    # ============ 메타데이터 및 해상도 처리 ============

    def displayMetadata(self):
        """
        가져온 메타데이터를 라벨 및 이미지에 표시한다.
        """
        title = self.metadata.get('title', 'Unknown Title')
        thumbnail_url = self.metadata.get('thumbnailImageUrl', '')
        category = self.metadata.get('videoCategoryValue', 'Unknown Category')
        channel_name = self.metadata.get('channelName', 'Unknown Channel')
        channel_image_url = self.metadata.get('channelImageUrl', '')
        live_open_date = self.metadata.get('liveOpenDate', 'Unknown Date')
        duration = self.metadata.get('duration', 0)

        duration_str = f"{duration // 3600:02d}:{(duration % 3600) // 60:02d}:{duration % 60:02d}"

        self.channelNameLabel.setText(f"{channel_name}")
        self.titleLabel.setText(f"Title: {title}")
        self.categoryLabel.setText(f"Category: {category}")
        self.liveOpenDateLabel.setText(f"Live Open Date: {live_open_date}")
        self.durationLabel.setText(f"Duration: {duration_str}")
        self.downloadPathLabel.setText(f"DownloadPath: {self.downloadPath}")

        # 이미지 비동기 로딩
        self.loadImageFromUrl(self.channelImageLabel, channel_image_url, 30, 30)
        self.loadImageFromUrl(self.thumbnailLabel, thumbnail_url, 256, 144)
    
    def loadImageFromUrl(self, label, url, width, height):
        """
        주어진 URL에서 이미지를 다운로드해 QLabel에 띄운다.
        """
        if not url:
            label.clear()
            return
        
        # 이미지 로딩 스레드 시작
        thread = threading.Thread(target=self.fetchImage, args=(label, url, width, height), daemon=True)
        thread.start()

    def fetchImage(self, label, url, width, height):
        try:
            response = requests.get(url)
            response.raise_for_status()
            image = QPixmap()
            image.loadFromData(BytesIO(response.content).read())
            scaled_image = image.scaled(
                width, height, Qt.IgnoreAspectRatio, Qt.SmoothTransformation
            )
            label.setPixmap(scaled_image)
        except Exception as e:
            print(f"Error loading image from {url}: {e}")

    def addRepresentationButtons(self, unique_reps):
        """
        해상도 목록(Representation)을 정렬 후, 버튼을 생성해 Resolution 영역에 배치한다.
        """
        sorted_reps = sorted(unique_reps, key=lambda x: int(x[1]) if x[1].isdigit() else 9999)
        auto_height = sorted_reps[-1][1]
        auto_base_url = sorted_reps[-1][2]
        self.setHeightUrl(auto_height, auto_base_url)

        for width, height, base_url in sorted_reps:
            self.addRepresentationButton(width, height, base_url)

    def addRepresentationButton(self, width, height, base_url):
        """
        해상도 버튼을 추가하고, 비동기로 파일 사이즈를 헤더에서 가져와 버튼 텍스트를 업데이트한다.
        """
        button = QPushButton(f'{width}x{height}', self)
        button.clicked.connect(lambda: self.setHeightUrl(height, base_url))
        self.resolutionButtonsLayout.addWidget(button)

        def update_button_text():
            try:
                resp = requests.head(base_url)
                resp.raise_for_status()
                size = int(resp.headers.get('content-length', 0))
                units = ["B", "KB", "MB", "GB", "TB"]
                unit_index = 0
                while size >= 1024 and unit_index < len(units) - 1:
                    size /= 1024
                    unit_index += 1
                new_text = f'{width}x{height} ({size:.2f} {units[unit_index]})'
                button.setText(new_text)
            except Exception:
                pass

        thread = threading.Thread(target=update_button_text, daemon=True)
        thread.start()

    def setHeightUrl(self, height, base_url):
        self.contents_height = height
        self.base_url = base_url
        self.downloadStatusLabel.setText('다운로드 대기 중...' + height)

    def onDownload(self):
        """
        해상도 버튼 클릭 시 다운로드 진행.
        """
        if self.metadata:
            title = self.metadata.get('title', 'Unknown Title')
            category = self.metadata.get('videoCategoryValue', 'Unknown Category')
            live_open_date = self.metadata.get('liveOpenDate', 'Unknown Date')

            # 특수 문자 제거
            title = re.sub(r'[\\/:\*\?"<>|]', '', title)
            category = re.sub(r'[\\/:\*\?"<>|]', '', category)
            prefix_date = live_open_date.split(' ')[0] if live_open_date else ''
            
            if not category and prefix_date == '':
                default_filename = f"{title}.mp4"
            elif not category:
                default_filename = f"{prefix_date}) {title} {self.contents_height}.mp4"
            elif not prefix_date == '':
                default_filename = f"[{category}] {title} {self.contents_height}.mp4"
            else:
                default_filename = f"{prefix_date}) [{category}] {title} {self.contents_height}.mp4"
        else:
            default_filename = "video.mp4"

        output_path = self.downloadPath + '\\' + default_filename

        if output_path:
            # 다운로드 요청 시그널 발행
            self.downloadClicked.emit(self, self.base_url, output_path, self.contents_height)

    def set_resolution_buttons_enabled(self, enabled):
        for i in range(self.resolutionButtonsLayout.count()):
            self.resolutionButtonsLayout.itemAt(i).widget().setEnabled(enabled)

    def onDelete(self):
        """
        삭제 버튼 클릭 시 발생하는 핸들러
        """
        self.deleteClicked.emit(self)

    def onStop(self):
        """
        중단 버튼 콜백.
        """
        self.stopClicked.emit(self)

    def updateProgress(self, value, status_message):
        """
        진행률(progressBar)과 상태 라벨을 업데이트한다.
        """
        self.progressBar.setValue(value)
        self.downloadStatusLabel.setText(status_message)
    
    def updateStatus(self, status):
        """
        상태 업데이트.
        """
        if status in ["다운로드 완료", "다운로드 중단"]:
            self.resetUIAfterDownload(status)
        self.status = status
        self.downloadStatusLabel.setText(status)

    def resetUIAfterDownload(self, status):
        """
        다운로드가 완료되거나 중지된 후, UI를 원상 복구한다.
        """
        self.deleteButton.setEnabled(True)
        self.stopButton.setEnabled(False)
        if status == "다운로드 중단":
            self.set_resolution_buttons_enabled(True)
        else: # 다운로드 완료
            self.isFinish = True
            self.deleteButton.setEnabled(True)
            self.downloadFinished.emit(self)

    def updateThreadStatus(self, completed, total, failed, restart):
        self.threadStatusLabel.setText(f"Seg.: {completed}/{total} (F: {failed}, R: {restart})")

    def updateTimeStatus(self, elapsed, remaining):
        self.timeLabel.setText(f'Ele.T/Est.T: {elapsed}/{remaining}')

    def updateActiveThreads(self, active_threads):
        self.maxThreadsLabel.setText(f'Threads: {active_threads}')

    def updateAvgSpeed(self, avg_speed):
        self.avgSpeedLabel.setText(f'Avg.Spd: {avg_speed:.2f} MB/s')

    def updateCardColor(self):
        if self.isFinish:
            self.downloadStatusLabel.setStyleSheet("""
                color: blue;
                font-weight: bold;
        """)
        else:
            self.downloadStatusLabel.setStyleSheet("""
                color: red;
                font-weight: bold;
            """)
             