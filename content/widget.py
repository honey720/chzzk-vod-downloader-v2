import os
import threading
from PySide6.QtWidgets import QWidget, QPushButton, QMessageBox
from PySide6.QtGui import QPixmap, QDesktopServices
from PySide6.QtCore import Qt, QSize, Signal, QUrl, QDir, QProcess
from content.data import ContentItem
from content.network import REQUEST_TIMEOUT, get_thread_session
from core.models.download_state import DownloadState
from app.viewmodels.item_state import ItemState
from app.viewmodels.path_gates import check_card_edit_path
from ui.contentItemWidget import Ui_ContentItemWidget
import theme
from time import strftime, gmtime
import platform
import logging

logger = logging.getLogger(__name__)

#: 상태별 아이콘 글리프(#244) — 카드 테두리색이 하던 상태 신호 역할을 이
#: 아이콘으로 옮긴다. 색은 파이썬이 아니라 전역 QSS
#: `#stateIconLabel[state="..."]`가 theme.py 토큰으로 정한다.
STATE_ICON = {
    "waiting": "⏸",   # ⏸
    "running": "▶",   # ▶
    "finished": "✓",  # ✓
    "failed": "✕",    # ✕
}

class ContentItemWidget(QWidget, Ui_ContentItemWidget):
    """컨텐츠 정보를 표시하는 커스텀 위젯"""

    textChanged = Signal(str)
    deleteRequest = Signal()

    # 워커 스레드 → 메인 스레드 중계 (#168). 위젯·아이템 조작은 반드시 메인
    # 스레드 슬롯에서 한다 — download 경로가 qt_bridge로 세운 스레드 경계
    # 규칙을 content 경로에도 적용한다
    _repSizeFetched = Signal(int, str)  # (해상도 index, 크기 텍스트 — 세그먼트 기반이면 "")
    _imageFetched = Signal(object, object, str, int, str)  # (label, bytes, url, maxHeight, type)

    def __init__(self, item: ContentItem, index=0, parent=None):
        super().__init__(parent)
        self.item = item  # ContentItem 저장
        self.index = index  # 인덱스 저장
        self.isEditing = False
        self.setupUi(self)
        self._setupThreadRelays()  # setupDynamicUi가 스레드를 띄우기 전에 연결돼야 한다 (#168)
        self.setupDynamicUi()
        self.setupSignals()  # 시그널 연결

    def _setupThreadRelays(self):
        self._repSizeFetched.connect(self._onRepSizeFetched)
        self._imageFetched.connect(self._onImageFetched)

    def setIndex(self, index: int) -> None:
        """카드 번호 라벨만 갱신한다 (#235).

        `setData()`는 채널명·제목·경로·상태 문구까지 전부 다시 채운다 —
        삭제 후 뒤쪽 카드들의 번호만 당길 때 그 카드들 전부에 `setData()`를
        돌리면 매번 무관한 필드까지 다시 쓰고 `applyStateStyle()`까지
        불필요하게 재실행된다(카드 수만큼 쌓이면 그 자체가 비용이다).
        번호만 바뀐 카드는 이 메서드로 라벨 하나만 건드린다.
        """
        self.index = index
        self.indexLabel.setText(f"#{index}")

    def setupDynamicUi(self):
        self.loadImageFromUrl(self.channelImageLabel, self.item.channel_image_url, 26, "channel")
        self.loadImageFromUrl(self.thumbnailLabel, self.item.thumbnail_url, 64, "thumbnail")
        self.contentTypeLabel.setText(self.item.content_type) # 콘텐츠 타입 업데이트
        self.channelNameLabel.setText(self.item.channel_name) # 채널 이름 업데이트
        self.progressLabel.setText("") # 진행률 업데이트
        self.deleteButton.setText("❌")
        self.setIndex(self.index)  # 인덱스 업데이트
        self.titleLabel.setText(self.item.title) # 제목 업데이트
        self.titleEdit.setText(self.item.title) # 제목 업데이트
        self.titleEdit.setVisible(False) # 제목 수정용 QLineEdit 숨김
        self.directoryLabel.setText(self.item.download_path) # 다운로드 경로 업데이트
        self.directoryEdit.setText(self.item.download_path) # 다운로드 경로 업데이트
        self.directoryEdit.setVisible(False) # 다운로드 경로 수정용 QLineEdit 숨김
        self.openDirectoryButton.setText("📁")
        self.applyStateStyle()  # setData 전에도 카드가 무스타일로 보이지 않게 (#227)

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
        # LOADING 자리표시 아이템은 해상도 목록이 아직 없다 (#124)
        if not self.item.unique_reps:
            return
        for unique_rep in self.item.unique_reps:
            # 크기 조회가 끝나기 전 표시 — "Unknown"은 실패로 읽혀 "확인 중"으로 표기 (#124)
            unique_rep.append(self.tr("Checking..."))  # 초기 값 설정

        self.setresolutionUrlSize(self.item.unique_reps[-1][0], self.item.unique_reps[-1][1], -1)

        for index, (resolution, base_url, _) in enumerate(self.item.unique_reps):
            self.addRepresentationButton(resolution, base_url, index)

    def addRepresentationButton(self, resolution, base_url, index):
        """
        해상도 버튼을 추가하고, 비동기로 파일 사이즈를 헤더에서 가져와 버튼 텍스트를 업데이트한다.
        """
        button = QPushButton(f'{resolution}p', self)
        button.clicked.connect(lambda: self.setresolutionUrlSize(resolution, base_url, index, button))
        # pill 모양·선택 표시는 전역 QSS의 [role="resolution"] 규칙이 그린다 (#227).
        # QSS는 `.className` 선택자를 지원하지 않아 조용히 무시하므로, 동적
        # 속성을 심어 속성 선택자로 잡는 게 유일한 방법이다
        button.setProperty("role", "resolution")
        # 제목과 한 줄을 공유하지 않는다(#244) — 제목·해상도는 서로 무관한
        # 정보라 titleLayout에 얹으면 "무관한 정보가 한 줄에 섞인다"는
        # 문제였다. 전용 줄(resolutionLayout)에 넣는다.
        self.resolutionLayout.addWidget(button)
        button.setFixedSize(60, 30)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.buttons.append(button)

        def update_button_text():
            # 네트워크만 담당한다 — 위젯·아이템 반영은 _onRepSizeFetched(메인 스레드)로 (#168)
            size_text = ""
            if not self.item.is_segment_based:
                try:
                    resp = get_thread_session().head(base_url, timeout=REQUEST_TIMEOUT)
                    resp.raise_for_status()
                    size = int(resp.headers.get('content-length', 0))
                    if size == 0:
                        resp = get_thread_session().get(base_url, stream=True, timeout=REQUEST_TIMEOUT)
                        resp.raise_for_status()
                        size = int(resp.headers.get('content-length', 0))
                        resp.close()
                    size_text = self.setSize(size)
                except Exception:
                    # 기존 동작 유지: 조회 실패는 조용히 "Checking..."으로 남는다
                    return
            try:
                self._repSizeFetched.emit(index, size_text)
            except RuntimeError:
                pass  # 위젯이 이미 파괴된 뒤의 늦은 완료

        thread = threading.Thread(target=update_button_text, daemon=True)
        thread.start()

    def _onRepSizeFetched(self, index, size_text):
        """해상도 크기 조회 결과를 위젯·아이템에 반영한다 — 메인 스레드 (#168)."""
        if index >= len(self.buttons) or index >= len(self.item.unique_reps):
            return  # setData로 아이템이 교체된 뒤의 늦은 완료
        if size_text:
            self.item.unique_reps[index][-1] = size_text
            self.buttons[index].setToolTip(size_text)
        if len(self.item.unique_reps) - 1 == index:
            resolution, base_url = self.item.unique_reps[index][0], self.item.unique_reps[index][1]
            self.setresolutionUrlSize(resolution, base_url, index, self.buttons[index])

    def setresolutionUrlSize(self, resolution, base_url, index=None, button:QPushButton = None):
        if self.item.downloadState == DownloadState.WAITING:
            if button is not None:
                for btn in self.buttons:
                    btn.setEnabled(True)
                button.setDisabled(True)
            self.item.resolution = resolution
            self.item.base_url = base_url
            # 세그먼트 기반(m3u8·hls_aes)은 total_size를 미리 알 수 없어 처리하지 않음
            if not self.item.is_segment_based and index is not None:
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
        """이미지 바이트를 받아 메인 스레드로 중계한다 — 워커 스레드 (#168).

        QPixmap 생성·스케일·setPixmap은 GUI 스레드 전용이라 여기서 하지
        않는다. 디코드 이후는 _onImageFetched(메인 스레드)가 담당한다.
        """
        try:
            response = get_thread_session().get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            self._imageFetched.emit(label, response.content, url, maxHeight, type)
        except RuntimeError:
            pass  # 위젯이 이미 파괴된 뒤의 늦은 완료
        except Exception as e:
            logger.error(f"Error loading image from {url}: {e}")

    def _onImageFetched(self, label, data, url, maxHeight, type):
        """이미지 디코드·스케일·표시 — 메인 스레드 (#168)."""
        try:
            image = QPixmap()
            image.loadFromData(data)

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
            logger.error(f"Error loading image from {url}: {e}")

    def setData(self, item: ContentItem, index: int):
        """✅ 모델 데이터를 위젯에 반영"""
        self.item = item
        self.setIndex(index)
        self.channelNameLabel.setText(item.channel_name)
        self.titleLabel.setText(item.title)
        self.directoryLabel.setText(item.download_path)

        if self.item.downloadState == ItemState.LOADING:
            self.statusLabel.setText(self.tr("Loading information..."))
            self.fileSizeLabel.setText("")
            self.progressLabel.setText(" ")

        elif self.item.downloadState == DownloadState.WAITING:
            self.statusLabel.setText(self.tr("Download waiting"))
            if self.item.is_segment_based:
                self.fileSizeLabel.setText(f" {strftime('%H:%M:%S', gmtime(item.duration))}")
            else:
                self.fileSizeLabel.setText(f" {item.total_size}")
            self.progressLabel.setText(" ")

        elif self.item.downloadState == DownloadState.RUNNING:
            self.statusLabel.setText(f"{item.download_remain_time}  {item.download_speed}")
            if self.item.is_segment_based:
                if self.item.post_process:
                    self.statusLabel.setText("Post-processing")
                self.fileSizeLabel.setText(f"  {self.setSize(item.download_size)}")
            else:
                self.fileSizeLabel.setText(f"  {self.setSize(item.download_size)} / {item.total_size}")
            self.progressLabel.setText(f"  {item.download_progress}% ")

        elif self.item.downloadState == DownloadState.PAUSED:
            self.statusLabel.setText(self.tr("Download paused"))
            if self.item.is_segment_based:
                self.fileSizeLabel.setText(f"  {self.setSize(item.download_size)}")
            else:
                self.fileSizeLabel.setText(f"  {self.setSize(item.download_size)} / {item.total_size}")
            self.progressLabel.setText(f"  {item.download_progress}% ")

        elif self.item.downloadState == DownloadState.FINISHED:
            self.statusLabel.setText(f"{item.download_time}")
            self.fileSizeLabel.setText(f"  {self.setSize(item.download_size)}")
            self.progressLabel.setText(f"  {item.download_progress}% ")

        elif self.item.downloadState == DownloadState.FAILED:
            # 실패는 대기("Download waiting")와 구분되는 상태로 표시한다 (#134).
            # 사유(stateMessage)는 키 기반 매핑을 거친 번역 문자열만 온다
            text = self.tr("Download failed")
            reason = getattr(item, "stateMessage", "")
            if reason:
                text = f"{text} — {reason}"
            self.statusLabel.setText(text)
            self.statusLabel.setToolTip(reason)
            self.progressLabel.setText(" ")

        self.applyStateStyle()

    def applyStateStyle(self):
        """상태 아이콘·진행바를 현재 다운로드 상태의 색으로 맞춘다 (#227, #240, #244 후속).

        색은 theme.py 한 곳에서만 정의된다. 상태 아이콘·진행바 둘 다
        위젯별 `setStyleSheet` 없이 동적 속성(`state`)만 바꾸고, 색은
        전역 QSS의 `#stateIconLabel[state="..."]`/`QProgressBar[state="..."]`
        규칙이 고른다 — 속성만 바꾸면 이미 계산된 스타일이 갱신되지
        않으므로 theme.repolish()가 항상 함께 필요하다.

        카드 테두리(#contentFrame)는 더는 상태색을 안 쓴다 — "테두리색·
        진행바색·상태 텍스트"로 상태를 세 번 반복해 알리던 것을 "상태
        아이콘·진행바색" 두 가지로 줄이는 오너 확정 결정(#244)이다.
        """
        state = self._cardState()
        self.progressBar.setValue(self._progressValue())
        if self.progressBar.property("state") != state:
            self.progressBar.setProperty("state", state)
            theme.repolish(self.progressBar)
        if self.stateIconLabel.property("state") != state:
            self.stateIconLabel.setText(STATE_ICON[state])
            self.stateIconLabel.setProperty("state", state)
            theme.repolish(self.stateIconLabel)

    def _cardState(self) -> str:
        """현재 아이템 상태를 카드 상태 어휘(theme.CARD_STATES)로 옮긴다.

        PAUSED(정지)는 대기와 같은 회색으로 둔다 — 유저가 멈춘 것이지
        진행 중도 실패도 아니다(기존 표시 문구도 "Download paused"로
        대기 계열이다). LOADING(조회 중)도 마찬가지다.
        """
        state = self.item.downloadState
        if state == DownloadState.RUNNING:
            return "running"
        if state == DownloadState.FINISHED:
            return "finished"
        if state == DownloadState.FAILED:
            return "failed"
        return "waiting"

    def _progressValue(self) -> int:
        """진행바에 넣을 0~100 값. 아직 시작 전(대기·조회 중)이면 0이다."""
        if self.item.downloadState in (DownloadState.WAITING, ItemState.LOADING):
            return 0
        try:
            return max(0, min(100, int(self.item.download_progress)))
        except (TypeError, ValueError):
            return 0

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
        """✅ QLineEdit에서 Enter 또는 포커스 해제 시 QLabel로 복귀

        존재하지 않는 경로는 기존처럼 반영하지 않되, 아무 표시 없이 무시하던
        것을 안내·로그로 남긴다 (#148 — #146 감사의 무피드백 지점).
        """
        if not self.isEditing:
            # returnPressed와 포커스 이탈이 editingFinished를 연달아 낼 수 있다 —
            # 첫 종료만 처리해 거부 안내가 중복되지 않게 한다
            return
        self.isEditing = False
        self.directoryEdit.setVisible(False)
        self.directoryLabel.setVisible(True)
        new_path = self.directoryEdit.text().strip()
        # 판정은 path_gates가 단일 지점으로 담당한다 (#169 — #146 ⓑ1)
        if check_card_edit_path(new_path):
            self.directoryLabel.setText(new_path)  # ✅ UI 업데이트
            self.item.download_path = new_path  # ✅ 데이터 업데이트
            self.textChanged.emit(new_path)  # ✅ 모델에도 반영하도록 시그널 전송
            logger.info("카드 저장 경로 변경: %r", new_path)
        elif new_path and new_path != self.item.download_path:
            # 경로는 repr로 남긴다 — 공백 유사 문자(U+00A0 등)·오염(따옴표 등)을
            # 육안 구분할 수 있는 유일한 표기다 (#144 실측)
            logger.warning("카드 저장 경로 거부 — 존재하지 않음: %r", new_path)
            QMessageBox.warning(self, self.tr("Warning"), self.tr("Path does not exist."))

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
                elif platform.system() == "Darwin":
                    success = QProcess.startDetached("open", ["-R", nativePath])
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
        return QSize(450, 130)  # ✅ 너비 450px, 높이 130px(#244 카드 압축 실측치)
    
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