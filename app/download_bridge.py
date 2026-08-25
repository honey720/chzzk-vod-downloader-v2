"""웹 다운로드 브리지 — core DownloadService와 pywebview UI를 잇는다 (#210, Phase A2).

`download/qt_bridge.py`(#75)의 후계자. 계산 로직(진행 이벤트 변환식, 실패
사유 매핑)은 프레임워크 무관한 순수 함수라 그대로 옮겼다 — Qt Signal emit이
`Dispatcher.dispatch_js()` 호출로 바뀐 것만 다르다.

스레드 경계 규칙 (qt_bridge.py와 동일한 계약, 전달 수단만 다르다):
- core 서비스·엔진의 콜백은 **워커 스레드에서 호출된다**.
- 어댑터 콜백은 **Dispatcher.dispatch()까지만** 수행한다 — 실제 실행(JS 호출
  포함)은 백엔드 스레드에서 큐를 소비하며 일어난다.
- 예외적으로 item.post_process(진행 변환용 데이터 플래그)만 구 어댑터와
  같은 시점 보존을 위해 콜백에서 직접(즉시) 기록한다 — 큐를 거치지 않는다.
- 완료·실패의 후처리(상태 정리)는 `Dispatcher.dispatch()`로 큐에 들어가
  백엔드 스레드에서 수행된다 — Qt의 내부 Signal(`_engineFinished`/
  `_engineFailed`)이 하던 역할과 동일하다.

**아이템 식별자에 대한 설계 결정**: `content.data.ContentItem`에는 아직
안정적인 id 필드가 없다(#210 조사 시점 확인). 이 모듈은 ContentItem 자체를
바꾸지 않고, 호출자(Phase B의 viewmodel)가 `item_id: str`를 명시적으로
공급하도록 한다 — JS 쪽은 이 문자열로만 카드를 식별하고, Python 쪽 엔진
로직은 기존처럼 ContentItem 객체로 동작한다.

**i18n 의존성**: 실패 사유 번역은 `translate: Callable[[str], str]`를
주입받는다(기본값은 항등 함수 — 키를 그대로 반환). `#212`(i18n JSON
카탈로그)가 아직 없으므로 지금은 영문 키가 그대로 나갈 수 있다 — A4 완료
후 실제 조회 함수를 주입하면 된다.
"""

import logging
from time import gmtime, strftime
from typing import Callable

import requests

from app.dispatcher import Dispatcher
from content.data import ContentItem
from core.downloaders.base import PostprocessError
from core.downloaders.hls_aes_downloader import DecryptionError
from core.models.events import ProgressEvent
from core.models.download_data import DownloadData
from core.services.download_service import DownloadService
from core.utils.ffmpeg import FFmpegNotFoundError
from download.logger import DownloadLogger
from download.resolvers import resolve_aes_key, resolve_m3u8_base_url
from download.task import DownloadTask

logger = logging.getLogger(__name__)

# 중지·완료 정리 시 워커 종료를 기다리는 상한(초) (qt_bridge.py의 #137과 동일 근거).
_HANDLE_WAIT_TIMEOUT_S = 2.0


def _failure_message_key(exc: BaseException) -> str | None:
    """다운로드 실패 예외를 안내 키로 매핑한다 (qt_bridge.py에서 그대로 이식, #134/#127/#180).

    원시 예외 문자열은 유저에게 보내지 않는다. 매핑에 없는 예외는 None(사유 생략)으로 둔다.
    """
    if isinstance(exc, PostprocessError):
        if isinstance(exc.__cause__, FFmpegNotFoundError):
            return "Postprocessing failed - ffmpeg not found"
        return "Postprocessing failed - invalid segments"
    if isinstance(exc, DecryptionError):
        return "Decryption failed"
    if isinstance(exc, requests.HTTPError):
        status = exc.response.status_code if exc.response is not None else None
        if status in (401, 403):
            return "Viewing permission required"
        if status == 404:
            return "Video not found"
        return "Network connection error"
    if isinstance(exc, requests.RequestException):
        return "Network connection error"
    if isinstance(exc, OSError):
        return "Failed to save file"
    return None


class WebDownloadBridge:
    """DownloadService의 콜백을 Dispatcher를 통해 JS로 전달하는 브리지."""

    def __init__(
        self,
        dispatcher: Dispatcher,
        service: DownloadService | None = None,
        translate: Callable[[str], str] | None = None,
    ):
        self._dispatcher = dispatcher
        self._service = service or DownloadService(
            base_url_resolver=resolve_m3u8_base_url, key_resolver=resolve_aes_key
        )
        self._translate = translate or (lambda key: key)
        self.handle = None
        self.task: DownloadTask | None = None
        self.item: ContentItem | None = None
        self.item_id: str | None = None

    # ============ 시작/일시정지/재개/중지 ============

    def start(self, item_id: str, item: ContentItem) -> None:
        """다운로드 한 건을 서비스에 제출한다 (qt_bridge.py의 start와 동일한 순서)."""
        self.item_id = item_id
        self.item = item
        data = DownloadData(
            item.base_url, item.vod_url, item.output_path, item.resolution, item.content_type
        )
        task_logger = DownloadLogger()
        self.task = DownloadTask(data, item, task_logger)
        self.task.start()

        self.handle = self._service.submit(
            data.content,
            data=data,
            task_logger=task_logger,
            on_progress=self._make_progress_relay(data, item, item_id),
            on_finished=self._relay_finished,
            on_failed=self._relay_failed,
            on_merge_start=self._relay_merge_start,
        )

    def pause(self) -> None:
        """다운로드 일시정지. 사용자 조작으로 호출되므로 즉시 처리+즉시 통지한다."""
        self.task.pause()
        self._dispatcher.dispatch_js("window.__cvdv2_onPaused", self.item_id)

    def resume(self) -> None:
        """다운로드 재개."""
        self.task.resume()
        self._dispatcher.dispatch_js("window.__cvdv2_onResumed", self.item_id)

    def stop(self) -> None:
        """다운로드 중지. 병합 표시도 함께 해제한다."""
        if self.task is not None:
            self.task.stop()
        if self.item is not None:
            self.item.post_process = False
        self._dispatcher.dispatch_js("window.__cvdv2_onStopped", self.item_id)

    def removeThreads(self) -> None:
        """실행 중인 워커의 종료를 상한을 두고 기다린 뒤 참조를 정리한다 (qt_bridge.py의 #137 그대로)."""
        if self.handle is not None and not self.handle.wait(_HANDLE_WAIT_TIMEOUT_S):
            logger.warning(
                "워커가 %.0f초 안에 끝나지 않아 대기를 포기한다 — 슬롯 방출 (#137)",
                _HANDLE_WAIT_TIMEOUT_S,
            )
            self._service.abandon(self.handle)
        self.handle = None
        self.task = None

    # ============ 워커 스레드 콜백 (dispatch까지만 수행) ============

    def _make_progress_relay(self, data: DownloadData, item: ContentItem, item_id: str):
        """ProgressEvent를 JS 인자로 변환해 dispatch_js를 큐에 넣는 콜백을 만든다.

        데이터·아이템을 클로저로 캡처해 제출 직후 첫 콜백과의 레이스를 없앤다
        (qt_bridge.py와 동일한 이유).
        """
        is_segment_based = item.is_segment_based

        def relay(event: ProgressEvent) -> None:
            if is_segment_based:
                args = _segment_progress_args(event, data, item)
            else:
                args = _file_progress_args(event)
            self._dispatcher.dispatch_js("window.__cvdv2_onProgress", item_id, *args)

        return relay

    def _relay_finished(self) -> None:
        """엔진 완료 콜백 — 워커 스레드에서 호출된다. 후처리는 백엔드 스레드로 넘긴다."""
        self._dispatcher.dispatch(self._on_engine_finished)

    def _relay_failed(self, exc: BaseException) -> None:
        """엔진 실패 콜백 — 병합 표시 해제는 즉시(qt_bridge.py와 동일), 후처리는 큐로."""
        if self.item is not None:
            self.item.post_process = False
        self._dispatcher.dispatch(lambda: self._on_engine_failed(exc))

    def _relay_merge_start(self) -> None:
        """엔진 병합 시작 콜백 — UI 병합 단계 플래그. 즉시 기록(qt_bridge.py와 동일)."""
        if self.item is not None:
            self.item.post_process = True

    # ============ 백엔드 스레드 후처리 (Dispatcher.pump가 실행) ============

    def _on_engine_finished(self) -> None:
        """정상 완료 후처리. #185류 사고 지점 — 반드시 백엔드 스레드(단일 소비자)에서만 실행된다."""
        if self.handle is None:
            # 완료 직후 사용자가 중지·정리를 마친 경우
            return
        item_id = self.item_id
        download_time = strftime("%H:%M:%S", gmtime(self.handle.elapsed_seconds()))
        self.removeThreads()
        self._dispatcher.dispatch_js("window.__cvdv2_onFinished", item_id, download_time)

    def _on_engine_failed(self, exc: BaseException) -> None:
        """실패 후처리 (#134). #185류 사고 지점 — 반드시 백엔드 스레드(단일 소비자)에서만 실행된다.

        엔진 종료는 반드시 stop(WAITING)으로 한다 (qt_bridge.py의 PR #135 코멘트 근거 그대로).
        """
        if self.handle is None:
            return
        item_id = self.item_id
        if self.task is not None:
            self.task.stop()
        self.handle = None
        self.task = None
        self._dispatcher.dispatch_js("window.__cvdv2_onFailed", item_id, self._failure_message(exc))

    def _failure_message(self, exc: BaseException) -> str:
        """실패 사유를 유저 표시용 문자열로 바꾼다. 매핑에 없으면 빈 문자열."""
        key = _failure_message_key(exc)
        if key is None:
            return ""
        return self._translate(key)


# ============ 진행 이벤트 변환식 (qt_bridge.py에서 그대로 이식) ============


def _file_progress_args(event: ProgressEvent) -> tuple[str, str, str, int]:
    """파일 다운로드 진행 변환 — 계산식은 qt_bridge.py와 동일."""
    total_size = event.total_size or 0
    speed_mb = event.speed or 0.0

    progress = int((event.downloaded_size / total_size) * 100) if total_size > 0 else 0

    if speed_mb > 0:
        remaining_time = (total_size - event.downloaded_size) / (speed_mb * 1024 * 1024)
        remaining_time_str = strftime("%H:%M:%S", gmtime(remaining_time))
    else:
        remaining_time_str = "N/A"

    return remaining_time_str, str(event.downloaded_size), f"{speed_mb:.1f} MB/s", progress


def _segment_progress_args(
    event: ProgressEvent, data: DownloadData, item: ContentItem
) -> tuple[str, str, str, int]:
    """세그먼트 기반(m3u8·hls_aes) 진행 변환 — 계산식은 qt_bridge.py와 동일."""
    speed_mb = event.speed or 0.0
    merge_total = data.max_threads + (1 if item.content_type == "m3u8" else 0)

    if item.post_process:
        progress = int((data.merged_segments / merge_total) * 100) if merge_total > 0 else 0
    else:
        progress = (
            int((data.completed_threads / data.max_threads) * 100) if data.max_threads > 0 else 0
        )

    if speed_mb > 0 and data.completed_threads > 0:
        avg_segment_size = event.downloaded_size / data.completed_threads
        remaining_segments = data.max_threads - data.completed_threads
        remaining_time = (avg_segment_size * remaining_segments) / (speed_mb * 1024 * 1024)
        remaining_time_str = strftime("%H:%M:%S", gmtime(remaining_time))
    else:
        remaining_time_str = "N/A"

    return remaining_time_str, str(event.downloaded_size), f"{speed_mb:.1f} MB/s", progress
