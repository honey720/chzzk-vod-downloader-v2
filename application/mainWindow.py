import logging
import os
import platform
import config.config as config

from PySide6.QtWidgets import QMainWindow, QMessageBox, QFileDialog, QApplication, QWidget
from PySide6.QtCore import QSize, QStandardPaths, QTimer

from app.viewmodels.download_viewmodel import DownloadViewModel
from app.viewmodels.path_gates import check_fetch_path, check_remember_path, normalize_path
from config.dialog import SettingDialog
from content.data import ContentItem
from content.manager import ContentManager
from content import widget as content_widget
from core.models.download_state import DownloadState
from ui.mainWindow import Ui_VodDownloader
import theme

logger = logging.getLogger(__name__)


def _default_download_path() -> str:
    """시작 시 저장 경로 입력창의 초기값을 정한다 (#159 — #157 실측 근거).

    cwd는 실행 방식에 따라 임의다 — macOS .app을 Finder/Dock으로 실행하면
    cwd가 '/'(쓰기 불가)임을 CI에서 실측했고(#157), Windows도 바로가기의
    '시작 위치' 설정에 좌우된다. 우선순위:
    ① 유저가 마지막으로 쓴 경로(설정, 실존할 때만 — 외장 드라이브 분리 대비)
    ② 시스템 다운로드 폴더(실존할 때만)
    ③ cwd — 소스 실행(개발) 관례를 유지하는 최후 폴백
    """
    saved = config.load_config().get("downloadPath", "")
    if saved and os.path.isdir(saved):
        return saved
    downloads = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
    if downloads and os.path.isdir(downloads):
        return downloads
    return os.getcwd()



class VodDownloader(QMainWindow, Ui_VodDownloader):
    """
    치지직 VOD 다운로더 메인 UI 클래스.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setupUi(self)
        self.setupDynamicUi()

        width_ratio = 0.25
        height_ratio = 0.5
        # 초기 크기와 최소 크기는 다른 관심사다 (#251, 오너 확정).
        #
        # 초기 폭  = 화면 논리폭 × 비율 — "화면에 비해 너무 큰 창을 강요하지 않는다".
        # 최소 폭  = 콘텐츠 최소폭만. "최소"는 더 줄이면 UI가 깨지는 지점이지 화면
        #            크기에 비례할 이유가 없다 — 비율 항을 max()로 섞어 두면 큰 화면에서
        #            최소폭이 콘텐츠 요구보다 올라가 유저가 창을 줄이지 못한다.
        #            콘텐츠 최소는 상수가 아니라 레이아웃에 묻는다(폰트·DPI·번역에 따라
        #            다르다). 창의 중앙 위젯이 스크롤 영역이라 이 최소가 창에 자동으로
        #            전파되지 않으므로(그래야 접근성 배율에서 좌우 스크롤 안전망이
        #            서므로) 명시한다. 작은 화면(800×600)에서 상단 버튼이 프레임 밖으로
        #            넘치던 #249의 결함은 이 바닥으로 그대로 막힌다.
        # 최소 높이 = 화면 논리높이 × 비율 (그대로). ⚠️ 가로와 맞추지 말 것 — 가로가
        #            모자라면 콘텐츠가 잘리지만 세로가 모자라면 카드 개수만 준다. 세로의
        #            레이아웃 최소는 "카드 0장"이라 쓸모의 최소와 크게 달라 별도 판단이
        #            필요하고, 그 판단은 하단바를 걷어낸 뒤로 미룬다(#251).
        #
        # 초기 폭이 콘텐츠 최소보다 작은 화면에서는 Qt가 resize() 요청을 최소 크기로
        # 클램프한다(offscreen·Windows 실기 확인).
        screen = self._screenLogicalSize()
        min_height = int(screen.height() * height_ratio)
        self.setMinimumSize(self._contentMinimumWidth(), min_height)
        self.resize(int(screen.width() * width_ratio), min_height)

        self.contentManager = ContentManager(self.listView)
        # 다운로드 이벤트(진행·완료·실패)는 viewmodel이 content에 직결한다 (#170)
        # — 구 릴레이 슬롯 6개(_onProgress~_onFailed)는 함께 제거됐다
        self.downloadViewModel = DownloadViewModel(self.contentManager, parent=self)
        self.setupThreadSignals()
        self.setupSignals()
        
        self.show()
        
    def setupDynamicUi(self):
        """
        UI 동적 설정을 수행한다.
        """
        self.total_downloads = 0
        self.completed_downloads = 0
        self.downloadPathInput.setText(_default_download_path())  # 초기 경로 (#159)
        # 카드의 "경로는 전역 설정과 다를 때만 표시" 판단 기준(#245) —
        # 시작 시와 경로 변경 시(_rememberPathIfValid)마다 밀어 넣는다
        content_widget.set_global_download_path(self.downloadPathInput.text())
        self.downloadCountLabel.setText(self.downloadCountLabel.text().format(self.completed_downloads, self.total_downloads))  # 초기값 설정
        self._applyLayoutMetrics()

    def _applyLayoutMetrics(self) -> None:
        """상단·카드·하단의 좌우 정렬선을 theme.METRICS 토큰 하나로 관통시킨다 (#244).

        `.ui`(uic 재생성 대상)에는 theme를 연결할 수 없어 여백을 여기서
        런타임에 건다 — 오너가 theme.py의 outerMargin/framePadding 숫자만
        바꾸고 `uv run python main.py`로 바로 확인할 수 있게 하기 위해서다.
        상단바·카드 목록·하단바가 같은 outerMargin에서 시작하고, 바 안쪽
        여백(framePadding)이 카드 안쪽 여백(cardPadding)과 같으면 입력창·
        썸네일·하단 요약의 왼쪽 끝이 한 선에 놓인다.
        """
        outer = theme.METRICS["outerMargin"]
        frame_pad = theme.METRICS["framePadding"]
        self.centralWidgetLayout.setContentsMargins(outer, outer, outer, outer)
        self.centralWidgetLayout.setSpacing(8)
        self.headerFrameLayout.setContentsMargins(frame_pad, frame_pad, frame_pad, frame_pad)
        self.infoLayout.setContentsMargins(frame_pad, frame_pad, frame_pad, frame_pad)
        self._equalizeHeaderButtons()

    def _screenLogicalSize(self) -> QSize:
        """주 화면의 논리 크기 — 초기 폭·최소 높이의 비율 기준. 테스트가 화면 크기를 주입하는 이음새."""
        return QApplication.primaryScreen().size()

    def _contentMinimumWidth(self) -> int:
        """콘텐츠 열이 실제로 요구하는 최소폭 — 레이아웃에 묻는다 (#244 목록/헤더).

        QSS padding·폰트는 polish 시점에 sizeHint에 들어오므로 먼저 polish한다
        (`_equalizeHeaderButtons`와 같은 이유). 상수를 박으면 폰트·DPI·OS·번역에서
        조용히 틀려진다 — offscreen 폰트와 Windows 실기가 이미 다르다(674 vs 406).

        ⚠️ polish만으로는 부족하다. Qt 6는 레이아웃 항목마다 크기 힌트를 캐시하고
        그 위젯의 `updateGeometry()`에서만 비우는데, 첫 표시 전에는 QSS polish가
        글꼴을 바꿔도 상단·하단 바(프레임)의 캐시가 polish 전 값으로 남아
        레이아웃 최소폭이 실제보다 작게 나온다(offscreen 실측 318 — 표시 후 674).
        자식 전부에 `updateGeometry()`를 걸어 캐시를 비우면 표시 후와 같은 값이 된다.
        """
        self.contentColumn.ensurePolished()
        for child in self.contentColumn.findChildren(QWidget):
            child.updateGeometry()
        return self.contentColumn.minimumSizeHint().width()

    def _equalizeHeaderButtons(self) -> None:
        """상단 두 텍스트 버튼([VOD 추가]·[경로 찾기])을 같은 폭으로 고정한다 (#245).

        사람 눈은 **같은 종류끼리**(텍스트 버튼 둘) 끝이 맞는 것을 본다 —
        폭이 다르면 오른쪽 끝을 맞춰도 왼쪽 끝이 어긋나 입력 블록이 사각형으로
        읽히지 않는다. 폭은 번역·폰트에 따라 달라지므로 .ui 상수가 아니라
        런타임에 더 넓은 쪽으로 맞춘다. `ensurePolished()`가 먼저다 — QSS
        padding이 sizeHint에 들어오는 시점이 polish다.
        """
        buttons = (self.fetchButton, self.downloadPathButton)
        for button in buttons:
            button.ensurePolished()
        width = max(button.sizeHint().width() for button in buttons)
        for button in buttons:
            button.setFixedWidth(width)

    def _setLinkStatus(self, text: str, kind: str = "info") -> None:
        """조회 상태 메시지를 갱신한다 — 색은 전역 QSS가 `status` 속성으로 입힌다 (#244).

        kind: "info"(중립 회색) / "ok"(성공 초록) / "error"(실패 빨강).
        속성만 바꾸면 이미 계산된 스타일이 안 갱신되므로 repolish가 함께
        필요하다(카드 상태 표시와 같은 패턴).
        """
        self.linkStatusLabel.setText(text)
        if self.linkStatusLabel.property("status") != kind:
            self.linkStatusLabel.setProperty("status", kind)
            theme.repolish(self.linkStatusLabel)

    def setupThreadSignals(self):
        """
        content 매니저와 UI를 연결하는 시그널 슬롯 설정.

        다운로드 이벤트 배선은 DownloadViewModel 내부로 이동했다 (#170).
        """
        # TODO 동시 다운로드 기능 추가시 로직 수정 필요
        self.contentManager.contentError.connect(self.showErrorDialog)
        self.contentManager.fetchRequested.connect(self.fetchContents)
        self.contentManager.deleteItemRequested.connect(self.onDeleteItem)
        self.contentManager.insertItemRequested.connect(self.onInsertItem)
        self.contentManager.downloadRequested.connect(self.startDownload)
        self.contentManager.stopRequested.connect(self.onStop)
        self.contentManager.finishedRequested.connect(self.onFinishedItem)
        self.contentManager.finishedAllRequested.connect(self.onDownloadAllFinished)

    def setupSignals(self):
        """
        각종 버튼 클릭 시그널 및 UI 내 이벤트를 핸들링할 슬롯을 연결한다.
        """
        self.urlInput.returnPressed.connect(self.onFetch)
        self.fetchButton.clicked.connect(self.onFetch)
        self.downloadPathButton.clicked.connect(self.onFindPath)
        self.downloadPathInput.editingFinished.connect(self._rememberPathIfValid)
        self.settingButton.clicked.connect(self.onSetting)
        self.clearFinishedButton.clicked.connect(self.contentManager.clrearFinishedItems)
        self.downloadButton.clicked.connect(self.onDownloadPause)
        self.stopButton.clicked.connect(self.onStop)
        # 카드 상태별 조작(#245) — ⏸/↻ 는 뷰가 아이템을 붙여 올려준다
        self.listView.pauseRequested.connect(self.onCardPause)
        self.listView.retryRequested.connect(self.onCardRetry)

    def onCardPause(self, item: ContentItem) -> None:
        """진행 카드의 ⏸ — 전역 일시정지/재개 토글과 같은 경로를 탄다 (#245).

        엔진은 동시 다운로드가 하나뿐이라(주석 TODO — 동시 다운로드
        미지원) 카드의 일시정지는 곧 전역 일시정지다. 다운로드 중이
        아닐 때는 아무 일도 하지 않는다 — onDownloadPause의 시작 분기
        (새 배치 시작)로 새면 안 된다.
        """
        if self.downloadViewModel.isDownloading():
            self.onDownloadPause()

    def onCardRetry(self, item: ContentItem) -> None:
        """실패 카드의 ↻ — 아이템을 대기로 되돌리고 배치를 잇는다 (#245).

        실패 상태·사유·진행률을 초기화해 다시 다운로드 대상(findItem이
        잡는 WAITING)으로 만든다. 이미 배치가 돌고 있으면 현재 항목이
        끝난 뒤 체인(emitFinishedRequest → downloadItem)이 이 아이템을
        집어 가고, 놀고 있으면 다운로드 버튼과 같은 경로로 즉시 배치를
        시작한다 — 재시도 전용 실행 경로를 새로 만들지 않는다.
        """
        item.stateMessage = ""
        item.downloadState = DownloadState.WAITING
        item.download_progress = 0
        self.contentManager.model.notifyChanged(item)
        if not self.downloadViewModel.isDownloading():
            self.onDownloadPause()

    def fetchContents(self, urls: str):
        # URL 목록을 미리 준비합니다.
        self.urlsToFetch = [url.strip() for url in urls.splitlines() if url.strip() != '']
        self.currentUrlIndex = 0
        self.scheduleNextFetch()

    def scheduleNextFetch(self):
        if self.currentUrlIndex < len(self.urlsToFetch):
            # 다음 URL을 가져와 처리합니다.
            url = self.urlsToFetch[self.currentUrlIndex]
            self.currentUrlIndex += 1
            self.onFetch(url)
            # 0.1초(100밀리초) 후에 다음 작업을 스케줄합니다.
            QTimer.singleShot(100, self.scheduleNextFetch)

    def onFetch(self, url = None):
        """
        VOD URL을 입력받아 메타데이터를 가져오고, 메타데이터 카드를 생성합니다.
        """
        if url:
            vod_url = url
        else:
            vod_url = self.urlInput.text().strip()

        if not vod_url:
            QMessageBox.warning(self, self.tr("Warning"), self.tr("Please enter VOD URL."))
            return
        
        cookies = config.load_cookies()  # 쿠키 조립의 단일 지점 (#170)
        self._setLinkStatus(self.tr('Fetching resolutions...'), "info")

        # 결과 처리 — 상대 경로 입력은 cwd 기준으로 조용히 저장되던 문제를
        # 판정 전에 정규화해 막는다 (#146 ⓑ-4, #219). 화면 표시도 실제
        # 사용값과 맞춘다 — onFindPath가 다이얼로그 결과를 setText하는 것과
        # 같은 관례
        downloadPath = normalize_path(self.downloadPathInput.text().strip() or os.getcwd())
        self.downloadPathInput.setText(downloadPath)

        # 판정은 path_gates가 단일 지점으로 담당한다 (#169 — #146 ⓑ1)
        if not check_fetch_path(downloadPath):
            # 유일한 안내가 팝업뿐이라 제보 진단이 불가능했다 (#146 감사) —
            # 입력값을 repr로 남겨 공백 유사 문자·오염(따옴표 등)을 식별한다 (#148)
            logger.warning("조회 거부 — 존재하지 않는 저장 경로: %r", downloadPath)
            QMessageBox.warning(self, self.tr("Warning"), self.tr("Path does not exist."))
            return
        # TODO:  코드 수정 및 테스트 예정
        # 검증을 통과해 실사용되는 경로만 보존한다 — 다음 실행의 초기값 ① (#159)
        self._rememberDownloadPath(downloadPath)

        self.contentManager.fetchContent(vod_url, cookies, downloadPath)

        self.urlInput.clear()

        self._setLinkStatus(self.tr('Resolutions fetched successfully.'), "ok")
    
    def showErrorDialog(self, errorMessage):
        errorTitle = self.tr("Error")
        errorBody = self.tr("Error occurred during content request:") + "\n" + errorMessage
        # 팝업을 닫은 뒤에도 실패했다는 사실이 남게 상태 줄도 빨갛게 바꾼다
        # (#244 — 성공 메시지만 남아 "성공했는데 카드가 없다"로 읽히던 문제).
        self._setLinkStatus(self.tr('Failed to fetch resolutions.'), "error")
        QMessageBox.critical(self, errorTitle, errorBody)

    def onDownloadPause(self):
        """
        추가한 동영상에 대한 다운로드 버튼.
        """
        if self.downloadViewModel.isDownloading():
            if self.downloadButton.text() == self.tr('Pause'):
                self.downloadViewModel.pause()
                self.downloadButton.setText(self.tr('Download'))
            else:
                self.downloadViewModel.resume()
                self.downloadButton.setText(self.tr('Pause'))
        else:
            if not self.contentManager.findItem()[0]:
                # 조회 중인 아이템만 있는 경우와 아무것도 없는 경우를 구분해 안내 (#124)
                if self.contentManager.hasLoadingItems():
                    QMessageBox.warning(self, self.tr("Warning"), self.tr("Still loading video information. Please try again in a moment."))
                else:
                    QMessageBox.warning(self, self.tr("Warning"), self.tr("No VODs added."))
                return
            self.downloadButton.setText(self.tr('Pause'))
            self.contentManager.downloadItem()
    
    def onSetting(self):
        """
        설정 파일에 저장된 값 불러오기 버튼.
        """
        self.dialog = SettingDialog(parent=self)
        self.dialog.exec_()

    def onFindPath(self):
        """
        다운로드 경로를 찾기위한 버튼.
        """
        downloadPath = QFileDialog.getExistingDirectory()
        if downloadPath != '':
            # 유저의 경로 지정 행위를 남긴다 (#148) — 제보 시 "무엇이 입력창에
            # 들어갔는가"를 로그로 재구성할 수 있게 한다
            logger.info("저장 경로 선택(경로 찾기): %r", downloadPath)
            self.downloadPathInput.setText(downloadPath)
            self._rememberPathIfValid()

    def _rememberPathIfValid(self) -> None:
        """입력창의 경로가 실존 폴더면 보존한다 (#165).

        보존 시점은 유저의 의사표시 세 곳 — 경로 찾기 선택, 입력 확정
        (editingFinished), 창 닫기 — 이라 조회 없이 경로만 바꿔도 다음
        실행의 초기값 ①이 된다. isdir 관문(판정은 path_gates 단일 지점,
        #169)은 커밋된 미완성·오타 경로가 초기값을 오염시키지 않게 한다.
        """
        path = normalize_path(self.downloadPathInput.text().strip())
        if check_remember_path(path):
            self.downloadPathInput.setText(path)
            self._rememberDownloadPath(path)
            content_widget.set_global_download_path(path)  # 카드 경로 표시 기준 갱신 (#245)

    def _rememberDownloadPath(self, path: str) -> None:
        """실사용된 저장 경로를 설정에 보존한다 (#159) — _default_download_path의 ①."""
        cfg = config.load_config()
        if cfg.get("downloadPath") != path:
            cfg["downloadPath"] = path
            config.save_config(cfg)
            logger.info("저장 경로 보존: %r", path)

    def onStop(self):
        """
        중지 버튼 콜백.
        """
        if self.downloadViewModel.task:
            reply = QMessageBox.warning(
                self,
                self.tr("Downloading"),
                self.tr("Download is in progress. Do you want to quit?"),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            #TODO: 메시지박스 중복 경고
            if reply == QMessageBox.Yes:
                self.stopDownload()

    def stopDownload(self):
        self.downloadViewModel.stop()
        self.downloadButton.setText(self.tr('Download'))
        self.setStopButtonEnable(False)
        self.downloadViewModel.removeThreads()

    def onInsertItem(self, row):
        self.total_downloads = row
        self.updateDownloadCountLabel()

    def onDeleteItem(self, item:ContentItem, index):
        """
        메타데이터 카드 삭제 버튼 콜백.
        """
        if item.downloadState == DownloadState.FINISHED:
            self.completed_downloads -= 1
        self.total_downloads = index
        self.updateDownloadCountLabel()
    
    def onFinishedItem(self, item:ContentItem):
        """
        메타데이터 카드 다운로드 완료 콜백.
        """
        if item.downloadState == DownloadState.FINISHED:
            self.completed_downloads += 1
            self.updateDownloadCountLabel()

    def onDownloadAllFinished(self):
        """
        모든 메타데이터 카드 다운로드 완료 콜백.
        """
        self.downloadButton.setText(self.tr('Download'))
        self.setStopButtonEnable(False)
        
        os_type = platform.system()

        afterDownload = config.load_config().get('afterDownload')

        if afterDownload == "sleep":
            if os_type == "Windows":
                os.system("rundll32.exe powrprof.dll,SetSuspendState 0,1,0")
            elif os_type == "Darwin":
                os.system("pmset sleepnow")
            elif os_type == "Linux":
                os.system("systemctl suspend")
            else:
                logger.warning(f"절전 모드는 {os_type}에서 지원되지 않습니다.")
        elif afterDownload == "shutdown":
            if os_type == "Windows":
                os.system("shutdown -s -t 0")
            elif os_type == "Darwin":
                os.system("osascript -e 'tell app \"System Events\" to shut down'")
            elif os_type == "Linux":
                os.system("shutdown -h now")
            else:
                logger.warning(f"시스템 종료는 {os_type}에서 지원되지 않습니다.")

        # 배치 종료 안내는 화면의 결과와 모순되지 않아야 한다 (#134 — #128 후속 ⑤).
        # 실패 카드가 보이는데 "완료했습니다"만 뜨던 문구를 결과별로 나눈다
        finished, failed = self.contentManager.downloadResultCounts()
        if failed and not finished:
            QMessageBox.warning(
                self,
                self.tr("Completed"),
                self.tr("All downloads failed. Check the failed cards for the reason."),
            )
        elif failed:
            QMessageBox.warning(
                self,
                self.tr("Completed"),
                self.tr("Download finished, but some items failed. Check the failed cards for the reason."),
            )
        else:
            QMessageBox.information(self, self.tr("Completed"), self.tr("Download completed."))

    def setStopButtonEnable(self, bool):
        self.stopButton.setEnabled(bool)

    # ============ 다운로드 진행 준비 및 상태 업데이트 ============

    def startDownload(self, item:ContentItem):
        """
        특정 해상도에 대한 다운로드 스레드를 생성 및 시작하기 전 UI 상태 업데이트를 수행한다.
        """
        self.contentManager.start(item)
        self.downloadViewModel.start(item)
        self.setStopButtonEnable(True)

    def updateDownloadCountLabel(self):
        """
        다운로드 갯수 라벨을 업데이트한다.
        """
        self.downloadCountLabel.setText(self.tr('Downloads: {}/{}').format(self.completed_downloads, self.total_downloads))

    def closeEvent(self, event):
        """
        창을 닫을 때 실행되는 이벤트
        """
        if self.downloadViewModel.isDownloading():
            reply = QMessageBox.warning(
                self,
                self.tr("Downloading"),
                self.tr("Download is in progress. Do you want to quit?"),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply == QMessageBox.No:
                event.ignore()  # 창 닫기 취소
                return
            else:
                self.stopDownload()
        # 확정(포커스 이탈) 없이 바로 닫는 경우의 보존 (#165)
        self._rememberPathIfValid()
        event.accept()  # 창 닫기 진행