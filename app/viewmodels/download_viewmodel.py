"""다운로드 viewmodel — core DownloadService와 UI를 잇는 유일한 Signal emit 지점 (#75 → #259 B1).

Phase 3(#72~#75)에서 download/download.py·download_m3u8.py·monitor.py·
monitor_m3u8.py·manager.py에 분산돼 있던 Qt 어댑터(콜백→Signal 변환)를
download/qt_bridge.py 한 모듈로 수렴했고, #170에서 그 브리지를 소유하는 얇은
viewmodel(이 클래스)이 mainWindow의 릴레이 슬롯 6개를 흡수했다. B1(#259)에서
브리지 자체를 이 클래스로 흡수해 층이 하나가 됐다 — 다운로드 경로의 Signal은
이 모듈만 emit한다.

스레드 경계 규칙 (core/models/events.py 계약이 정본):
- core 서비스·엔진의 콜백은 **워커 스레드에서 호출된다**.
- 콜백은 **Signal emit까지만** 수행한다. emit은 스레드 세이프하며, content
  (ContentManager)의 반영은 큐 연결로 메인 스레드 슬롯에서 일어난다. 콜백
  안에서 content를 직접 부르거나 위젯을 만지는 것은 금지다 — 그러면 위젯이
  워커 스레드에서 갱신된다. tests/unit/test_download_viewmodel_contract.py의
  스레드 축 게이트가 이 경계를 잰다.
- 예외적으로 item.post_process(진행 변환용 데이터 플래그, 위젯 아님)만
  구 어댑터와 같은 시점 보존을 위해 콜백에서 직접 기록한다.
- 완료·실패의 후처리(상태 정리, 참조 정리)는 내부 Signal을 거쳐 메인
  스레드 슬롯에서 수행한다.

진행 이벤트 변환식(남은 시간·크기·속도·%)은 구 MonitorThread /
MonitorM3U8Thread의 계산과 동일하며 모듈 수준 함수로 둔다.
"""

import logging
from time import gmtime, strftime

import requests
from PySide6.QtCore import QObject, Signal

from app.viewmodels.data import ContentItem
from core.downloaders.base import PostprocessError
from core.downloaders.hls_aes_downloader import DecryptionError
from core.models.events import ProgressEvent
from core.services.download_service import DownloadService
from core.models.download_data import DownloadData
from core.utils.ffmpeg import FFmpegNotFoundError
from app.download_logger import DownloadLogger
from app.download_resolvers import resolve_aes_key, resolve_m3u8_base_url
from app.download_task import DownloadTask

logger = logging.getLogger(__name__)

# 중지·완료 정리 시 워커 종료를 기다리는 상한(초) (#137 — #136 제안 ①).
# 정상 종료는 대부분 즉시(워커가 청크 단위로 상태를 확인)지만, 파일 I/O에
# 갇힌 워커는 영원히 안 끝난다 — 여기는 메인 스레드라 무한 대기가 곧 앱
# 프리즈였다. 네트워크 read에 막힌 워커(최대 30초)도 UI를 잡아둘 가치가
# 없어 짧게 둔다
_HANDLE_WAIT_TIMEOUT_S = 2.0


def _failure_message_key(exc: BaseException) -> str | None:
    """다운로드 실패 예외를 안내 키로 매핑한다 (#134, #127의 조회 경로 방식).

    원시 예외 문자열은 유저에게 보내지 않는다 — PostprocessError는 ffmpeg
    stderr·실행 경로를, OSError는 전체 파일 경로를 품는다. 상세는 다운로드
    로그가 이미 담당한다. 매핑에 없는 예외는 None(사유 생략)으로 둔다.
    """
    if isinstance(exc, PostprocessError):
        # PostprocessError는 항상 FFmpegError를 원인으로 체인한다(base.py의
        # 유일한 raise 지점, `raise PostprocessError(...) from e`). 원인
        # 유형으로 안내를 가른다 — "ffmpeg 실행 파일을 못 찾음"과 "ffmpeg는
        # 돌았지만 입력이 무효함"은 유저가 할 수 있는 조치가 다르다(#180
        # 조사 — 이 둘을 하나의 문구로 뭉뚱그린 탓에 macOS 실기 진단이
        # ffmpeg 설치 문제로 잘못 쏠렸다)
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


class DownloadViewModel(QObject):
    """다운로드 한 건의 수명 주기를 소유하고 core 콜백을 content 반영 호출로 잇는 viewmodel.

    위쪽 경계는 DownloadService(제출·핸들·콜백 4개), 아래쪽 경계는 content
    (update_progress/pause/resume/stop/finish/fail). Signal 시그니처는 구
    DownloadManager·QtDownloadBridge와 동일하다.
    """

    progress = Signal(str, str, str, int, object)
    paused = Signal(object)
    resumed = Signal(object)
    stopped = Signal(object)
    finished = Signal(object, str)
    # 실패 통지 (#134): (item, 번역된 사유 메시지). 사유가 없으면 빈 문자열
    failed = Signal(object, str)

    # 내부 전용: 워커 스레드 콜백 → 메인 스레드 후처리 슬롯 전환용 큐 연결.
    # ⚠️ 이 둘을 직접 호출로 바꾸면 참조 정리·content 통지가 워커 스레드에서
    # 일어난다 — 스레드 축 게이트가 잡는다
    _engineFinished = Signal()
    _engineFailed = Signal(object)

    def __init__(self, content, service: DownloadService | None = None, parent=None):
        """content: 다운로드 이벤트를 반영할 상대 — update_progress/pause/resume/
        stop/finish/fail을 가진 객체(ContentManager 바인더)를 받는다.
        service: DownloadService 주입 이음새(테스트 대역용). None이면 제품과
        같이 resolver를 끼운 실제 서비스를 만든다."""
        super().__init__(parent)
        self._service = service or DownloadService(
            base_url_resolver=resolve_m3u8_base_url, key_resolver=resolve_aes_key
        )
        self.handle = None
        self.task: DownloadTask | None = None
        self.item: ContentItem | None = None
        self._engineFinished.connect(self._onEngineFinished)
        self._engineFailed.connect(self._onEngineFailed)
        # 구 mainWindow.setupThreadSignals의 다운로드 릴레이 6개 — 위임 없이 직결.
        # 워커 스레드에서 emit되는 progress도 이 연결이 큐로 메인 스레드에 배달한다
        self.progress.connect(content.update_progress)
        self.paused.connect(content.pause)
        self.resumed.connect(content.resume)
        self.stopped.connect(content.stop)
        self.finished.connect(content.finish)
        self.failed.connect(content.fail)

    def isDownloading(self) -> bool:
        """활성 다운로드 핸들 존재 여부 — 구 d_thread/m_thread truthiness 폴링 대체."""
        return self.handle is not None

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
        """실행 중인 워커의 종료를 상한을 두고 기다린 뒤 참조를 정리한다 (구 removeThreads).

        상한 초과 시(파일 I/O에 갇힌 워커 — #136) 서비스 슬롯을 방출하고
        참조만 정리한다 (#137). 부분 산출물 정리가 생략되는 것은 아니다 —
        정리는 wait가 아니라 엔진 스레드(run 꼬리의 _cleanup_partial)가
        수행하므로, 포기해도 정리는 늦어질 뿐이며 갇힌 스레드가 깨어나면
        그때 수행된다. 재시작 충돌은 산출물 경로 유일화(#105)가 막는다.
        """
        if self.handle is not None and not self.handle.wait(_HANDLE_WAIT_TIMEOUT_S):
            logger.warning(
                "워커가 %.0f초 안에 끝나지 않아 대기를 포기한다 — 슬롯 방출 (#137)",
                _HANDLE_WAIT_TIMEOUT_S,
            )
            self._service.abandon(self.handle)
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
        """실패 후처리 (#134) — 엔진 종료 신호 후 참조를 정리하고 failed Signal로 사유를 알린다.

        이전에는 exc를 읽지 않고 버려서 실패가 유저의 정지와 구분되지 않았다
        (#128 조사 ①). 사유는 키 기반 매핑을 거친 번역 문자열만 내보낸다.

        엔진 종료는 반드시 stop(WAITING)으로 한다 (죽은 네트워크 드라이브
        프리즈 회귀 — PR #135 코멘트). 워커 예외 경로(_download_completed_callback)의
        실패는 실행 루프가 살아 있는 중에 통지되는데, 루프는 WAITING만 종료
        신호로 보므로 여기서 모델을 FAILED로 전이하면 루프가 영원히 돌고,
        FAILED→WAITING은 불허 전이라 되돌릴 수도 없다. FAILED 표시는 모델이
        아니라 아이템 레벨(ContentManager.fail)에서 한다 — 사전 경로 검사
        실패와 같은 방식이다.

        참조 정리는 완료 경로와 달리 handle.wait() 없이 한다: run()의 꼬리
        정리(_cleanup_partial)가 죽은 마운트 I/O에 갇힐 수 있어, 메인 스레드가
        기다리면 UI가 얼어붙는다. 엔진 스레드는 stop 신호로 스스로 끝난다
        (v2.9.0·main도 실패 시 기다리지 않았다).
        """
        if self.handle is None:
            # 실패 도착 전에 사용자가 중지·정리를 마친 경우 (완료 경로의 가드와 동일)
            return
        item = self.item
        if self.task is not None:
            self.task.stop()
        self.handle = None
        self.task = None
        self.failed.emit(item, self._failure_message(exc))

    def _failure_message(self, exc: BaseException) -> str:
        """실패 사유를 유저 표시용 번역 문자열로 바꾼다. 매핑에 없으면 빈 문자열.

        lupdate가 `-no-obsolete`로 .ts를 재생성하므로 반드시 리터럴로 tr()을
        호출해 추출 대상을 유지한다 (content_viewmodel.py의 _translate_key와 동일).
        번역 컨텍스트는 이 클래스 이름(DownloadViewModel)이다 — B1(#259)에서
        QtDownloadBridge 컨텍스트의 번역문 7건을 그대로 옮겨 왔다.
        """
        # 문구 규약(#245): **첫 줄 = 핵심 한 줄(무엇을 해야 하는지 포함) /
        # 둘째 줄 = 상세.** 카드는 첫 줄만 3행에 올리고 전문을 툴팁으로 준다 —
        # 실패 사유는 마우스를 올려야 보이면 안 된다. 첫 줄 길이 상한은 언어별
        # (en ≤ 60자, ko ≤ 40자 — 640px 실측, tests/unit/test_card_state_matrix.py).
        # 왼쪽 키(매핑 키)는 내부 식별자라 그대로 두고 표시 문자열만 바꿨다.
        translated = {
            "Postprocessing failed - ffmpeg not found": self.tr(
                "ffmpeg not found · check the installation\n"
                "Postprocessing failed: the ffmpeg executable could not be found."
            ),
            "Postprocessing failed - invalid segments": self.tr(
                "Segments are corrupted · download the video again\n"
                "Postprocessing failed: the downloaded segments look corrupted. "
                "Please download the video again."
            ),
            "Decryption failed": self.tr(
                "Decryption failed · check your cookies\n"
                "The video could not be decrypted. Check the cookies in Settings and try again."
            ),
            "Viewing permission required": self.tr(
                "Viewing permission required · add cookies in Settings\n"
                "This video requires viewing permission. Register the cookies of an account "
                "that can watch it in Settings."
            ),
            "Video not found": self.tr(
                "Video not found · check the URL\nThe video could not be found. Check the address."
            ),
            "Network connection error": self.tr(
                "Network connection error · check your connection\n"
                "A network error occurred while downloading. Check your connection and try again."
            ),
            "Failed to save file": self.tr(
                "Failed to save file · check the path and disk space\n"
                "The file could not be saved. Check the download path and free disk space."
            ),
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
