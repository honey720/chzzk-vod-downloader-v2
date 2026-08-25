"""content 조회·다운로드 오케스트레이션 viewmodel — Qt-free 대응 (#220, Phase B1).

`app/viewmodels/content_viewmodel.py`(#169)의 로직(모델 소유·워커
오케스트레이션·다운로드 게이트·배치 체인·항목 상태 전이)을 그대로 옮긴다.
`main.py`(Qt 앱)가 쓰는 `content_viewmodel.py`·`content/manager.py`·
`content/model.py`·`content/worker.py`는 이 파일과 무관하게 그대로
남는다 — 로직 중복은 `download/qt_bridge.py`/`app/download_bridge.py`와
같은 의도적 트레이드오프이며, Phase D에서 옛 Qt 파일이 삭제되면 해소된다.

**Qt에서 걷어낸 것과 그 대체**:
- `QAbstractListModel`(`content/model.py`) → 평 리스트(`self.items`). 원본이
  모델 인덱스를 거치는 건 전부 "row로 아이템 찾기"뿐이라 `Qt.ItemDataRole`
  왕복이 통째로 사라진다.
- `QThreadPool` → `concurrent.futures.ThreadPoolExecutor`.
- 커스텀 Signal(뷰 방향 통지) → `app/dispatcher.py`의 `Dispatcher.dispatch_js`.
  단, `stopRequested`·`fetchRequested`는 실제 신호 경로를 grep으로 추적한
  결과 **어디서도 emit되지 않는 죽은 Signal**이라 포팅하지 않았다
  (`emitStopRequested`도 정의만 있고 호출부가 전혀 없음).
- `QAbstractListModel`이 내부적으로 처리하던 "자리표시 아이템을 완성된
  아이템으로 교체" 알림(Qt는 `beginRemoveRows`/`beginInsertRows`로 뷰가
  자동으로 안다)은 Qt 신호 목록에 이름이 없었다 — 모델·뷰 연결이 그
  역할을 대신했기 때문이다. 이 파일에는 그 자동 연결이 없으므로
  `window.__cvdv2_onItemUpdated(item_id)`를 새로 정의했다(Qt 배관 제거의
  직접적 귀결이지 로직 개선이 아니다). 실제 페이로드 스키마(카드 렌더에
  필요한 필드 전체를 어떻게 JS에 넘길지)는 Phase C가 뷰 배선 시 정한다.
- `content_viewmodel.py`의 `start/stop/pause/resume`는 전부 "재emit"뿐이었다
  (상태 변경 없음). 이 중 `stop`/`pause`/`resume`는 `app/download_bridge.py`의
  `WebDownloadBridge`가 이미 같은 JS 이벤트(`onStopped`/`onPaused`/`onResumed`)를
  직접 dispatch하므로 **포팅하지 않았다**(중복 배관). `start`만 남겼다 —
  "다운로드 시작됨" 통지는 브리지 쪽에 대응하는 dispatch가 없어 유일한
  출처이기 때문이다.
- `update_progress`(Qt 모델 `dataChanged.emit` 트리거용)는 포팅하지 않았다 —
  `WebDownloadBridge`가 진행률을 이미 JS로 직접 보낸다. Python 쪽
  `item.download_progress` 등 필드를 실제로 읽는 소비처가 있는지는 `#221`
  (Phase B2)이 확인하기로 이미 정해져 있다.

**아이템 식별자(`item_id`)**: `#214` 조사 결론대로 카드 생성 시점(`ContentItem`
생성 직후)에 `uuid4().hex`를 `item.id`로 부여한다. `ContentItem.__init__`
자체는 건드리지 않았다(Qt 경로와 공유하는 파일이라 — 대신 생성 직후
속성으로 얹는다). 조회 완료 시 자리표시를 완성된 아이템으로 교체할 때
새 아이템에 **자리표시의 id를 그대로 이어받게** 한다 — 새로 발급하면
조회 중이던 카드가 JS 쪽에서 다른 카드로 보인다.

의존성 주입은 바인더(Phase C)가 넘긴다:
- worker_factory: `ContentWorkerWeb`류를 만드는 함수 (테스트 monkeypatch 지점,
  `content/manager.py`의 `_make_worker` 패턴과 동일)
- probe: 쓰기 프로브 콜러블
- messages: 실패 문구 콜러블 딕셔너리 (`invalid_path`/`save_failed`)
- on_download_requested: 해상도 확정된 아이템의 실제 다운로드 시작을 맡을
  콜백(Phase B2의 download_viewmodel_web.py가 공급 — `WebDownloadBridge.start`
  호출까지 담당). 원본의 `downloadRequested` Signal이 mainWindow의
  `startDownload`(콘텐츠 알림 + 실제 엔진 시작 둘 다 수행)로 연결되던 것과
  같은 자리다. 주입 안 하면 아무 일도 하지 않는다(테스트 편의).

로거 이름은 `"content.manager"`를 그대로 쓴다 — 유저 행위 로그의 소비자
주소를 고정한다(`#169`가 이미 세운 원칙, `caplog` 하드코딩 함정 회피).
"""

import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from app.dispatcher import Dispatcher
from app.viewmodels.item_state import ItemState
from app.viewmodels.path_gates import check_download_path
from content.data import ContentItem
from core.models.download_state import DownloadState
from core.utils.paths import build_output_path, ensure_unique_path

logger = logging.getLogger("content.manager")


class ContentViewModelWeb:
    def __init__(
        self,
        dispatcher: Dispatcher,
        worker_factory,
        probe,
        messages: dict,
        on_download_requested: Callable[[ContentItem], None] | None = None,
        max_workers: int = 4,
    ):
        self._dispatcher = dispatcher
        self._worker_factory = worker_factory
        self._probe = probe
        self._messages = messages
        self._on_download_requested = on_download_requested or (lambda item: None)

        self.items: list[ContentItem] = []
        self.downloadPath = ""
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        # 조회 중인 워커 → 자리표시 아이템. 결과가 도착할 때까지 참조를 잡아
        # 둔다 — content_viewmodel.py의 동일 이유(#124)를 그대로 유지한다
        self._pendingPlaceholders: dict = {}
        # 제출된 조회 Future — 테스트가 완료를 기다리는 관측 지점
        # (QThreadPool.waitForDone의 web 대응)
        self._pendingFutures: list = []

    def fetchContent(self, vod_url: str, cookies: dict, downloadPath: str) -> None:
        placeholder = ContentItem(
            vod_url, {'title': vod_url, 'category': '', 'channelName': '', 'createdDate': '', 'duration': 0},
            [], None, '', downloadPath, '', None,
        )
        placeholder.id = uuid.uuid4().hex
        placeholder.downloadState = ItemState.LOADING
        self.items.append(placeholder)
        self._dispatcher.dispatch_js("window.__cvdv2_onItemInserted", placeholder.id, len(self.items))

        worker = self._worker_factory(vod_url, cookies, downloadPath)
        self._pendingPlaceholders[worker] = placeholder

        def on_finished(result, content_type):
            self._dispatcher.dispatch(lambda: self._workerFinished(worker, result, content_type))

        def on_error(error_message):
            self._dispatcher.dispatch(lambda: self._workerError(worker, error_message))

        self._pendingFutures.append(self._executor.submit(worker.run, on_finished, on_error))

    def _workerFinished(self, worker, result, content_type) -> None:
        placeholder = self._pendingPlaceholders.pop(worker, None)
        if placeholder is None:
            return
        vod_url, metadata, unique_reps, resolution, base_url, downloadPath, liveRewindPlaybackJson = result
        self.downloadPath = downloadPath
        row = self._row_of(placeholder)
        if row is None:
            # 조회 중 유저가 카드를 삭제한 경우 — 결과를 버린다
            return
        item = ContentItem(vod_url, metadata, unique_reps, resolution, base_url, downloadPath, content_type, liveRewindPlaybackJson)
        item.id = placeholder.id  # 카드 정체성 이어받기 — 재발급 금지
        self.items[row] = item
        self._dispatcher.dispatch_js("window.__cvdv2_onItemUpdated", item.id)

    def _workerError(self, worker, error_message) -> None:
        placeholder = self._pendingPlaceholders.pop(worker, None)
        if placeholder is not None:
            row = self._row_of(placeholder)
            if row is not None:
                del self.items[row]
                self._dispatcher.dispatch_js("window.__cvdv2_onItemDeleted", placeholder.id, len(self.items))
        self._dispatcher.dispatch_js("window.__cvdv2_onContentError", error_message)

    def clrearFinishedItems(self) -> None:
        for item in [it for it in self.items if it.downloadState == DownloadState.FINISHED]:
            self.removeItem(item)

    def removeItem(self, item: ContentItem) -> None:
        row = self._row_of(item)
        if row is not None:
            del self.items[row]
            self._dispatcher.dispatch_js("window.__cvdv2_onItemDeleted", item.id, len(self.items))

    def downloadItem(self) -> None:
        found, item, _ = self.findItem()
        if found:
            try:
                # 사전 검사(#137): 존재+쓰기 프로브. 판정은 path_gates가 단일
                # 지점으로 담당하고(#169 — #146 ⓑ1), 프로브 수단은 주입받는다.
                writable, reason = check_download_path(item.download_path, self._probe)
                if reason == "missing":
                    raise ValueError(self._messages["invalid_path"]())
                if not writable:
                    logger.warning("쓰기 프로브 실패(%s): %r", reason, item.download_path)
                    self.fail(item, self._messages["save_failed"]())
                    return
                self.onDownload(item)
            except ValueError as e:
                logger.warning(
                    "다운로드 시작 거부 — 존재하지 않는 저장 경로: %r", item.download_path
                )
                self.fail(item, str(e))
            except Exception:
                logger.exception("다운로드 준비 실패: %s", item.title)
                self.fail(item, self._messages["save_failed"]())
        else:
            self._dispatcher.dispatch_js("window.__cvdv2_onAllFinished")

    def onDownload(self, item: ContentItem) -> None:
        """해상도가 정해진 아이템의 산출물 경로를 조립하고 다운로드를 요청한다."""
        if item:
            item.output_path = build_output_path(item.download_path, item.title, item.resolution)
        else:
            item.output_path = ensure_unique_path(os.path.join(item.download_path, "video.mp4"))

        if item.output_path:
            self._on_download_requested(item)

    def start(self, item: ContentItem) -> None:
        self._dispatcher.dispatch_js("window.__cvdv2_onItemStarted", item.id)

    def finish(self, item: ContentItem, download_time) -> None:
        item.download_time = download_time
        self._dispatcher.dispatch_js("window.__cvdv2_onItemFinished", item.id, True)
        self.emitFinishedRequest(item)

    def fail(self, item: ContentItem, message: str = "") -> None:
        """아이템을 실패 상태로 표시하고 배치 체인을 계속 진행한다 (#134)."""
        item.stateMessage = message
        item.downloadState = DownloadState.FAILED
        self._dispatcher.dispatch_js("window.__cvdv2_onItemFinished", item.id, False)
        self.emitFinishedRequest(item)

    def emitFinishedRequest(self, item: ContentItem) -> None:
        self._dispatcher.dispatch_js("window.__cvdv2_onItemFinishedRequest", item.id)
        self.downloadItem()

    def findItem(self):
        for item in self.items:
            if item.downloadState not in [DownloadState.FINISHED, DownloadState.FAILED, ItemState.LOADING]:
                return True, item, self._row_of(item)
        return False, None, None

    def hasLoadingItems(self) -> bool:
        return any(item.downloadState == ItemState.LOADING for item in self.items)

    def downloadResultCounts(self) -> tuple[int, int]:
        finished = sum(1 for item in self.items if item.downloadState == DownloadState.FINISHED)
        failed = sum(1 for item in self.items if item.downloadState == DownloadState.FAILED)
        return finished, failed

    def _row_of(self, item: ContentItem) -> int | None:
        try:
            return self.items.index(item)
        except ValueError:
            return None
