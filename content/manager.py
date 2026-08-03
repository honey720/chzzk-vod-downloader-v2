import logging
import os
import tempfile
import threading

from PySide6.QtCore import Qt, Signal, QThreadPool, QObject
from content.model import ContentListModel
from content.view import ContentListView
from content.delegate import ContentListDelegate
from content.data import ContentItem
from download.state import DownloadState
from content.worker import ContentWorker
from core.utils.paths import build_output_path, ensure_unique_path

logger = logging.getLogger(__name__)

# 쓰기 프로브 대기 상한(초) (#137). 정상 디스크에서 프로브는 밀리초 수준이라
# 이 값은 병리 상황(무응답 마운트 — #136)에서만 발동한다. 발동 시 메인 스레드가
# 이 시간만큼 기다리는 대가가 있지만, 무한 정지(0B 침묵) 대신 유한 대기 후
# 명확한 실패가 목적이다
_WRITE_PROBE_TIMEOUT_S = 5.0


def probe_writable(directory: str, timeout_s: float = _WRITE_PROBE_TIMEOUT_S) -> tuple[bool, str]:
    """저장 경로의 존재·쓰기 가능 여부를 제물 스레드로 검사한다 (#137 — #136 제안 ②).

    존재 검사(os.path.isdir)조차 무응답 마운트에서는 매달릴 수 있어, 검사
    전체를 별도 스레드에서 수행하고 join(timeout)으로 포기한다 — 파이썬에
    파일 I/O 시간 제한 수단이 없다는 조사(#136)의 상한 적용이다. 갇힌
    스레드는 회수할 수 없지만(데몬), 프로브는 다운로드 시작 시점 1회뿐인
    작고 드문 지점이라 누수 비용이 유계다.

    Returns:
        (쓰기 가능 여부, 사유): 사유는 "" | "missing" | "denied" | "timeout"
    """
    outcome: dict[str, str] = {}

    def probe() -> None:
        try:
            if not os.path.isdir(directory):
                outcome["reason"] = "missing"
                return
            # 실제 파일 생성·삭제로 확인한다 — os.access는 네트워크 파일시스템의
            # 권한(예: SFTP 상 ZFS 풀 루트)을 신뢰할 수 없다
            fd, probe_path = tempfile.mkstemp(prefix=".cvdv2_probe_", dir=directory)
            os.close(fd)
            os.remove(probe_path)
            outcome["reason"] = ""
        except OSError:
            outcome["reason"] = "denied"

    worker = threading.Thread(target=probe, daemon=True, name="WriteProbe")
    worker.start()
    worker.join(timeout_s)
    reason = outcome.get("reason", "timeout")
    return reason == "", reason


class ContentManager(QObject):
    # 메타데이터 매니저 UI
    downloadRequested = Signal(object)
    stopRequested = Signal(object)
    insertItemRequested = Signal(int)
    deleteItemRequested = Signal(object, int)
    finishedRequested = Signal(object)
    finishedAllRequested = Signal()
    fetchRequested = Signal(str)
    contentError = Signal(str)  # ✅ UI에서 오류를 처리할 수 있도록 signal 추가

    def __init__(self, view: ContentListView, parent = None):
        super().__init__(parent)
        
        self.view = view
        self.model = ContentListModel()
        self.view.setModel(self.model)
        self.view.setItemDelegate(ContentListDelegate())
        self.view.deleteRequest.connect(self.removeItem)
        self.view.fetchRequested.connect(self.fetchReuest)

        self.downloadPath = ""
        self.threadpool = QThreadPool()
        # 조회 중인 워커 → 자리표시 아이템. 결과가 도착할 때까지 워커의 파이썬
        # 참조를 잡아 두는 역할도 한다 — 참조가 없으면 run() 종료 직후 워커가
        # 파괴되어 큐에 남은 finished/error 전달이 유실된다 (#124)
        self._pendingPlaceholders = {}

    def fetchReuest(self, urls):
        self.fetchRequested.emit(urls)

    def fetchContent(self, vod_url: str, cookies: dict, downloadPath: str) -> None:
        # 조회가 끝나기 전에도 카드가 보이도록 LOADING 상태의 자리표시 아이템을
        # 즉시 추가한다. LOADING 아이템은 findItem이 건너뛰므로 다운로드되지 않는다 (#124)
        placeholder = ContentItem(
            vod_url,
            {'title': vod_url, 'category': '', 'channelName': '', 'createdDate': '', 'duration': 0},
            [], None, '', downloadPath, '', None,
        )
        placeholder.downloadState = DownloadState.LOADING
        self.model.addItem(placeholder)
        self.insertItemRequested.emit(self.model.rowCount())

        worker = ContentWorker(vod_url, cookies, downloadPath)
        self._pendingPlaceholders[worker] = placeholder

        # 시그널/슬롯 연결 — 반드시 바운드 메서드로 연결한다. partial/lambda로
        # 연결하면 연결의 소유가 워커(발신자) 쪽이 되어, 워커가 파괴되는 순간
        # 큐에 남은 전달이 함께 사라진다 (#124 스모크 실패의 원인)
        worker.finished.connect(self.onWorkerFinished)  # 결과 처리 슬롯
        worker.error.connect(self.onWorkerError)           # 에러 처리 슬롯

        self.threadpool.start(lambda: worker.run())

    def onWorkerFinished(self, result, content_type):
        # result는 (vod_url, metadata, unique_reps, resolution, base_url, downloadPath, liveRewindPlaybackJson) 형식
        placeholder = self._pendingPlaceholders.pop(self.sender(), None)
        if placeholder is None:
            return
        vod_url, metadata, unique_reps, resolution, base_url, downloadPath, liveRewindPlaybackJson = result
        self.downloadPath = downloadPath
        row = self.model.getRow(placeholder)
        if row is None:
            # 조회 중 유저가 카드를 삭제한 경우 — 결과를 버린다
            return
        # 완성된 아이템으로 같은 자리에서 교체한다. 행 삭제→삽입을 거쳐야
        # 해상도 버튼·썸네일이 붙은 위젯이 새로 만들어진다
        item = ContentItem(vod_url, metadata, unique_reps, resolution, base_url, downloadPath, content_type, liveRewindPlaybackJson)
        self.model.removeRows(row, 1)
        self.model.addItem(item, row)

    def onWorkerError(self, error_message):
        placeholder = self._pendingPlaceholders.pop(self.sender(), None)
        if placeholder is not None:
            row = self.model.getRow(placeholder)
            if row is not None:
                self.model.removeRows(row, 1)
                self.deleteItemRequested.emit(placeholder, self.model.rowCount())
        self.contentError.emit(error_message)

    def clrearFinishedItems(self):
        if not self.model.isEmpty():
            for row in reversed(range(self.model.rowCount())):
                index = self.model.index(row, 0)
                item: ContentItem = self.model.data(index, Qt.ItemDataRole.UserRole)
                # 아이템이 완료 상태이면 삭제
                if item.downloadState == DownloadState.FINISHED:
                    self.removeItem(item)


    def removeItem(self, item: ContentItem):
        row = self.model.getRow(item)  # ✅ 객체의 row 찾기
        if row is not None:
            self.model.removeRows(row, 1)  # ✅ 올바른 삭제 요청
            index = self.model.rowCount()
            self.deleteItemRequested.emit(item, index)

    def downloadItem(self):
        found, item, index = self.findItem()
        if found:
            try:
                # 사전 검사 (#137): 존재만 보던 구 검사(os.path.exists)를 존재+쓰기
                # 프로브로 확장한다. 존재 검사도 프로브 스레드 안에서 수행한다 —
                # 무응답 마운트에서는 exists조차 메인 스레드를 매달 수 있다 (#136)
                writable, reason = probe_writable(item.download_path)
                if reason == "missing":
                    raise ValueError(self.tr("Invalid file path"))
                if not writable:
                    # 권한 없음(denied — 예: SFTP 상 ZFS 풀 루트) 또는 무응답
                    # 마운트(timeout — 권한 오류가 오류로 전파되지 않는 경우).
                    # 유저에게는 같은 사실이다: 이 경로에는 저장할 수 없다
                    # 경로는 repr로 남긴다 (#148) — 공백 유사 문자(U+00A0 등)를
                    # 육안 구분할 수 있는 유일한 표기다 (#144 실측)
                    logger.warning("쓰기 프로브 실패(%s): %r", reason, item.download_path)
                    self.fail(item, self.tr("Failed to save file"))
                    return
                self.onDownload(item)
            except ValueError as e:
                # 위에서 직접 던진 번역된 안내 — 그대로 카드에 표시한다.
                # 이 거부는 지금까지 로그가 전혀 없어 제보 진단이 불가능했다 (#148)
                logger.warning(
                    "다운로드 시작 거부 — 존재하지 않는 저장 경로: %r", item.download_path
                )
                self.fail(item, str(e))
            except Exception:
                # 경로 조립(OSError 등)의 원시 문자열에는 전체 경로가 섞여 있어
                # 유저에게 보내지 않는다 (#134) — 상세는 로그로만 남긴다
                logger.exception("다운로드 준비 실패: %s", item.title)
                self.fail(item, self.tr("Failed to save file"))
        else:
            self.finishedAllRequested.emit()

    def onDownload(self, item: ContentItem):
        """
        해상도 버튼 클릭 시 다운로드 진행.
        """
        if item:
            # 조립·중복 회피는 core가 단일 지점으로 담당한다 — 같은 제목이
            # 이미 있으면 " (n)"이 붙은 새 경로를 받는다 (#105)
            item.output_path = build_output_path(item.download_path, item.title, item.resolution)
        else:
            item.output_path = ensure_unique_path(os.path.join(item.download_path, "video.mp4"))

        if item.output_path:
            # 다운로드 요청 시그널 발행
            self.downloadRequested.emit(item)

    def update_progress(self, rem, size, spd, prog, item: ContentItem):
        item.download_remain_time = rem
        item.download_size = size
        item.download_speed = spd
        item.download_progress = prog
        
        row = self.model.getRow(item)
        index = self.model.index(row, 0)
        self.model.dataChanged.emit(index, index)

    def start(self, item):
        self.view.onDownloadStarted(item)

    def stop(self, item):
        self.view.onDownloadStoped(item)

    def pause(self, item):
        self.view.onDownloadPaused(item)

    def resume(self, item):
        self.view.onDownloadResumed(item)

    def finish(self, item: ContentItem, download_time):
        item.download_time = download_time
        self.view.onDownloadFinished(item, True)
        self.emitFinishedRequest(item)
    
    def fail(self, item: ContentItem, message: str = ""):
        """아이템을 실패 상태로 표시하고 배치 체인을 계속 진행한다 (#134).

        message는 카드의 실패 사유로 렌더된다 — 키 기반 매핑을 거친 번역
        문자열만 넣는다 (원시 예외 문자열 금지). emitFinishedRequest가 완료
        경로와 동일하게 다음 항목의 다운로드를 이어 간다.
        """
        item.stateMessage = message
        item.downloadState = DownloadState.FAILED
        self.view.onDownloadFinished(item, False)
        self.emitFinishedRequest(item)

    def emitStopRequested(self, item: ContentItem):
        self.stopRequested.emit(item)
    
    def emitFinishedRequest(self, item: ContentItem):
        self.finishedRequested.emit(item)
        self.downloadItem()

    def findItem(self):
        row_count = self.model.rowCount()
        for row in range(row_count):
            index = self.model.index(row, 0)
            item: ContentItem = self.model.data(index, Qt.ItemDataRole.UserRole)
            # LOADING은 메타데이터가 아직 없어 다운로드 대상이 아니다 (#124)
            if item.downloadState not in [DownloadState.FINISHED, DownloadState.FAILED, DownloadState.LOADING]:
                return True, item, index
        return False, None, None

    def hasLoadingItems(self):
        """메타데이터 조회가 끝나지 않은 아이템이 있는지 여부."""
        for row in range(self.model.rowCount()):
            index = self.model.index(row, 0)
            item: ContentItem = self.model.data(index, Qt.ItemDataRole.UserRole)
            if item.downloadState == DownloadState.LOADING:
                return True
        return False

    def downloadResultCounts(self) -> tuple[int, int]:
        """화면(모델)의 (완료, 실패) 항목 수를 센다 — 배치 종료 안내 분기용 (#134).

        별도 배치 장부를 두지 않고 화면 상태를 그대로 센다: 안내의 역할은
        "지금 화면에 보이는 결과"와 모순되지 않는 것이고, 배치의 경계는
        항목 추가·삭제가 진행 중에도 가능해 정확한 장부가 존재하지 않는다.
        """
        finished = failed = 0
        for row in range(self.model.rowCount()):
            index = self.model.index(row, 0)
            item: ContentItem = self.model.data(index, Qt.ItemDataRole.UserRole)
            if item.downloadState == DownloadState.FINISHED:
                finished += 1
            elif item.downloadState == DownloadState.FAILED:
                failed += 1
        return finished, failed