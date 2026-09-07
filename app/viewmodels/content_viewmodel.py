"""content 조회·다운로드 오케스트레이션 viewmodel — 뷰 무의존 (#169 → #259 B2).

모델 소유·조회 오케스트레이션(풀 스레드 + Signal 큐)·다운로드 게이트·배치
체인·항목 상태 전이. 뷰에는 시그널(itemStarted 등)로만 말하고 위젯 타입을
import하지 않는다 — 뷰 배선은 뷰 쪽(`ContentListView.bind`)이 이 뷰모델의
시그널·메서드에 자기를 건다.

B2(#259)에서 content/manager.py(뷰 바인더·쓰기 프로브·실패 문구 tr())와
content/worker.py(조회 콜백 본체·풀→메인 Signal)를 여기로 흡수했다:
- `probe_writable` — OS 수준 쓰기 프로브(#137). 모듈 수준 함수이며
  `downloadItem`이 호출 시점에 모듈 전역을 조회한다(테스트 monkeypatch 지점).
- `FetchJob` — 조회 한 건. 풀 스레드에서 `run()`하고 결과를 finished/error
  Signal로 emit한다(스레드 경계). 모듈 전역 이름을 호출 시점에 조회한다.
  조회 오류 문구 tr() 10건은 **FetchJob이 소유**한다(컨텍스트 `FetchJob`).
- 다운로드 관문 문구 tr() 2건은 이 클래스가 소유한다(컨텍스트 `ContentViewModel`).

번역 컨텍스트가 둘인 것은 사실에 맞다 — 조회 오류는 대화상자에, 관문 문구는
카드 3행에 뜬다. tests/unit/test_card_state_matrix.py가 컨텍스트를 "카드에 뜨는
문구"의 경계로 쓰므로, 둘을 한 컨텍스트에 두면 대화상자 문구에 카드 길이
규약이 적용된다. 제외 목록으로 푸는 것은 새 문구가 생길 때마다 누군가 추가해야
하고 잊으면 조용히 잘못 재므로 쓰지 않는다.

스레드 경계: 조회는 QThreadPool 워커에서 돌고, 반영(자리표시 교체·통지)은
`_WorkerRelay`(메인 스레드 QObject)가 큐 연결로 받아 메인 스레드에서 한다.
tests/unit/test_fetch_thread_boundary.py가 이 경계를 잰다 — 풀에서 끝났다고
슬롯을 직접 부르면 모델·위젯이 풀 스레드에서 갱신된다.
"""

import logging
import os
import tempfile
import threading

from PySide6.QtCore import QObject, QThreadPool, Signal

from app.viewmodels.item_state import ItemState
from app.viewmodels.path_gates import check_download_path
from app.viewmodels.data import ContentItem
from app.viewmodels.model import ContentListModel
from app.network import NetworkManager
from core.services import metadata_service
from core.services.metadata_service import MetadataError
from core.utils.paths import build_output_path, ensure_unique_path
from core.models.download_state import DownloadState

logger = logging.getLogger(__name__)
# 조회 실패 트레이스백(구 content.worker) — 이름을 박은 테스트가 없어 모듈 경로 유도
_fetch_logger = logging.getLogger(__name__)

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


class FetchJob(QObject):
    """메타데이터 조회 한 건 — 풀 스레드에서 `run()`하고 결과를 Signal로 메인에 넘긴다 (구 ContentWorker, #72).

    조회 로직(URL 파싱 → API 조회 → 에러 분기)은 core/services/metadata_service.py에
    있다. 이 클래스는 다음만 담당한다:
    - core 호출 결과를 finished/error Signal로 emit (시그니처·페이로드 무변경).
      emit은 풀 스레드에서 일어나고, 수신자(`_WorkerRelay`)가 메인 스레드에 살아
      큐로 배달된다.
    - MetadataError의 i18n 키를 tr()로 번역해 기존 에러 메시지 형식("<url>\n<메시지>")
      유지. 조회 오류 문구는 대화상자에 뜨는 것이라 이 클래스가 소유한다
      (번역 컨텍스트 `FetchJob`).
    """

    finished = Signal(object, str)
    error = Signal(str)

    def __init__(self, vod_url: str, cookies: dict, downloadPath: str):
        super().__init__()
        self.vod_url = vod_url
        self.cookies = cookies
        self.downloadPath = downloadPath

    def run(self):
        """메타데이터를 조회해 finished(성공) 또는 error(실패) Signal을 emit한다."""
        try:
            result, content_type = metadata_service.fetch_content(
                self.vod_url, self.cookies, self.downloadPath, api=NetworkManager
            )
            self.finished.emit(result, content_type)
        except Exception as e:
            # 크래시 지점 추적을 위해 traceback을 로그에 남긴다 (#55 디버깅).
            # str(e)만으로는 AttributeError 등의 발생 위치를 알 수 없다
            _fetch_logger.exception("컨텐츠 요청 실패: %s", self.vod_url)
            self.error.emit(self._user_message(e))

    def _user_message(self, e: Exception) -> str:
        """예외를 사용자 표시용 메시지로 바꾼다. MetadataError는 i18n 키를 번역한다.

        MetadataError가 아닌 예외의 원시 문자열은 내부 API URL 등이 섞여 있어
        유저에게 보여주지 않는다 — 상세는 run()의 logger.exception이 남긴다 (#126).
        """
        if isinstance(e, MetadataError):
            return f"{e.url}\n{self._translate_key(e.message_key)}"
        return f"{self.vod_url}\n{self._translate_key('Failed to fetch video information')}"

    def _translate_key(self, message_key: str) -> str:
        """i18n 키를 현재 언어로 번역한다.

        lupdate가 `-no-obsolete`로 .ts를 재생성하므로(compile_translations.py 참고)
        키가 소스에서 사라지면 번역 항목도 삭제된다 — 반드시 리터럴로 tr()을 호출해
        추출 대상을 유지한다. 키 목록은 core/services/metadata_service.py가 던지는
        message_key 전체와 1:1이다.
        """
        translated = {
            "Invalid VOD URL": self.tr("Invalid VOD URL"),
            "Invalid cookies value": self.tr("Invalid cookies value"),
            "Encrypted content is not supported": self.tr("Encrypted content is not supported"),
            "Channel membership required": self.tr("Channel membership required"),
            "Unencoded Video(.m3u8)": self.tr("Unencoded Video(.m3u8)"),
            "Failed to get DASH manifest": self.tr("Failed to get DASH manifest"),
            "Video not found": self.tr("Video not found"),
            "Viewing permission required": self.tr("Viewing permission required"),
            "Network connection error": self.tr("Network connection error"),
            "Failed to fetch video information": self.tr("Failed to fetch video information"),
        }
        return translated.get(message_key, message_key)


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

    def __init__(self, parent=None):
        super().__init__(parent)
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

        # 모듈 전역 FetchJob을 호출 시점에 조회한다 — 테스트의 monkeypatch 지점
        worker = FetchJob(vod_url, cookies, downloadPath)
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

    # ---- 다운로드 관문 문구 — 카드 3행에 뜨는 tr() 2건 (컨텍스트 ContentViewModel) ----

    def _invalidPathMessage(self) -> str:
        """다운로드 관문: 저장 경로가 존재하지 않을 때의 카드 문구."""
        return self.tr("Invalid file path")

    def _saveFailedMessage(self) -> str:
        """다운로드 관문: 쓰기 프로브 실패(denied·timeout)의 카드 문구."""
        # 첫 줄=핵심 / 둘째 줄=상세 규약(#245, app/viewmodels/download_viewmodel.py 참고) —
        # 다운로드 쪽의 같은 사유와 문구를 맞춘다
        return self.tr(
            "Failed to save file · check the path and disk space\n"
            "The file could not be saved. Check the download path and free disk space."
        )

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
                writable, reason = check_download_path(
                    item.download_path, lambda directory: probe_writable(directory)
                )
                if reason == "missing":
                    raise ValueError(self._invalidPathMessage())
                if not writable:
                    # 권한 없음(denied — 예: SFTP 상 ZFS 풀 루트) 또는 무응답
                    # 마운트(timeout — 권한 오류가 오류로 전파되지 않는 경우).
                    # 유저에게는 같은 사실이다: 이 경로에는 저장할 수 없다
                    # 경로는 repr로 남긴다 (#148) — 공백 유사 문자(U+00A0 등)를
                    # 육안 구분할 수 있는 유일한 표기다 (#144 실측)
                    logger.warning("쓰기 프로브 실패(%s): %r", reason, item.download_path)
                    self.fail(item, self._saveFailedMessage())
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
                self.fail(item, self._saveFailedMessage())
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
