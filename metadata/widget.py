import os, requests, threading
from PySide6.QtWidgets import QWidget, QFrame, QLabel, QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit, QSizePolicy, QMessageBox
from PySide6.QtGui import QPixmap, QDesktopServices
from PySide6.QtCore import Qt, QSize, Signal, QUrl, QDir, QProcess
from metadata.data import MetadataItem
from download.state import DownloadState
from io import BytesIO
import platform

class MetadataItemWidget(QWidget):
    """✅ 다운로드 메타데이터 정보를 표시하는 커스텀 위젯"""

    textChanged = Signal(str)
    deleteRequest = Signal()

    def __init__(self, item: MetadataItem, index=0, parent=None):
        super().__init__(parent)
        self.item = item  # ✅ MetadataItem 저장
        self.index = index  # ✅ 인덱스 저장
        self.isEditing = False

        # ✅ 전체 배경을 위한 QFrame 추가 (크기 조정 가능하도록 설정)
        self.frame = QFrame(self)
        self.frame.setFrameShape(QFrame.Box)
        self.frame.setFrameShadow(QFrame.Plain)
        self.frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)  # ✅ 가로 확장 설정 

        # ✅ ✅ ✅ **프레임 배경 설정 (내부 위젯이 덮이지 않도록 수정)** ✅ ✅ ✅
        self.frame.setStyleSheet("""
            QFrame {
                background-color: #424242;  /* ✅ 불투명한 배경 */
                border-radius: 8px;  
                padding: 0px;
            }
        """)

        # ✅ 메인 레이아웃 (위젯 전체를 감싸도록 설정)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 0)  # ✅ 마진 제거
        layout.setSpacing(0)  # ✅ 간격 제거
        layout.addWidget(self.frame)  # ✅ 프레임 추가

        self.initUI()
        self.loadImageFromUrl(self.channel_image_label, self.item.channel_image_url, 30, 30)
        self.loadImageFromUrl(self.thumbnail_label, self.item.thumbnail_url, 107, 60)

    def initUI(self):   
        """✅ UI 초기화"""
        # ✅ 프레임 내부 레이아웃 (크기 확장 가능하도록 설정)
        main_layout = QVBoxLayout(self.frame)
        main_layout.setSpacing(0)
        self.frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)  # ✅ 프레임 확장 가능 설정

        # ✅ 상단 정보 (번호, 채널명, 진행 상태)
        top_layout = QHBoxLayout()
        self.index_label = QLabel(f"#{self.index}")
        self.index_label.setStyleSheet("color: white; font-weight: bold;")
        self.index_label.setToolTip("대기열 순서")

        self.channel_image_label = QLabel()
        self.channel_image_label.resize(30, 30)

        self.channel_label = QLabel(self.item.channel_name)
        self.channel_label.setStyleSheet("color: white; font-weight: bold;")
        self.channel_label.setToolTip("채널명")

        self.status_label = QLabel("다운로드 대기")
        self.status_label.setStyleSheet("color: white;")

        self.size_label = QLabel("")
        self.size_label.setStyleSheet("color: white;")

        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet("color: white;")

        self.delete_btn = QPushButton("❌")
        self.delete_btn.setFixedSize(30, 30)
        self.delete_btn.clicked.connect(self.requestDelete)  # ✅ 삭제 요청
        self.delete_btn.setToolTip("삭제")

        top_layout.addWidget(self.index_label)
        top_layout.addWidget(self.channel_image_label)
        top_layout.addWidget(self.channel_label)
        top_layout.addStretch()
        top_layout.addWidget(self.status_label)
        top_layout.addWidget(self.size_label)
        top_layout.addWidget(self.progress_label)
        top_layout.addWidget(self.delete_btn)

        # 중간 정보 (왼쪽 썸네일, 오른쪽 컨텐츠 정보)
        center_layout = QHBoxLayout()

        self.thumbnail_label = QLabel("썸네일")
        center_layout.addWidget(self.thumbnail_label)  # 썸네일은 왼쪽

        # 컨텐츠 정보 (제목, 다운로드 위치 등)
        content_layout = QVBoxLayout()

        # ✅ 중간 (제목, 수정 가능)
        self.title_layout = QHBoxLayout()
        self.title_label = QLabel(self.item.title)
        self.title_label.setStyleSheet("color: white; font-size: 14px;")
        self.title_label.mousePressEvent = self.startTitleEditing
        self.title_label.setToolTip("제목")

        self.title_edit = QLineEdit(self.item.title)
        self.title_edit.setVisible(False)
        self.title_edit.setStyleSheet("font-size: 14px;")
        self.title_edit.editingFinished.connect(self.finishTitleEditing)

        self.buttons = []

        self.title_layout.addWidget(self.title_label)
        self.title_layout.addWidget(self.title_edit, 1)
        self.title_layout.addStretch()

        # ✅ 하단 (파일 경로, 진행 상태, 버튼)
        bottom_layout = QHBoxLayout()
        self.directory_label = QLabel("")
        self.directory_label.setStyleSheet("color: white; font-size: 12px;")
        self.directory_label.mousePressEvent = self.startPathEditing
        self.directory_label.setText(self.item.download_path)
        self.directory_label.setToolTip("다운로드 위치")

        self.directory_edit = QLineEdit(self.item.download_path)
        self.directory_edit.setVisible(False)
        self.directory_edit.setStyleSheet("font-size: 12px;")
        self.directory_edit.editingFinished.connect(self.finishPathEditing)

        self.open_folder_btn = QPushButton("📁")
        self.open_folder_btn.setFixedSize(30, 30)
        self.open_folder_btn.clicked.connect(self.requestOpenDir)  # ✅ 삭제 요청
        self.open_folder_btn.setToolTip("폴더 열기")

        bottom_layout.addWidget(self.directory_label)
        bottom_layout.addWidget(self.directory_edit, 1)
        bottom_layout.addStretch()
        bottom_layout.addWidget(self.open_folder_btn)

        content_layout.addLayout(self.title_layout)
        content_layout.addLayout(bottom_layout)

        center_layout.addLayout(content_layout)

        # ✅ 레이아웃 추가
        main_layout.addLayout(top_layout)
        main_layout.addLayout(center_layout)
        self.frame.setLayout(main_layout)

    def addRepresentationButtons(self):
        """
        해상도 목록(Representation)을 정렬 후, 버튼을 생성해 Resolution 영역에 배치한다.
        """

        for unique_rep in self.item.unique_reps:
            unique_rep.append("Unknown")  # 초기 값 설정

        self.setHeightUrlSize(self.item.unique_reps[-1][1], self.item.unique_reps[-1][2], -1)

        for index, (width, height, base_url, _) in enumerate(self.item.unique_reps):
            self.addRepresentationButton(height, base_url, index)

    def addRepresentationButton(self, height, base_url, index):
        """
        해상도 버튼을 추가하고, 비동기로 파일 사이즈를 헤더에서 가져와 버튼 텍스트를 업데이트한다.
        """
        button = QPushButton(f'{height}p', self)
        button.clicked.connect(lambda: self.setHeightUrlSize(height, base_url, index, button))
        self.title_layout.addWidget(button)
        button.setFixedSize(60, 30)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.buttons.append(button)

        def update_button_text():
            try:
                resp = requests.get(base_url, stream=True)
                resp.raise_for_status()
                size = int(resp.headers.get('content-length', 0))
                
                size_text = self.setSize(size)
                self.item.unique_reps[index][-1] = size_text

                if len(self.item.unique_reps) - 1 == index:
                    self.setHeightUrlSize(height, base_url, index, button)

                button.setToolTip(size_text)
            except Exception:
                pass

        thread = threading.Thread(target=update_button_text, daemon=True)
        thread.start()

    def setHeightUrlSize(self, height, base_url, index=None, button:QPushButton = None):
        if self.item.downloadState == DownloadState.WAITING:
            if button is not None:
                for btn in self.buttons:
                    btn.setEnabled(True)
                button.setDisabled(True)
            self.item.height = height
            self.item.base_url = base_url
            if index is not None:
                self.item.total_size = self.item.unique_reps[index][-1]
                self.size_label.setText(f" {self.item.unique_reps[index][-1]}")

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

    def setData(self, item: MetadataItem, index: int):
        """✅ 모델 데이터를 위젯에 반영"""
        self.item = item
        self.index = index
        self.index_label.setText(f"#{index}")  # ✅ 인덱스 업데이트
        self.channel_label.setText(item.channel_name)
        self.title_label.setText(item.title)
        self.directory_label.setText(item.download_path)

        if self.item.downloadState == DownloadState.WAITING:
            self.status_label.setText(item.stateMessage)
            self.size_label.setText(f" {item.total_size}")
            self.progress_label.setText(" ")

        elif self.item.downloadState == DownloadState.RUNNING:
            self.status_label.setText(f"{item.download_remain_time}  {item.download_speed}")
            self.size_label.setText(f"  {self.setSize(item.download_size)} / {item.total_size}")
            self.progress_label.setText(f"  {item.download_progress}% ")

        elif self.item.downloadState == DownloadState.PAUSED:
            self.status_label.setText("다운로드 정지")
            self.size_label.setText(f"  {self.setSize(item.download_size)} / {item.total_size}")
            self.progress_label.setText(f"  {item.download_progress}% ")

        elif self.item.downloadState == DownloadState.FINISHED:
            self.status_label.setText(f"{item.download_time}")
            self.size_label.setText(f"  {self.setSize(item.download_size)} / {item.total_size}")
            self.progress_label.setText(f"  {item.download_progress}% ")

    def getData(self) -> MetadataItem:
        """✅ 위젯에서 입력된 데이터를 가져와서 MetadataItem으로 반환"""
        return MetadataItem(
            #index=self.item.index,
            channel_name=self.channel_label.text(),
            title=self.title_edit.text(),
            directory=self.directory_label.text(),
            #status=self.status_label.text(),
            progress=self.progress_label.text(),
            #remaining_time=self.remaining_time_label.text(),
            #size_info=self.size_info_label.text(),
            #color=self.item.color
        )

    def startTitleEditing(self, event):
        """✅ QLabel을 더블클릭하면 QLineEdit로 변경"""
        if self.item.downloadState == DownloadState.WAITING:
            if not self.isEditing:
                self.isEditing = True
                self.title_edit.setText(self.title_label.text())  # ✅ 현재 값 적용
                self.title_label.setVisible(False)
                self.title_edit.setVisible(True)
                self.title_edit.setFocus()  # ✅ 포커스 이동

    def finishTitleEditing(self):
        """✅ QLineEdit에서 Enter 또는 포커스 해제 시 QLabel로 복귀"""
        self.isEditing = False
        self.title_edit.setVisible(False)
        self.title_label.setVisible(True)
        new_text = self.title_edit.text().strip()
        if new_text:
            self.title_label.setText(new_text)  # ✅ UI 업데이트
            self.item.title = new_text  # ✅ 데이터 업데이트
            self.textChanged.emit(new_text)  # ✅ 모델에도 반영하도록 시그널 전송
        else:
            self.title_label.setText(self.item.default_title)
            
    def startPathEditing(self, event):
        """✅ QLabel을 더블클릭하면 QLineEdit로 변경"""
        if self.item.downloadState == DownloadState.WAITING:
            if not self.isEditing:
                self.isEditing = True
                self.directory_edit.setText(self.directory_label.text())  # ✅ 현재 값 적용
                self.directory_label.setVisible(False)
                self.directory_edit.setVisible(True)
                self.directory_edit.setFocus()  # ✅ 포커스 이동

    def finishPathEditing(self):
        """✅ QLineEdit에서 Enter 또는 포커스 해제 시 QLabel로 복귀"""
        self.isEditing = False
        self.directory_edit.setVisible(False)
        self.directory_label.setVisible(True)
        new_path = self.directory_edit.text().strip()
        if new_path and os.path.exists(new_path):
            self.directory_label.setText(new_path)  # ✅ UI 업데이트
            self.item.download_path = new_path  # ✅ 데이터 업데이트
            self.textChanged.emit(new_path)  # ✅ 모델에도 반영하도록 시그널 전송

    def requestDelete(self):
        """✅ 삭제 요청"""
        # print("widget - requestDelete") # Debugging
        self.deleteRequest.emit()

    def requestOpenDir(self):
        try:
            path = self.directory_label.text()
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