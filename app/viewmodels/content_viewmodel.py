"""content 조회·다운로드 오케스트레이션 viewmodel — 뷰 무의존 (#169).

구 ContentManager의 로직 절반이다: 모델 소유·워커 오케스트레이션·다운로드
게이트·배치 체인·항목 상태 전이. 뷰에는 시그널(itemStarted 등)로만 말하고
위젯 타입을 import하지 않는다 — 뷰 바인딩(시그널↔뷰 메서드 연결)은
content/manager.py의 ContentManager가 맡는다.

의존성 주입 3종은 바인더가 넘긴다:
- worker_factory: content.manager 모듈 전역 ContentWorker를 조회하는 함수
  (테스트의 monkeypatch 지점 보존)
- probe: content.manager.probe_writable (동상)
- messages: 실패 문구 콜러블 — tr() 리터럴은 번역 컨텍스트("ContentManager")와
  lupdate 스캔 대상(project.json sources)이 걸려 있어 바인더에 남는다
"""

import logging
import os

from PySide6.QtCore import QObject, QThreadPool, Signal

from app.viewmodels.item_state import ItemState
from app.viewmodels.path_gates import check_download_path
from content.data import ContentItem
from content.model import ContentListModel
from core.utils.paths import build_output_path, ensure_unique_path
from core.models.download_state import DownloadState

# 유저 행위·관문 로그의 로거 이름은 "content.manager"를 유지한다 — 제보
# 진단 절차와 기존 테스트(caplog)가 이 이름을 알고 있고, 로직의 거처가
# 바뀌었다고 로그 소비자의 주소까지 바꾸지 않는다. 로거 재편은 셸 단계에서
logger = logging.getLogger("content.manager")


class _WorkerRelay(QObject):
    """워커 1개의 finished/error를 워커 식별자와 함께 중계한다 — sender() 대체 (#169).

    바운드 메서드 연결 요구(#124: partial/lambda 연결은 소유가 워커 쪽이 되어
    워커 파괴 시 큐에 남은 전달이 유실된다)를 지키면서 워커→자리표시 매핑을
    명시적으로 만든다. 릴레이 참조는 viewmodel의 _relays에 담겨 결과 도착까지
    살아 있다.
    """

    def __init__(self, viewmodel: "ContentViewModel", worker):
        super().__init__(viewmodel)
        self._viewmodel = viewmodel
        self._worker = worker

    def onFinished(self, result, content_type):
        self._viewmodel._workerFinished(self._worker, result, content_type)

    def onError(self, error_message):
        self._viewmodel._workerError(self._worker, error_message)


class ContentViewModel(QObject):
    downloadRequested = Signal(object)
    stopRequested = Signal(object)
    insertItemRequested = Signal(int)
    deleteItemRequested = Signal(object, int)
    finishedRequested = Signal(object)
    finishedAllRequested = Signal()
    fetchRequested = Signal(str)
    contentError = Signal(str)

    # 뷰 방향 시그널 — 구 ContentManager의 view 직접 호출 6곳을 반전한 것.
    # 바인더가 view.onDownload*에 연결한다
    itemStarted = Signal(object)
    itemStopped = Signal(object)
    itemPaused = Signal(object)
    itemResumed = Signal(object)
    itemFinished = Signal(object, bool)

    def __init__(self, worker_factory, probe, messages: dict, parent=None):
        super().__init__(parent)
        self._worker_factory = worker_factory
        self._probe = probe
        self._messages = messages

        self.model = ContentListModel()
        self.downloadPath = ""
        self.threadpool = QThreadPool()
        # 조회 중인 워커 → 자리표시 아이템. 결과가 도착할 때까지 워커의 파이썬
        # 참조를 잡아 두는 역할도 한다 — 참조가 없으면 run() 종료 직후 워커가
        # 파괴되어 큐에 남은 finished/error 전달이 유실된다 (#124)
        self._pendingPlaceholders = {}
        self._relays = {}

    def fetchContent(self, vod_url: str, cookies: dict, downloadPath: str) -> None:
        # 조회가 끝나기 전에도 카드가 보이도록 LOADING 상태의 자리표시 아이템을
        # 즉시 추가한다. LOADING 아이템은 findItem이 건너뛰므로 다운로드되지 않는다 (#124)
        placeholder = ContentItem(
            vod_url,
            {'title': vod_url, 'category': '', 'channelName': '', 'createdDate': '', 'duration': 0},
            [], None, '', downloadPath, '', None,
        )
        placeholder.downloadState = ItemState.LOADING
        self.model.addItem(placeholder)
        self.insertItemRequested.emit(self.model.rowCount())

        worker = self._worker_factory(vod_url, cookies, downloadPath)
        relay = _WorkerRelay(self, worker)
        self._pendingPlaceholders[worker] = placeholder
        self._relays[worker] = relay

        worker.finished.connect(relay.onFinished)
        worker.error.connect(relay.onError)

        self.threadpool.start(lambda: worker.run())

    def _workerFinished(self, worker, result, content_type):
        # result는 (vod_url, metadata, unique_reps, resolution, base_url, downloadPath, liveRewindPlaybackJson) 형식
        placeholder = self._pendingPlaceholders.pop(worker, None)
        self._relays.pop(worker, None)
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

    def _workerError(self, worker, error_message):
        placeholder = self._pendingPlaceholders.pop(worker, None)
        self._relays.pop(worker, None)
        if placeholder is not None:
            row = self.model.getRow(placeholder)
            if row is not None:
                self.model.removeRows(row, 1)
                self.deleteItemRequested.emit(placeholder, self.model.rowCount())
        self.contentError.emit(error_message)

    def clrearFinishedItems(self):
        if not self.model.isEmpty():
            for row in reversed(range(self.model.rowCount())):
                item = self.model.itemAt(row)
                # 아이템이 완료 상태이면 삭제
                if item.downloadState == DownloadState.FINISHED:
                    self.removeItem(item)

    def removeItem(self, item: ContentItem):
        row = self.model.getRow(item)
        if row is not None:
            self.model.removeRows(row, 1)
            index = self.model.rowCount()
            self.deleteItemRequested.emit(item, index)

    def downloadItem(self):
        found, item, index = self.findItem()
        if found:
            try:
                # 사전 검사 (#137): 존재+쓰기 프로브. 판정은 path_gates가 단일
                # 지점으로 담당하고(#169 — #146 ⓑ1), 프로브 수단은 주입받는다.
                # 존재 검사도 프로브 스레드 안에서 수행한다 — 무응답 마운트에서는
                # exists조차 메인 스레드를 매달 수 있다 (#136)
                writable, reason = check_download_path(item.download_path, self._probe)
                if reason == "missing":
                    raise ValueError(self._messages["invalid_path"]())
                if not writable:
                    # 권한 없음(denied — 예: SFTP 상 ZFS 풀 루트) 또는 무응답
                    # 마운트(timeout — 권한 오류가 오류로 전파되지 않는 경우).
                    # 유저에게는 같은 사실이다: 이 경로에는 저장할 수 없다
                    # 경로는 repr로 남긴다 (#148) — 공백 유사 문자(U+00A0 등)를
                    # 육안 구분할 수 있는 유일한 표기다 (#144 실측)
                    logger.warning("쓰기 프로브 실패(%s): %r", reason, item.download_path)
                    self.fail(item, self._messages["save_failed"]())
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
                self.fail(item, self._messages["save_failed"]())
        else:
            self.finishedAllRequested.emit()

    def onDownload(self, item: ContentItem):
        """해상도가 정해진 아이템의 산출물 경로를 조립하고 다운로드를 요청한다."""
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

        self.model.notifyChanged(item)

    def start(self, item):
        self.itemStarted.emit(item)

    def stop(self, item):
        self.itemStopped.emit(item)

    def pause(self, item):
        self.itemPaused.emit(item)

    def resume(self, item):
        self.itemResumed.emit(item)

    def finish(self, item: ContentItem, download_time):
        item.download_time = download_time
        self.itemFinished.emit(item, True)
        self.emitFinishedRequest(item)

    def fail(self, item: ContentItem, message: str = ""):
        """아이템을 실패 상태로 표시하고 배치 체인을 계속 진행한다 (#134).

        message는 카드의 실패 사유로 렌더된다 — 키 기반 매핑을 거친 번역
        문자열만 넣는다 (원시 예외 문자열 금지). emitFinishedRequest가 완료
        경로와 동일하게 다음 항목의 다운로드를 이어 간다.
        """
        item.stateMessage = message
        item.downloadState = DownloadState.FAILED
        self.itemFinished.emit(item, False)
        self.emitFinishedRequest(item)

    def emitStopRequested(self, item: ContentItem):
        self.stopRequested.emit(item)

    def emitFinishedRequest(self, item: ContentItem):
        self.finishedRequested.emit(item)
        self.downloadItem()

    def findItem(self):
        row_count = self.model.rowCount()
        for row in range(row_count):
            item = self.model.itemAt(row)
            # LOADING은 메타데이터가 아직 없어 다운로드 대상이 아니다 (#124)
            if item.downloadState not in [DownloadState.FINISHED, DownloadState.FAILED, ItemState.LOADING]:
                return True, item, row
        return False, None, None

    def hasLoadingItems(self):
        """메타데이터 조회가 끝나지 않은 아이템이 있는지 여부."""
        for row in range(self.model.rowCount()):
            item = self.model.itemAt(row)
            if item.downloadState == ItemState.LOADING:
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
            item = self.model.itemAt(row)
            if item.downloadState == DownloadState.FINISHED:
                finished += 1
            elif item.downloadState == DownloadState.FAILED:
                failed += 1
        return finished, failed
