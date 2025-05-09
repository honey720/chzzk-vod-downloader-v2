import os, requests, threading
from PySide6.QtWidgets import QWidget, QPushButton, QMessageBox
from PySide6.QtGui import QPixmap, QDesktopServices
from PySide6.QtCore import Qt, QSize, Signal, QUrl, QDir, QProcess
from content.data import ContentItem
from download.state import DownloadState
from ui.contentItemWidget import Ui_ContentItemWidget
from io import BytesIO
import platform

class ContentItemWidget(QWidget, Ui_ContentItemWidget):
    """컨텐츠 정보를 표시하는 커스텀 위젯"""

    textChanged = Signal(str)
    deleteRequest = Signal()

    def __init__(self, item: ContentItem, index=0, parent=None):
        super().__init__(parent)
        self.item = item  # ContentItem 저장
        self.index = index  # 인덱스 저장
        self.isEditing = False
        self.setupUi(self)  # TODO: 테마별 스타일시트 설정하기
        self.setupDynamicUi()  # TODO: 테마별 스타일시트 설정하기
        self.setupSignals()  # 시그널 연결

    def setupDynamicUi(self):
        self.loadImageFromUrl(self.channelImageLabel, self.item.channel_image_url, 30, "channel")
        self.loadImageFromUrl(self.thumbnailLabel, self.item.thumbnail_url, 66, "thumbnail")
        self.contentTypeLabel.setText(self.item.content_type) # 콘텐츠 타입 업데이트
        self.channelNameLabel.setText(self.item.channel_name) # 채널 이름 업데이트
        self.progressLabel.setText("") # 진행률 업데이트
        self.deleteButton.setText("❌")
        self.indexLabel.setText(f"#{self.index}") # 인덱스 업데이트
        self.titleLabel.setText(self.item.title) # 제목 업데이트
        self.titleEdit.setText(self.item.title) # 제목 업데이트
        self.titleEdit.setVisible(False) # 제목 수정용 QLineEdit 숨김
        self.directoryLabel.setText(self.item.download_path) # 다운로드 경로 업데이트
        self.directoryEdit.setText(self.item.download_path) # 다운로드 경로 업데이트
        self.directoryEdit.setVisible(False) # 다운로드 경로 수정용 QLineEdit 숨김
        self.openDirectoryButton.setText("📁")

    def setupSignals(self):
        self.deleteButton.clicked.connect(self.requestDelete)
        self.titleLabel.mousePressEvent = self.startTitleEditing
        self.titleEdit.editingFinished.connect(self.finishTitleEditing)
        self.directoryLabel.mousePressEvent = self.startPathEditing
        self.directoryEdit.editingFinished.connect(self.finishPathEditing)
        self.openDirectoryButton.clicked.connect(self.requestOpenDir)

    def addRepresentationButtons(self):
        """
        해상도 목록(Representation)을 정렬 후, 버튼을 생성해 Resolution 영역에 배치한다.
        """

        self.buttons = []
        for unique_rep in self.item.unique_reps:
            unique_rep.append("Unknown")  # 초기 값 설정

        self.setresolutionUrlSize(self.item.unique_reps[-1][0], self.item.unique_reps[-1][1], -1)

        for index, (resolution, base_url, _) in enumerate(self.item.unique_reps):
            self.addRepresentationButton(resolution, base_url, index)

    def addRepresentationButton(self, resolution, base_url, index):
        """
        해상도 버튼을 추가하고, 비동기로 파일 사이즈를 헤더에서 가져와 버튼 텍스트를 업데이트한다.
        """
        button = QPushButton(f'{resolution}p', self)
        button.clicked.connect(lambda: self.setresolutionUrlSize(resolution, base_url, index, button))
        self.titleLayout.addWidget(button)
        button.setFixedSize(60, 30)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.buttons.append(button)

        def update_button_text():
            try:
                resp = requests.head(base_url)
                resp.raise_for_status()
                size = int(resp.headers.get('content-length', 0))
                
                size_text = self.setSize(size)
                self.item.unique_reps[index][-1] = size_text

                if len(self.item.unique_reps) - 1 == index:
                    self.setresolutionUrlSize(resolution, base_url, index, button)

                button.setToolTip(size_text)
            except Exception:
                pass

        thread = threading.Thread(target=update_button_text, daemon=True)
        thread.start()

    def setresolutionUrlSize(self, resolution, base_url, index=None, button:QPushButton = None):
        if self.item.downloadState == DownloadState.WAITING:
            if button is not None:
                for btn in self.buttons:
                    btn.setEnabled(True)
                button.setDisabled(True)
            self.item.resolution = resolution
            self.item.base_url = base_url
            if index is not None:
                self.item.total_size = self.item.unique_reps[index][-1]
                self.fileSizeLabel.setText(f" {self.item.unique_reps[index][-1]}")

    def loadImageFromUrl(self, label, url, maxHeight, type):
        """
        주어진 URL에서 이미지를 다운로드해 QLabel에 띄운다.
        세로 높이를 고정하고 가로 크기를 비율에 맞게 조정한다.
        """
        if not url:
            label.clear()
            return
        
        # 이미지 로딩 스레드 시작
        thread = threading.Thread(target=self.fetchImage, args=(label, url, maxHeight, type), daemon=True)
        thread.start()

    def fetchImage(self, label, url, maxHeight, type):
        try:
            response = requests.get(url)
            response.raise_for_status()
            image = QPixmap()
            image.loadFromData(BytesIO(response.content).read())
            
            # 원본 이미지의 비율 계산
            original_width = image.width()
            original_height = image.height()
            
            if type == "channel" and original_height > original_width:
                # 가로 높이를 고정하고 세로 크기를 비율에 맞게 계산
                aspect_ratio = original_height / original_width
                new_width = maxHeight
                new_height = int(maxHeight * aspect_ratio)
            else:      
                # 세로 높이를 고정하고 가로 크기를 비율에 맞게 계산      
                aspect_ratio = original_width / original_height
                new_height = maxHeight
                new_width = int(new_height * aspect_ratio)
            
            scaled_image = image.scaled(
                new_width, new_height, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
            label.setPixmap(scaled_image)
        except Exception as e:
            print(f"Error loading image from {url}: {e}")

    def setData(self, item: ContentItem, index: int):
        """✅ 모델 데이터를 위젯에 반영"""
        self.item = item
        self.index = index
        self.indexLabel.setText(f"#{index}")  # ✅ 인덱스 업데이트
        self.channelNameLabel.setText(item.channel_name)
        self.titleLabel.setText(item.title)
        self.directoryLabel.setText(item.download_path)

        if self.item.downloadState == DownloadState.WAITING:
            self.statusLabel.setText(self.tr("Download waiting"))
            self.fileSizeLabel.setText(f" {item.total_size}")
            self.progressLabel.setText(" ")

        elif self.item.downloadState == DownloadState.RUNNING:
            self.statusLabel.setText(f"{item.download_remain_time}  {item.download_speed}")
            self.fileSizeLabel.setText(f"  {self.setSize(item.download_size)} / {item.total_size}")
            self.progressLabel.setText(f"  {item.download_progress}% ")

        elif self.item.downloadState == DownloadState.PAUSED:
            self.statusLabel.setText(self.tr("Download paused"))
            self.fileSizeLabel.setText(f"  {self.setSize(item.download_size)} / {item.total_size}")
            self.progressLabel.setText(f"  {item.download_progress}% ")

        elif self.item.downloadState == DownloadState.FINISHED:
            self.statusLabel.setText(f"{item.download_time}")
            self.fileSizeLabel.setText(f"  {self.setSize(item.download_size)} / {item.total_size}")
            self.progressLabel.setText(f"  {item.download_progress}% ")

    def getData(self) -> ContentItem:
        """✅ 위젯에서 입력된 데이터를 가져와서 ContentItem으로 반환"""
        return ContentItem(
            #index=self.item.index,
            channel_name=self.channelNameLabel.text(),
            title=self.titleEdit.text(),
            directory=self.directoryLabel.text(),
            #status=self.statusLabel.text(),
            progress=self.progressLabel.text(),
            #remaining_time=self.remainingTimeLabel.text(),
            #size_info=self.sizeInfoLabel.text(),
        )

    def startTitleEditing(self, event):
        """✅ QLabel을 더블클릭하면 QLineEdit로 변경"""
        if self.item.downloadState == DownloadState.WAITING:
            if not self.isEditing:
                self.isEditing = True
                self.titleEdit.setText(self.titleLabel.text())  # ✅ 현재 값 적용
                self.titleLabel.setVisible(False)
                self.titleEdit.setVisible(True)
                self.titleEdit.setFocus()  # ✅ 포커스 이동

    def finishTitleEditing(self):
        """✅ QLineEdit에서 Enter 또는 포커스 해제 시 QLabel로 복귀"""
        self.isEditing = False
        self.titleEdit.setVisible(False)
        self.titleLabel.setVisible(True)
        new_text = self.titleEdit.text().strip()
        if new_text:
            self.titleLabel.setText(new_text)  # ✅ UI 업데이트
            self.item.title = new_text  # ✅ 데이터 업데이트
            self.textChanged.emit(new_text)  # ✅ 모델에도 반영하도록 시그널 전송
        else:
            self.titleLabel.setText(self.item.default_title)
            
    def startPathEditing(self, event):
        """✅ QLabel을 더블클릭하면 QLineEdit로 변경"""
        if self.item.downloadState == DownloadState.WAITING:
            if not self.isEditing:
                self.isEditing = True
                self.directoryEdit.setText(self.directoryLabel.text())  # ✅ 현재 값 적용
                self.directoryLabel.setVisible(False)
                self.directoryEdit.setVisible(True)
                self.directoryEdit.setFocus()  # ✅ 포커스 이동

    def finishPathEditing(self):
        """✅ QLineEdit에서 Enter 또는 포커스 해제 시 QLabel로 복귀"""
        self.isEditing = False
        self.directoryEdit.setVisible(False)
        self.directoryLabel.setVisible(True)
        new_path = self.directoryEdit.text().strip()
        if new_path and os.path.exists(new_path):
            self.directoryLabel.setText(new_path)  # ✅ UI 업데이트
            self.item.download_path = new_path  # ✅ 데이터 업데이트
            self.textChanged.emit(new_path)  # ✅ 모델에도 반영하도록 시그널 전송

    def requestDelete(self):
        """✅ 삭제 요청"""
        # print("widget - requestDelete") # Debugging
        self.deleteRequest.emit()

    def requestOpenDir(self):
        try:
            path = self.directoryLabel.text()
            if self.item.downloadState != DownloadState.WAITING:
                path = self.item.output_path
            if os.path.isfile(path):
                nativePath = QDir.toNativeSeparators(path)
                success = False

                if platform.system() == "Windows":
                    success = QProcess.startDetached("explorer.exe", ["/select,", nativePath])
                elif platform.system() == "Linux":
                    success = QProcess.startDetached("nautilus", [nativePath])

                if not success:
                    raise OSError(f"'{path}'을(를) 찾을 수 없습니다.")
            else:
                url = QUrl.fromLocalFile(path)
                if not QDesktopServices.openUrl(url):  # openUrl이 False를 반환하면 실패
                    raise OSError(f"'{path}'을(를) 열 수 없습니다.")
        except Exception as e:
            QMessageBox.warning(self, "경고", str(e))
            return
            

    def sizeHint(self):
        """✅ 위젯 크기 설정"""
        return QSize(450, 130)  # ✅ 너비 450px, 높이 120px
    
    def setSize(self, size):
        try:
            size = float(size)
        except (ValueError, TypeError):
            return "0 B"
        units = ["B", "KB", "MB", "GB", "TB"]
        unit_index = 0
        while size >= 1024 and unit_index < len(units) - 1:
            size /= 1024
            unit_index += 1 
        return f'{size:.2f} {units[unit_index]}'