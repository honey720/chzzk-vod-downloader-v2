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
카드 3행에 뜬다. tests/app/gui/test_card_state_matrix.py가 컨텍스트를 "카드에 뜨는
문구"의 경계로 쓰므로, 둘을 한 컨텍스트에 두면 대화상자 문구에 카드 길이
규약이 적용된다. 제외 목록으로 푸는 것은 새 문구가 생길 때마다 누군가 추가해야
하고 잊으면 조용히 잘못 재므로 쓰지 않는다.

스레드 경계: 조회는 QThreadPool 워커에서 돌고, 반영(자리표시 교체·통지)은
`_WorkerRelay`(메인 스레드 QObject)가 큐 연결로 받아 메인 스레드에서 한다.
tests/app/gui/test_fetch_thread_boundary.py가 이 경계를 잰다 — 풀에서 끝났다고
슬롯을 직접 부르면 모델·위젯이 풀 스레드에서 갱신된다.
"""

import logging
import os
import tempfile
import threading
import time

from PySide6.QtCore import QObject, QThreadPool, Signal

from app.viewmodels.card_order import CardOrder
from app.viewmodels.item_state import ItemState
from app.viewmodels.path_gates import check_download_path
from app.viewmodels.data import SECTION_CHECK_PENDING, ContentItem
from app.viewmodels.model import ContentListModel
from app.viewmodels.section_edit import SectionRefitter, SectionSizer
from app.network import NetworkManager
from core.chzzk import metadata_service
from core.chzzk.metadata_service import MetadataError
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

UNDO_LIMIT = 50  # 되살릴 수 있는 지우기 수(#561) — 한 번에 여러 장을 지웠어도 하나로 센다(설계 §6 C4, 프로토 50)
# 엔진이 쥔 카드의 상태(#561) — 받는 중(자르는 중 · 합치는 중 포함) · 일시정지. 지울 때 엔진을 멈추고, 되살리면 대기다.
# 목록의 BUSY_STATES(app/widgets/widget.py)와 같은 값 — 뷰모델은 위젯 모듈을 import하지 않는다
WORKING_STATES = (DownloadState.RUNNING, DownloadState.PAUSED)
# 카드의 속도 · 남은 시간 글을 바꾸는 간격(초) — 진행 통지가 0.25초마다 와도 이 글은 1초에 한 번만
# 바꾼다(읽기 쉽게, 오너 2026-11-10). 진행률 · 받은 크기 · 막대는 통지마다 바뀐다 (#557)
RATE_TEXT_SECONDS = 1.0
# 그 간격의 이 배가 지나면 바꾼다 — 통지 간격이 시계 단위만큼 흔들려도 넷째 통지에서 바뀌게
RATE_TEXT_FILL = 0.9
_clock = time.monotonic  # 속도 · 남은 시간 글의 시계 — 테스트가 바꿔 끼운다


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


# 다운로드 대상이 아닌 카드 상태 — 끝났거나(완료 · 실패) 아직 조회 중이다
_NOT_DOWNLOADABLE = (DownloadState.FINISHED, DownloadState.FAILED, ItemState.LOADING)


class FetchJob(QObject):
    """메타데이터 조회 한 건 — 풀 스레드에서 `run()`하고 결과를 Signal로 메인에 넘긴다 (구 ContentWorker, #72).

    조회 로직(URL 파싱 → API 조회 → 에러 분기)은 core/chzzk/metadata_service.py에
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

    def __init__(self, vod_url: str, cookies: dict, downloadPath: str) -> None:
        super().__init__()
        self.vod_url = vod_url
        self.cookies = cookies
        self.downloadPath = downloadPath

    def run(self) -> None:
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
        추출 대상을 유지한다. 키 목록은 core/chzzk/metadata_service.py가 던지는
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
            "Server response is slow": self.tr("Server response is slow"),
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

    def __init__(self, viewmodel: "ContentViewModel", worker) -> None:
        super().__init__(viewmodel)
        self._viewmodel = viewmodel
        self._worker = worker

    def onFinished(self, result, content_type) -> None:
        self._viewmodel._workerFinished(self._worker, result, content_type)

    def onError(self, error_message) -> None:
        self._viewmodel._workerError(self._worker, error_message)


class RemoveCards:
    """카드 지우기 한 번 — 한 장이든 여러 장이든 되돌리기 한 번에 함께 되살아난다 (#561).

    `redo`가 지우고 `undo`가 지운 자리로 되살린다. 지울 때 엔진이 쥐고 있던 카드(받는 중 · 일시정지)는
    멈추고 지웠으므로(임시 파일 · 구간 파일이 지워진다) 대기로 되살린다. 그 밖 카드는 지울 때의 상태 ·
    편집값 · 구간 그대로다(프로토 `RemoveCard`).
    """

    def __init__(self, viewmodel: "ContentViewModel", items: list[ContentItem]) -> None:
        self._viewmodel = viewmodel
        model = viewmodel.model
        # (목록 행, 카드) — 행이 작은 것부터. 되살릴 때 이 차례로 넣어야 저마다의 행이 지울 때와 같다
        self.entries = sorted(((model.getRow(item), item) for item in items), key=lambda e: e[0])
        self.working = {item for item in items if item.downloadState in WORKING_STATES}

    def items(self) -> list[ContentItem]:
        """이 지우기가 든 카드."""
        return [item for _row, item in self.entries]

    def redo(self) -> None:
        self._viewmodel._detach(self.items())

    def undo(self) -> None:
        self._viewmodel._attach(self.entries, self.working)


class ContentViewModel(QObject):
    downloadRequested = Signal(object)
    stopRequested = Signal(object)
    insertItemRequested = Signal(int)
    deleteItemRequested = Signal(object, int)
    finishedRequested = Signal(object)
    finishedAllRequested = Signal()
    fetchRequested = Signal(str)
    contentError = Signal(str)
    # 조회 중인 카드가 생겼다(True) · 다 끝났다(False) — 등록 버튼 안의 도는 표시(#495)
    fetchingChanged = Signal(bool)
    # 배치가 구간 편집 중인 카드를 기다리기 시작했다(True) · 그만 기다린다(False) (#309) —
    # 편집 창이 아래쪽 안내를 켜고 끈다
    editWaitChanged = Signal(bool)

    # 뷰 방향 시그널 — 구 ContentManager의 view 직접 호출 6곳을 반전한 것.
    # 바인더가 view.onDownload*에 연결한다
    itemStarted = Signal(object)
    itemStopped = Signal(object)
    itemPaused = Signal(object)
    itemResumed = Signal(object)
    itemFinished = Signal(object, bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.model = ContentListModel()
        # 필터 · 정렬 상태와 보이는 차례 = 다운로드 차례(#495). ⚠️ 목록 뷰가 모델에 잇기 전에 만든다 —
        # 뷰가 카드를 넣을 때 이 객체에 자리를 묻는다(같은 시그널은 이은 차례로 돈다)
        self.order = CardOrder(self.model, self)
        # 카드 상태는 이 알림들 뒤에 바뀌어 있다 — 순서 뷰모델이 칩 숫자 · 자리를 그 차례 끝에 다시 본다
        for signal in (
            self.itemStarted,
            self.itemStopped,
            self.itemPaused,
            self.itemResumed,
            self.itemFinished,
        ):
            signal.connect(self.order.touch)
        self.downloadPath = ""
        self.threadpool = QThreadPool()
        # 조회 중인 워커 → 자리표시 아이템. 결과가 도착할 때까지 워커의 파이썬
        # 참조를 잡아 두는 역할도 한다 — 참조가 없으면 run() 종료 직후 워커가
        # 파괴되어 큐에 남은 finished/error 전달이 유실된다 (#124)
        self._pendingPlaceholders = {}
        self._relays = {}
        # 구간 편집 창이 열려 있는 카드 (#309). 다운로드 대상에서 건너뛴다 — 상태
        # (DownloadState)로 만들지 않는다. 엔진과 카드는 이 값을 모른다
        self._editingItem: ContentItem | None = None
        # 편집 중인 카드만 남아 배치를 끝내지 않고 멈춰 둔 상태 — 창을 닫으면 이어 간다
        self._heldForEdit = False
        # 길이를 확인하는 중인 카드만 남아 배치를 멈춰 둔 상태 — 조회가 끝나면 이어 간다
        self._heldForCheck = False
        # 구간이 있는 카드의 해상도가 바뀌면 구간을 새 해상도에 다시 맞춘다 (#309)
        self._sectionRefitter = SectionRefitter(self.model, self.threadpool, self)
        self._sectionRefitter.settled.connect(self._onSectionSettled)
        # 구간 카드가 받을 크기를 백그라운드에서 센다 — 구간을 확인했을 때와 해상도 변경의 조회가 끝났을 때
        self._sectionSizer = SectionSizer(self.model, self.threadpool, self)
        # 지우기 되돌리기(#561, 설계 §6 C4) — Ctrl/⌘+Z가 마지막 지우기부터 되살린다. 오래된 것부터 차례로, 한도
        # UNDO_LIMIT. 설계 · 프로토는 QUndoStack이지만 QUndoStack은 QtGui에 있어 뷰모델이 쓸 수 없다
        # (tests/meta/test_app_layer_imports.py) — 다시 하기 없이 되돌리기만 쓰므로 같은 규칙의 목록으로 둔다
        self._removals: list[RemoveCards] = []

    def fetchContent(self, vod_url: str, cookies: dict, downloadPath: str) -> None:
        # 조회가 끝나기 전에도 카드가 보이도록 LOADING 상태의 자리표시 아이템을
        # 즉시 추가한다. LOADING 아이템은 findItem이 건너뛰므로 다운로드되지 않는다 (#124)
        placeholder = ContentItem(
            vod_url,
            {"title": vod_url, "category": "", "channelName": "", "createdDate": "", "duration": 0},
            [],
            None,
            "",
            downloadPath,
            "",
            None,
        )
        placeholder.downloadState = ItemState.LOADING
        self.model.addItem(placeholder)
        self.insertItemRequested.emit(self.model.rowCount())

        # 모듈 전역 FetchJob을 호출 시점에 조회한다 — 테스트의 monkeypatch 지점
        worker = FetchJob(vod_url, cookies, downloadPath)
        relay = _WorkerRelay(self, worker)
        self._pendingPlaceholders[worker] = placeholder
        self._relays[worker] = relay
        if len(self._pendingPlaceholders) == 1:
            self.fetchingChanged.emit(True)

        worker.finished.connect(relay.onFinished)
        worker.error.connect(relay.onError)

        self.threadpool.start(lambda: worker.run())

    def _workerFinished(self, worker, result, content_type) -> None:
        # result는 (vod_url, metadata, unique_reps, resolution, base_url, downloadPath, liveRewindPlaybackJson) 형식
        placeholder = self._pendingPlaceholders.pop(worker, None)
        self._relays.pop(worker, None)
        self._emitFetchingDone()
        if placeholder is None:
            return
        (
            vod_url,
            metadata,
            unique_reps,
            resolution,
            base_url,
            downloadPath,
            liveRewindPlaybackJson,
        ) = result
        self.downloadPath = downloadPath
        row = self.model.getRow(placeholder)
        if row is None:
            # 조회 중 유저가 카드를 삭제한 경우 — 결과를 버린다
            return
        # 완성된 아이템으로 같은 자리에서 교체한다. 행 삭제→삽입을 거쳐야
        # 해상도 버튼·썸네일이 붙은 위젯이 새로 만들어진다
        item = ContentItem(
            vod_url,
            metadata,
            unique_reps,
            resolution,
            base_url,
            downloadPath,
            content_type,
            liveRewindPlaybackJson,
        )
        self.model.removeRows(row, 1)
        self.model.addItem(item, row)

    def _workerError(self, worker, error_message) -> None:
        placeholder = self._pendingPlaceholders.pop(worker, None)
        self._relays.pop(worker, None)
        self._emitFetchingDone()
        if placeholder is not None:
            row = self.model.getRow(placeholder)
            if row is not None:
                self.model.removeRows(row, 1)
                self.deleteItemRequested.emit(placeholder, self.model.rowCount())
        self.contentError.emit(error_message)

    def isFetching(self) -> bool:
        """조회가 끝나지 않은 등록이 있는지 — 등록 버튼의 도는 표시(#495)."""
        return bool(self._pendingPlaceholders)

    def _emitFetchingDone(self) -> None:
        """마지막 조회가 끝났으면 알린다(결과를 쓰기 전 — 성공 · 실패 모두)."""
        if not self._pendingPlaceholders:
            self.fetchingChanged.emit(False)

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

    def removeItem(self, item: ContentItem) -> None:
        """카드 하나를 지운다 — `removeItems([item])`과 같다(Ctrl/⌘+Z로 되살린다)."""
        self.removeItems([item])

    def removeItems(self, items) -> None:
        """카드 여럿을 한 번에 지운다 — 되돌리기 한 번에 함께 되살아난다 (#561).

        목록에 없는 카드는 건너뛴다. 엔진이 쥔 카드는 부르는 쪽(`MainWindow`)이 먼저 멈춘다 — 이
        뷰모델은 엔진을 모른다. 되살릴 수 있게 카드를 들고 있으므로 목록은 위젯을 지우지 않고 숨겨 둔다.
        """
        present = [item for item in dict.fromkeys(items) if self.model.getRow(item) is not None]
        if not present:
            return
        removal = RemoveCards(self, present)
        removal.redo()
        self._removals.append(removal)
        if len(self._removals) > UNDO_LIMIT:
            dropped = self._removals.pop(0)  # 가장 오래된 지우기 — 그 카드는 이제 되살릴 수 없다
            held = {item for r in self._removals for item in r.items()}
            self.model.release(item for item in dropped.items() if item not in held)

    def canUndoRemove(self) -> bool:
        """되살릴 지우기가 있는지."""
        return bool(self._removals)

    def undoRemove(self) -> None:
        """마지막 지우기를 되살린다 — 없으면 아무 일도 없다. 되살린 지우기는 목록에서 빠진다(다시 하기는 없다)."""
        if self._removals:
            self._removals.pop().undo()

    def _detach(self, items: list[ContentItem]) -> None:
        """카드를 목록에서 뺀다 — 되살릴 수 있게 든 채로(`ContentListModel.removeRows(keep=True)`).

        여러 장 작업(`beginBulk`)으로 묶는다 — 목록 레이아웃 · 칩 숫자는 끝에 한 번 맞춘다.
        """
        self.model.beginBulk()
        try:
            for item in items:
                row = self.model.getRow(item)
                if row is None:
                    continue
                self.model.removeRows(row, 1, keep=True)
                item.section_head = None  # 구간을 정하며 받아 둔 moov를 버린다 (#309)
                # 이 카드의 조회 · 크기 세기가 돌고 있으면 그만두게 한다 — 결과를 쓸 카드가 없다
                self._sectionRefitter.cancel(item)
                self._sectionSizer.cancel(item)
                self.deleteItemRequested.emit(item, self.model.rowCount())
        finally:
            self.model.endBulk()
        self._resumeHeldBatch()  # 기다리던 카드가 지워졌을 수 있다

    def _attach(self, entries: list[tuple[int, ContentItem]], working: set[ContentItem]) -> None:
        """지운 카드를 지운 자리로 되살린다 — 행이 작은 것부터 넣는다.

        그 사이 목록이 줄었으면 끝에 붙인다. 지울 때 엔진이 쥐고 있던 카드는 대기로(진행 · 속도 · 준비
        표시를 비운다). 지우며 그만두게 한 길이 확인 · 크기 세기는 다시 시작하거나 표시를 내린다. 여러 장
        작업으로 묶는다(`_detach`와 같다).
        """
        self.model.beginBulk()
        try:
            for row, item in entries:
                if self.model.getRow(item) is not None:
                    continue
                if (
                    item.downloadState == ItemState.LOADING
                    and item not in self._pendingPlaceholders.values()
                ):
                    # 조회가 이미 끝났다(결과는 카드가 없어 버렸다) — 되살려도 채워 줄 조회가 없어 조회 중으로 남는다
                    self.model.release([item])
                    continue
                if item in working:
                    self._resetToWaiting(item)
                if item.section_sizing:
                    item.section_sizing = False  # 크기 세기는 지울 때 그만뒀다 — moov도 버렸다
                self.model.addItem(item, min(row, self.model.rowCount()))
                if item.section_check == SECTION_CHECK_PENDING:
                    self.refitSections(item)  # 길이 확인 조회도 지울 때 그만뒀다 — 다시 묻는다
        finally:
            self.model.endBulk()

    @staticmethod
    def _resetToWaiting(item: ContentItem) -> None:
        """멈추고 지운 카드를 대기로 돌린다 — 받는 중 · 일시정지의 표시가 남지 않게."""
        item.downloadState = DownloadState.WAITING
        item.stateMessage = ""
        item.download_progress = 0
        item.download_size = ""
        item.download_speed = ""
        item.download_remain_time = ""
        item.preparing = False
        item.prepare_pending = False
        item.cut_preparing = False
        item.post_process = False

    def downloadItem(self) -> None:
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
        elif self._editingIsCandidate():
            # 남은 대상이 편집 중인 카드뿐이다 (#309) — 배치를 끝내지 않고 멈춰 둔다. 끝내면
            # 편집하는 사이에 완료 안내와 다운로드 후 동작(절전 · 종료)이 실행된다
            self._heldForEdit = True
            self.editWaitChanged.emit(True)
        elif self._checkingIsCandidate():
            # 남은 대상이 새 해상도의 길이를 확인하는 중인 카드뿐이다 (#309) — 조회가 끝나
            # 구간을 다시 맞출 때까지 기다린다. 조회는 성공하든 실패하든 끝난다
            self._heldForCheck = True
        else:
            self.finishedAllRequested.emit()

    def refitSections(self, item: ContentItem) -> None:
        """구간이 있는 카드의 해상도가 바뀌었다 — 구간을 새 해상도에 다시 맞춘다 (#309).

        곧바로 선언 프레임률로 맞추고, 새 해상도의 프레임률 · 길이를 백그라운드에서 조회해
        그 값으로 다시 맞춘다(``SectionRefitter``). 구간이 없는 카드는 조회하지 않는다.
        """
        # 앞 해상도의 moov로 크기를 세고 있으면 그만두게 한다 — 긴 영상의 색인 해석은 몇 초를
        # 돌고, 그 결과는 어차피 버려진다
        self._sectionSizer.cancel(item)
        self._sectionRefitter.request(item)

    def beginSectionEdit(self, item: ContentItem) -> None:
        """구간 편집 창이 열린 카드를 다운로드 대상에서 뺀다 (#309).

        창이 열린 사이 그 카드의 차례가 오면 건너뛰고 다음 카드로 간다. 한 번에 한 카드만
        편집한다(창이 모달이다).
        """
        self._editingItem = item

    def endSectionEdit(self, item: ContentItem) -> None:
        """구간 편집 창이 닫혔다 — 카드가 다시 다운로드 대상이 된다 (#309).

        편집 중인 카드만 남아 멈춰 둔 배치가 있으면 이어 간다. 멈춰 둔 것이 없으면 아무것도
        시작하지 않는다 — 창을 닫는 것이 다운로드를 시작시키지 않는다.
        """
        if self._editingItem is not item:
            return
        self._editingItem = None
        self._sectionSizer.request(item)  # 구간을 확인했으면 받을 크기를 센다
        self._resumeHeldBatch()

    def _onSectionSettled(self, item: ContentItem) -> None:
        """카드의 길이 확인 조회가 끝났다 — 그 카드가 다시 다운로드 대상이 된다 (#309)."""
        self._sectionSizer.request(item)  # 새 해상도의 moov로 받을 크기를 다시 센다
        self._resumeHeldBatch()

    def _resumeHeldBatch(self) -> None:
        """멈춰 둔 배치가 있으면 이어 간다. 없으면 아무것도 시작하지 않는다.

        기다리던 사유(편집 중 · 길이 확인 중)가 풀렸거나 기다리던 카드가 지워졌을 때 부른다.
        표시를 먼저 내리고 다음 카드를 다시 고른다 — 여전히 기다려야 하면 ``downloadItem``이
        다시 멈춰 둔다. 표시를 든 채 시작하면 받는 중에 한 번 더 시작하게 된다.
        """
        if not (self._heldForEdit or self._heldForCheck):
            return
        if self._heldForEdit:
            self.editWaitChanged.emit(False)
        self._heldForEdit = self._heldForCheck = False
        self.downloadItem()

    def isWaitingOnEdit(self) -> bool:
        """배치가 구간 편집 중인 카드를 기다리고 있는지."""
        return self._heldForEdit

    def _editingIsCandidate(self) -> bool:
        """편집 중인 카드가 목록에 있고, 편집 중이 아니었다면 다운로드 대상이었는지."""
        item = self._editingItem
        return (
            item is not None
            and self.model.getRow(item) is not None
            and item.downloadState not in _NOT_DOWNLOADABLE
        )

    @staticmethod
    def _awaitsSectionCheck(item: ContentItem) -> bool:
        """해상도를 바꾼 뒤 새 해상도의 길이를 확인하는 중인 구간 카드인지 (#309).

        조회가 끝나 구간을 다시 맞추기 전에는 받지 않는다 — 선언값으로만 맞춘 구간은 새
        영상의 길이를 넘을 수 있다. 조회가 실패한 카드는 여기에 들지 않는다(그대로 받는다).
        """
        return bool(item.selections) and item.section_check == SECTION_CHECK_PENDING

    def _checkingIsCandidate(self) -> bool:
        """길이를 확인하는 중이 아니었다면 다운로드 대상이었을 카드가 목록에 있는지."""
        for row in range(self.model.rowCount()):
            item = self.model.itemAt(row)
            if item.downloadState not in _NOT_DOWNLOADABLE and self._awaitsSectionCheck(item):
                return True
        return False

    def onDownload(self, item: ContentItem) -> None:
        """해상도가 정해진 아이템의 산출물 경로를 조립하고 다운로드를 요청한다."""
        if item:
            # 조립·중복 회피는 core가 단일 지점으로 담당한다 — 같은 제목이
            # 이미 있으면 " (n)"이 붙은 새 경로를 받는다 (#105)
            item.output_path = build_output_path(
                item.download_path, item.title, item.resolution, item.resolution_tag
            )
        else:
            item.output_path = ensure_unique_path(os.path.join(item.download_path, "video.mp4"))

        if item.output_path:
            # 받기 시작하면 대기 카드의 크기 세기는 쓸 곳이 없다 — 결과는 어차피 버려진다.
            # 그만두게 해 해석의 차례를 엔진에 넘긴다: 표에서 셀 수 없는 모양의 moov는 크기
            # 세기가 색인을 만들며 차례를 쥐고, 엔진은 그동안 기다린 뒤 같은 moov를 다시 해석했다
            self._sectionSizer.cancel(item)
            # 다운로드 요청 시그널 발행
            self.downloadRequested.emit(item)

    def update_progress(self, rem, size, spd, prog, item: ContentItem) -> None:
        """진행 통지 하나를 카드에 적는다 — 속도 · 남은 시간 글만 1초에 한 번 바꾼다 (#557)."""
        now = _clock()
        if (
            item.rate_text_at is None
            or now - item.rate_text_at >= RATE_TEXT_SECONDS * RATE_TEXT_FILL
        ):
            item.download_remain_time = rem
            item.download_speed = spd
            item.rate_text_at = now
        item.download_size = size
        item.download_progress = prog

        self.model.notifyChanged(item)

    def start(self, item) -> None:
        self.itemStarted.emit(item)

    def stop(self, item) -> None:
        self.itemStopped.emit(item)

    def pause(self, item) -> None:
        self.itemPaused.emit(item)

    def resume(self, item) -> None:
        self.itemResumed.emit(item)

    def finish(self, item: ContentItem, download_time) -> None:
        item.download_time = download_time
        self.itemFinished.emit(item, True)
        self.emitFinishedRequest(item)

    def fail(self, item: ContentItem, message: str = "") -> None:
        """아이템을 실패 상태로 표시하고 배치 체인을 계속 진행한다 (#134).

        message는 카드의 실패 사유로 렌더된다 — 키 기반 매핑을 거친 번역
        문자열만 넣는다 (원시 예외 문자열 금지). emitFinishedRequest가 완료
        경로와 동일하게 다음 항목의 다운로드를 이어 간다.
        """
        item.stateMessage = message
        item.downloadState = DownloadState.FAILED
        self.itemFinished.emit(item, False)
        self.emitFinishedRequest(item)

    def emitStopRequested(self, item: ContentItem) -> None:
        self.stopRequested.emit(item)

    def emitFinishedRequest(self, item: ContentItem) -> None:
        self.finishedRequested.emit(item)
        self.downloadItem()

    def findItem(self) -> tuple[bool, ContentItem | None, int | None]:
        """다음에 받을 카드를 고른다 — (찾음, 카드, 목록 행).

        고르는 차례는 화면 순서(정렬 · 방향)다 — 받을 차례가 올 때마다 그 순서에서 위부터 첫
        받을 수 있는 카드를 고른다(#495, 설계 §2). 필터로 가린 카드도 차례에 든다. 목록 자체는
        추가된 순서 그대로이고, 돌려주는 행은 목록의 행이다.
        """
        for item in self.order.ordered():
            # LOADING은 메타데이터가 아직 없어 다운로드 대상이 아니다 (#124).
            # 구간 편집 창이 열린 카드는 건너뛴다 — 창을 닫으면 다시 대상이 된다 (#309)
            if item is self._editingItem:
                continue
            # 새 해상도의 길이를 확인하는 중인 구간 카드도 건너뛴다 — 조회가 끝나면 대상이 된다
            if self._awaitsSectionCheck(item):
                continue
            if item.downloadState not in _NOT_DOWNLOADABLE:
                return True, item, self.model.getRow(item)
        return False, None, None

    def hasLoadingItems(self) -> bool:
        """메타데이터 조회가 끝나지 않은 아이템이 있는지 여부."""
        for row in range(self.model.rowCount()):
            item = self.model.itemAt(row)
            if item.downloadState == ItemState.LOADING:
                return True
        return False
