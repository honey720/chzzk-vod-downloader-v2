import logging
import os
import platform
import config.config as config

from PySide6.QtWidgets import QMainWindow, QMessageBox, QFileDialog, QApplication
from PySide6.QtCore import QStandardPaths, QTimer

from app.viewmodels.path_gates import check_fetch_path, check_remember_path
from config.dialog import SettingDialog
from content.data import ContentItem
from content.manager import ContentManager
from download.manager import DownloadManager
from download.state import DownloadState
from ui.mainWindow import Ui_VodDownloader

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
        min_width = int(QApplication.primaryScreen().size().width() * width_ratio)
        min_height = int(QApplication.primaryScreen().size().height() * height_ratio)
        self.setMinimumSize(min_width, min_height)
        self.resize(min_width, min_height)

        self.contentManager = ContentManager(self.listView)
        self.downloadManager = DownloadManager()
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
        self.downloadCountLabel.setText(self.downloadCountLabel.text().format(self.completed_downloads, self.total_downloads))  # 초기값 설정

    def setupThreadSignals(self):
        """
        다운로드 스레드와 UI를 연결하는 시그널 슬롯 설정.
        """
        self.downloadManager.progress.connect(self._onProgress)
        self.downloadManager.paused.connect(self._onPaused)
        self.downloadManager.resumed.connect(self._onResumed)
        self.downloadManager.stopped.connect(self._onStopped)
        self.downloadManager.finished.connect(self._onFinished)
        self.downloadManager.failed.connect(self._onFailed)
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
        
        data = config.load_config().get("cookies", {})
        cookies = {
            'NID_AUT': data.get("NID_AUT", ""),
            'NID_SES': data.get("NID_SES", "")
        }
        self.linkStatusLabel.setText(self.tr('Fetching resolutions...'))

        # 결과 처리
        downloadPath = self.downloadPathInput.text().strip() or os.getcwd()

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

        self.linkStatusLabel.setText(self.tr('Resolutions fetched successfully.'))
    
    def showErrorDialog(self, errorMessage):
        errorTitle = self.tr("Error")
        errorBody = self.tr("Error occurred during content request:") + "\n" + errorMessage
        QMessageBox.critical(self, errorTitle, errorBody)

    def onDownloadPause(self):
        """
        추가한 동영상에 대한 다운로드 버튼.
        """
        if self.downloadManager.d_thread:
            if self.downloadButton.text() == self.tr('Pause'):
                self.downloadManager.pause()
                self.downloadButton.setText(self.tr('Download'))
            else:
                self.downloadManager.resume()
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
        path = self.downloadPathInput.text().strip()
        if check_remember_path(path):
            self._rememberDownloadPath(path)

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
        if self.downloadManager.task:
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
        self.downloadManager.stop()
        self.downloadButton.setText(self.tr('Download'))
        self.setStopButtonEnable(False)
        self.downloadManager.removeThreads()

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
        self.downloadManager.start(item)
        self.setStopButtonEnable(True)

    def _onProgress(self, rem, size, spd, prog, item):
        """
        progress 시그널이 발생할 때마다, 지금 다운로드 중인 item 업데이트.
        """
        self.contentManager.update_progress(rem, size, spd, prog, item)

    def _onPaused(self, item):
        self.contentManager.pause(item)

    def _onResumed(self, item):
        self.contentManager.resume(item)

    def _onStopped(self, item):
        self.contentManager.stop(item)

    def _onFinished(self, item, download_time):
        self.contentManager.finish(item, download_time)

    def _onFailed(self, item, message):
        """다운로드 실패 콜백 — 카드에 실패 상태·사유를 표시하고 배치를 계속 진행한다 (#134)."""
        self.contentManager.fail(item, message)

    def updateDownloadCountLabel(self):
        """
        다운로드 갯수 라벨을 업데이트한다.
        """
        self.downloadCountLabel.setText(self.tr('Downloads: {}/{}').format(self.completed_downloads, self.total_downloads))

    def closeEvent(self, event):
        """
        창을 닫을 때 실행되는 이벤트
        """
        if self.downloadManager.d_thread or self.downloadManager.m_thread:
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