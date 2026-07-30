"""Qt 다운로드 브리지 — core DownloadService와 UI를 잇는 유일한 Signal emit 지점 (#75).

이슈 #72~#74를 거치며 download/download.py·download_m3u8.py·monitor.py·
monitor_m3u8.py·manager.py에 분산돼 있던 Qt 어댑터(콜백→Signal 변환)를
이 모듈 하나로 수렴했다. 다운로드 경로의 Signal은 이 모듈만 emit한다 —
다른 모듈(manager 파사드 포함)에서는 Signal을 발신하지 않는다.

스레드 경계 규칙 (core/models/events.py 계약이 정본):
- core 서비스·엔진의 콜백은 **워커 스레드에서 호출된다**.
- 어댑터 콜백은 **Signal emit까지만** 수행한다. emit은 스레드 세이프하며,
  UI 반영은 큐 연결로 메인 스레드 슬롯에서 일어난다. 콜백 안에서 위젯을
  직접 만들거나 만지는 것은 금지다.
- 예외적으로 item.post_process(진행 변환용 데이터 플래그, 위젯 아님)만
  구 어댑터와 같은 시점 보존을 위해 콜백에서 직접 기록한다.
- 완료·실패의 후처리(상태 정리, 파사드 정리)는 내부 Signal을 거쳐
  메인 스레드 슬롯에서 수행한다.

진행 이벤트 변환식(남은 시간·크기·속도·%)은 구 MonitorThread /
MonitorM3U8Thread의 계산과 동일하다.
"""

from time import gmtime, strftime

import requests
from PySide6.QtCore import QObject, Signal

from content.data import ContentItem
from core.downloaders.base import PostprocessError
from core.downloaders.hls_aes_downloader import DecryptionError
from core.models.events import ProgressEvent
from core.services.download_service import DownloadService
from download.data import DownloadData
from download.logger import DownloadLogger
from download.resolvers import resolve_aes_key, resolve_m3u8_base_url
from download.task import DownloadTask


def _failure_message_key(exc: BaseException) -> str | None:
    """다운로드 실패 예외를 안내 키로 매핑한다 (#134, #127의 조회 경로 방식).

    원시 예외 문자열은 유저에게 보내지 않는다 — PostprocessError는 ffmpeg
    stderr·실행 경로를, OSError는 전체 파일 경로를 품는다. 상세는 다운로드
    로그가 이미 담당한다. 매핑에 없는 예외는 None(사유 생략)으로 둔다.
    """
    if isinstance(exc, PostprocessError):
        return "Postprocessing failed"
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


class QtDownloadBridge(QObject):
    """DownloadService의 콜백을 기존 DownloadManager Signal 인터페이스로 변환하는 브리지.

    Signal 시그니처는 구 DownloadManager와 동일하다 — UI(mainWindow) 배선 무변경.
    """

    progress = Signal(str, str, str, int, object)
    paused = Signal(object)
    resumed = Signal(object)
    stopped = Signal(object)
    finished = Signal(object, str)
    # 실패 통지 (#134): (item, 번역된 사유 메시지). 사유가 없으면 빈 문자열
    failed = Signal(object, str)

    # 내부 전용: 워커 스레드 콜백 → 메인 스레드 후처리 슬롯 전환용
    _engineFinished = Signal()
    _engineFailed = Signal(object)

    def __init__(self, service: DownloadService | None = None, parent=None):
        super().__init__(parent)
        self._service = service or DownloadService(
            base_url_resolver=resolve_m3u8_base_url, key_resolver=resolve_aes_key
        )
        self.handle = None
        self.task: DownloadTask | None = None
        self.item: ContentItem | None = None
        self._engineFinished.connect(self._onEngineFinished)
        self._engineFailed.connect(self._onEngineFailed)

    # ============ 시작/일시정지/재개/중지 (구 DownloadManager 인터페이스) ============

    def start(self, item: ContentItem) -> None:
        """다운로드 한 건을 서비스에 제출한다 (구 DownloadManager.start).

        구 코드와 같은 순서를 보존한다: 공유 데이터·로거·태스크 생성 →
        RUNNING 전이+다운로드 정보 로깅(task.start) → 실행(서비스 제출).
        """
        self.item = item
        data = DownloadData(
            item.base_url, item.vod_url, item.output_path, item.resolution, item.content_type
        )
        task_logger = DownloadLogger()
        # DownloadTask가 상태 전이 흡수와 모델↔카드(item) 상태 연결을 담당한다
        self.task = DownloadTask(data, item, task_logger)
        self.task.start()

        self.handle = self._service.submit(
            data.content,
            data=data,
            task_logger=task_logger,
            on_progress=self._make_progress_relay(data, item),
            on_finished=self._engineFinished.emit,
            on_failed=self._relay_failed,
            on_merge_start=self._relay_merge_start,
        )

    def pause(self) -> None:
        """다운로드 일시정지 (구 DownloadManager.pause)."""
        self.task.pause()
        self.paused.emit(self.item)

    def resume(self) -> None:
        """다운로드 재개 (구 DownloadManager.resume)."""
        self.task.resume()
        self.resumed.emit(self.item)

    def stop(self) -> None:
        """다운로드 중지 (구 DownloadManager.stop). 병합 표시도 함께 해제한다."""
        if self.task is not None:
            self.task.stop()
        if self.item is not None:
            # 구 DownloadM3U8Thread가 run 종료 후 수행하던 WAITING 정리와 동일
            self.item.post_process = False
        self.stopped.emit(self.item)

    def removeThreads(self) -> None:
        """실행 중인 워커가 끝나기를 기다린 뒤 참조를 정리한다 (구 removeThreads)."""
        if self.handle is not None:
            self.handle.wait()
        self.handle = None
        self.task = None

    # ============ 워커 스레드 콜백 (emit까지만 수행) ============

    def _make_progress_relay(self, data: DownloadData, item: ContentItem):
        """ProgressEvent를 기존 progress Signal 인자로 변환해 emit하는 콜백을 만든다.

        데이터·아이템을 클로저로 캡처해 제출 직후 첫 콜백과의 레이스를 없앤다.
        엔진 관측 스레드에서 호출되므로 계산과 Signal emit까지만 수행한다.
        """
        is_segment_based = item.is_segment_based

        def relay(event: ProgressEvent) -> None:
            if is_segment_based:
                args = _segment_progress_args(event, data, item)
            else:
                args = _file_progress_args(event)
            self.progress.emit(*args, item)

        return relay

    def _relay_failed(self, exc: BaseException) -> None:
        """엔진 실패 콜백 — 병합 표시 해제 후 내부 Signal로 메인 스레드에 넘긴다."""
        if self.item is not None:
            self.item.post_process = False
        self._engineFailed.emit(exc)

    def _relay_merge_start(self) -> None:
        """엔진 병합 시작 콜백 — UI 병합 단계 플래그 (구 post_process = True)."""
        if self.item is not None:
            self.item.post_process = True

    # ============ 메인 스레드 후처리 슬롯 ============

    def _onEngineFinished(self) -> None:
        """정상 완료 후처리 (구 DownloadManager.finish의 잔여분).

        완료 전이·최종 진행 통지는 서비스가 이미 수행했다. 여기서는 소요 시간
        계산, 워커 종료 대기·참조 정리, 완료 Signal emit만 한다.
        """
        if self.handle is None:
            # 완료 직후 사용자가 중지·정리를 마친 경우 (구 finish의 task None 가드와 동일)
            return
        item = self.item
        download_time = strftime("%H:%M:%S", gmtime(self.handle.elapsed_seconds()))
        self.removeThreads()
        self.finished.emit(item, download_time)

    def _onEngineFailed(self, exc: BaseException) -> None:
        """실패 후처리 (#134) — FAILED 전이·참조 정리 후 failed Signal로 사유를 알린다.

        이전에는 exc를 읽지 않고 task.stop()으로 WAITING에 되돌려, 실패가
        유저의 정지와 구분되지 않았다 (#128 조사 ①). 이제 완료 경로
        (_onEngineFinished)와 같은 순서로 전이 → 참조 정리 → Signal emit을
        수행하고, 사유는 키 기반 매핑을 거친 번역 문자열만 내보낸다.
        """
        if self.handle is None:
            # 실패 도착 전에 사용자가 중지·정리를 마친 경우 (완료 경로의 가드와 동일)
            return
        item = self.item
        if self.task is not None:
            # WAITING(엔진이 이미 stop한 경우)·RUNNING 어느 쪽에서도 FAILED 전이가
            # 허용된다. 실행 루프는 이 시점에 이미 종료됐다 — WAITING만 종료
            # 신호로 보는 루프 구조(#131)에 새 감시 대상을 만들지 않는다
            self.task.fail()
        self.removeThreads()
        self.failed.emit(item, self._failure_message(exc))

    def _failure_message(self, exc: BaseException) -> str:
        """실패 사유를 유저 표시용 번역 문자열로 바꾼다. 매핑에 없으면 빈 문자열.

        lupdate가 `-no-obsolete`로 .ts를 재생성하므로 반드시 리터럴로 tr()을
        호출해 추출 대상을 유지한다 (content/worker.py의 _translate_key와 동일).
        """
        translated = {
            "Postprocessing failed": self.tr("Postprocessing failed"),
            "Decryption failed": self.tr("Decryption failed"),
            "Viewing permission required": self.tr("Viewing permission required"),
            "Video not found": self.tr("Video not found"),
            "Network connection error": self.tr("Network connection error"),
            "Failed to save file": self.tr("Failed to save file"),
        }
        key = _failure_message_key(exc)
        return translated.get(key, "") if key is not None else ""


# ============ 진행 이벤트 변환식 (구 MonitorThread / MonitorM3U8Thread) ============


def _file_progress_args(event: ProgressEvent) -> tuple[str, str, str, int]:
    """파일 다운로드 진행 변환 — 계산식은 구 MonitorThread.update_progress와 동일."""
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
    """세그먼트 기반(m3u8·hls_aes) 진행 변환 — 구 MonitorM3U8Thread.update_progress와 동일.

    전체 크기를 미리 알 수 없어 진행률은 세그먼트 수 기반이다. 병합 단계
    (item.post_process)에서는 병합된 세그먼트 수, 그 전에는 완료된 세그먼트 수를
    쓰고, 남은 시간은 평균 세그먼트 크기로 추정한다.

    병합 분모는 m3u8이 초기화 세그먼트(EXT-X-MAP) 1개를 더 병합하므로 +1이고,
    hls_aes(TS)는 초기화 세그먼트가 없어 세그먼트 수 그대로다 (#57).
    """
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
