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

import dataclasses
import logging
import os
import threading
import time
from time import gmtime, strftime

import requests
from PySide6.QtCore import QObject, Qt, QTimer, Signal

from app.process_memory import log_process_memory
from app.viewmodels.data import ContentItem
from app.viewmodels.section_edit_viewmodel import take_section_head
from core.api.playback_tracks import StreamSelectionError
from core.downloaders.base import PostprocessError
from core.downloaders.hls_aes_downloader import DecryptionError
from core.downloaders.integrity import TruncatedSegmentError
from core.models.events import ProgressEvent
from core.services.download_service import DownloadService
from core.models.download_data import DownloadData
from core.models.mp4_index import Mp4Raw
from core.models.section_resume import SectionResume
from core.utils.hybrid_cut import CutError
from core.utils.paths import (
    build_section_output_paths,
    release_output_paths,
    reserve_section_output_paths,
)
from core.utils.ffmpeg import FFmpegNotFoundError
from core.utils.timecode import frame_index
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

# 구간 다운로드의 진행 막대는 "지난 시간 ÷ 예상 전체 시간"이다 (#309). 전송에 걸리는 시간은
# 재면서 알고, 컷에 걸릴 시간은 아래 두 값으로 어림한다:
#
#     컷 예상 시간(초) = 자를 구간 수 × CUT_SECONDS_PER_SECTION
#                      + 자를 구간 길이의 합(초) × CUT_SECONDS_PER_MEDIA_SECOND
#
# 전송과 컷의 몫을 고정하지 않는다 — 회선이 빠르면 컷이, 느리면 전송이 대부분이다(실측:
# 8시간 영상에서 구간 2개 678MB는 전송 6.8초 · 컷 13.9초, 구간 3개 73MB는 전송 1.3초 · 컷 5.5초).
# 값은 그 두 실측과 합성 영상의 단계별 측정에서 정했다 — 구간마다 ffmpeg를 다섯 번쯤 띄우는
# 고정비가 약 1초, 길이에 비례하는 몫(대부분 오디오 재인코딩)이 1초에 약 0.018초다.
# 어림이 틀려도 막대는 거꾸로 가지 않고, 100은 마지막 구간을 다 잘랐을 때만 된다
CUT_SECONDS_PER_SECTION = 1.0  # 구간 하나의 고정비(초)
CUT_SECONDS_PER_MEDIA_SECOND = 0.018  # 구간 길이 1초에 드는 시간(초)

_clock = time.monotonic  # 진행 막대가 지난 시간을 재는 시계 — 테스트가 바꿔 끼운다

# 다운로드 준비(엔진이 받을 것을 정하는 단계 — 구간 다운로드는 moov · 플레이리스트를 받는다)가
# 이보다 오래 걸릴 때만 카드에 "준비 중"을 보인다(ms). moov를 다시 쓰는 카드의 준비는 0.2초
# 안팎이라, 바로 보이면 문구가 깜빡이기만 한다
PREPARE_NOTICE_DELAY_MS = 500


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
        if isinstance(exc.__cause__, CutError):
            # 구간을 자르다 실패했다 (#309) — 받은 데이터가 손상된 것이 아니다. 컷은 ffmpeg의
            # 실패를 CutError로 바꿔 올리므로 그 원인이 "실행 파일 없음"이면 그 안내를 준다
            if isinstance(exc.__cause__.__cause__, FFmpegNotFoundError):
                return "Postprocessing failed - ffmpeg not found"
            return "Section cut failed"
        return "Postprocessing failed - invalid segments"
    if isinstance(exc, DecryptionError):
        return "Decryption failed"
    if isinstance(exc, StreamSelectionError):
        # 고른 해상도의 스트림을 마스터 플레이리스트에서 하나로 정하지 못했다 (#318)
        return exc.message_key
    if isinstance(exc, TruncatedSegmentError):
        # 다시 받아도 세그먼트가 계속 잘려 왔다 (#321)
        return exc.message_key
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
    # 내부 전용: 받을 구간이 하나도 없는 카드 — 엔진에 넘기지 않고 실패로 끝낸다. 큐로 돌려
    # start()가 돌아간 뒤에 끝낸다(끝내는 통지가 다음 카드의 start()를 부른다)
    _nothingToReceive = Signal(object)

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
        # 실행 중인 다운로드의 엔진 공유 데이터 — 구간 상태(완료 · 실패 수)를 여기서 읽는다
        self._data: DownloadData | None = None
        # 실행 중인 다운로드가 무엇을 받아 어디에 쓰는지를 가리키는 값 — 일부 구간 실패로 끝나면
        # 남긴 것과 함께 아이템에 적어, 다음 다운로드가 같은 값일 때만 이어받게 한다
        self._resume_key: tuple | None = None
        # 실행 중인 다운로드에서 엔진에 넘기지 않고 뺀 구간 수 (#309) — 새 영상의 끝 이후에서
        # 시작해 받을 수 없는 구간이다. 실패한 구간 수에 더해 카드에 보인다
        self._excluded = 0
        self._content = content
        # 준비가 길어지면 카드에 "준비 중"을 켜는 타이머 — 엔진의 첫 진행 통지가 끈다
        self._prepareTimer = QTimer(self)
        self._prepareTimer.setSingleShot(True)
        self._prepareTimer.setInterval(PREPARE_NOTICE_DELAY_MS)
        self._prepareTimer.timeout.connect(self._showPreparing)
        # 진행 통지가 메인 스레드에 닿으면 구간 상태를 아이템에 먼저 옮긴다 — content보다
        # 먼저 연결해, content가 카드를 다시 그릴 때 값이 이미 들어 있게 한다
        self.progress.connect(self._onPrepared)
        self.progress.connect(self._syncSections)
        self._engineFinished.connect(self._onEngineFinished)
        self._engineFailed.connect(self._onEngineFailed)
        self._nothingToReceive.connect(self._onNothingToReceive, Qt.ConnectionType.QueuedConnection)
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
        # 해상도가 같은 두 스트림을 가르는 값 — 다운로드 시작 때 그 변형을 다시 찾는다 (#318)
        data.content.stream = getattr(item, "stream", None)
        selections = tuple(getattr(item, "selections", ()) or ())
        self._excluded = 0
        if selections:
            # 구간 다운로드 (#309) — 구간 파일명은 시작할 때 한꺼번에 배정한다. 예약은 엔진이
            # 끝날 때 푼다.
            # 받을 수 없는 구간(새 영상의 끝 이후에서 시작한다)은 엔진에 넘기지 않는다 — 엔진의
            # 길이 검사는 위반 구간이 하나라도 있으면 다운로드 전체를 실패시킨다. 그 구간만
            # 빼고 나머지를 받은 뒤, 뺀 수를 실패한 구간 수에 더해 일부 실패로 끝낸다
            unfit = _unfit_sections(item, len(selections))
            kept = [number for number in range(len(selections)) if number not in unfit]
            self._excluded = len(unfit)
            received = tuple(selections[number] for number in kept)
            self._resume_key = _resume_key(item)
            resume = _usable_resume(item, received)
            if resume is not None:
                # 일부 구간만 실패한 다운로드를 이어서 처리한다 — 구간 파일 이름을 새로 배정하지
                # 않고 그때의 경로를 그대로 쓴다. 새로 배정하면 남아 있는 구간 파일 때문에
                # 모든 구간이 새 이름(" (n)")을 받는다
                paths = reserve_section_output_paths(resume.paths, resume.done)
                if paths != resume.paths:
                    # 끝내지 않은 구간의 이름이 그사이 다른 것에 차지돼 바뀌었다 — 기록도 맞춘다
                    resume = dataclasses.replace(resume, paths=paths)
                item.sections_done = len(resume.done)
            else:
                item.section_retry = None
                # 파일 번호는 구간 목록의 순서다 — 뺀 구간의 번호는 비운다(3개 중 2번을 빼면
                # _1 · _3). 번호를 당기면 유저가 정한 순서와 파일 이름이 어긋난다
                numbered = build_section_output_paths(
                    item.download_path, item.title, item.resolution, len(selections)
                )
                release_output_paths(numbered[number] for number in unfit)
                paths = tuple(numbered[number] for number in kept)
                item.sections_done = 0
            item.sections_failed = 0
            if self._excluded and item.sections_done == len(kept):
                # 받을 구간이 없다 — 전부 빠졌거나, 남은 구간은 이전 실행이 이미 끝냈다.
                # 엔진에 넘기지 않고 실패로 끝낸다
                release_output_paths(paths)
                item.sections_failed = self._excluded
                self._data = None
                self._nothingToReceive.emit(item)
                return
            data.content.selections = received
            data.content.section_resume = resume
            data.content.selection_paths = paths
            item.section_paths = tuple(paths)  # 완료 카드의 폴더 열기가 여기서 구간 파일을 찾는다
            # 구간을 정하며 받은 moov를 넘긴다 (#309) — 주소가 같을 때만. 엔진이 다시 받지 않는다.
            # 카드에서는 비운다 — 이제 엔진이 들고, 다운로드가 끝나면 함께 사라진다
            moov = take_section_head(item)
            if isinstance(moov, Mp4Raw):
                # 아직 색인을 만들지 않았다 — 바이트를 넘긴다. 엔진이 받지 않고 해석만 한다
                data.content.mp4_raw = moov
            else:
                data.content.mp4_head = moov
        self._data = data
        item.transfer_bytes = None
        item.preparing = False
        self._prepareTimer.start()
        log_process_memory("다운로드 시작")
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

    def prepareRetry(self, item: ContentItem) -> None:
        """실패 카드의 재시도를 누른 순간에 한 번, 끝낸 구간의 파일이 그대로 있는지 확인한다 (#309).

        일부 구간만 실패한 구간 다운로드에만 할 일이 있다. 끝낸 것으로 적힌 구간 가운데
        파일이 없어진 것은 끝나지 않은 구간으로 돌린다 — 다음 다운로드가 그 구간도 다시
        만든다. 확인은 여기서만 한다. 다운로드를 시작하지 않는다.
        """
        retry = getattr(item, "section_retry", None)
        if retry is None:
            return
        resume_key, resume = retry
        done = frozenset(number for number in resume.done if os.path.isfile(resume.paths[number]))
        item.section_retry = (resume_key, dataclasses.replace(resume, done=done))

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
        self._endPreparing()
        self._dropHead()
        if self.item is not None:
            # 구 DownloadM3U8Thread가 run 종료 후 수행하던 WAITING 정리와 동일
            self.item.post_process = False
        self.stopped.emit(self.item)

    def _dropHead(self) -> None:
        """엔진에 넘겼던 moov를 놓는다 (#309) — 다운로드가 끝났다(완료 · 실패 · 정지).

        긴 영상의 해석된 색인은 수백 MB다. 이 뷰모델은 다음 다운로드를 시작할 때까지 마지막
        다운로드의 데이터를 들고 있으므로 여기서 놓지 않으면 그때까지 남는다. 일부 구간만
        실패해 엔진이 남긴 이어받기 기록(``section_retry``)의 moov는 그대로 둔다 — 재시도가 쓴다.
        """
        if self._data is not None:
            self._data.content.mp4_head = None
            self._data.content.mp4_raw = None

    def _showPreparing(self) -> None:
        """준비가 길어지고 있다 — 카드에 "준비 중"을 켠다 (#309). 타이머가 부른다."""
        item = self.item
        if item is None or self.handle is None:
            return
        item.preparing = True
        self._content.model.notifyChanged(item)

    def _endPreparing(self) -> None:
        """준비가 끝났다(또는 다운로드가 끝났다) — "준비 중"을 끄고 타이머를 멈춘다.

        카드를 다시 그리지는 않는다 — 부르는 쪽의 통지(진행 · 완료 · 실패 · 정지)가 그린다.
        """
        self._prepareTimer.stop()
        if self.item is not None:
            self.item.preparing = False

    def _onPrepared(self, *_args) -> None:
        """진행 통지가 왔다 — 엔진의 준비가 끝났다 (#309). 메인 스레드에서 돈다.

        엔진은 준비가 끝나면 받기 전에 진행을 한 번 알린다. "준비 중"을 끄고, 인코딩 완료
        VOD면 엔진이 정한 받을 크기를 카드에 옮긴다. 구간 다운로드에서는 그 값이 받을 구간의
        합이다 — 이어받기와 뺀 구간이 반영된 값이다.
        """
        self._endPreparing()
        item, data = self.item, self._data
        if item is None or data is None or item.is_segment_based or not data.total_size:
            return
        item.transfer_bytes = data.total_size
        if data.sections_total:
            item.section_bytes = data.total_size

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
        sections = _SectionProgress(_cut_seconds(data.content))

        def relay(event: ProgressEvent) -> None:
            if data.sections_total:
                args = _section_progress_args(event, data, item, sections)
            elif is_segment_based:
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

    def _syncSections(self, *_args) -> None:
        """엔진의 구간 상태(완료 · 실패한 구간 수)를 아이템에 옮긴다 (#309). 메인 스레드에서 돈다.

        수는 통지가 몇 번 왔는지로 세지 않는다 — 같은 진행 통지가 두 번 올 수 있다(서비스는
        완료 뒤에 진행을 한 번 더 알린다). 엔진이 공유 데이터에 적어 둔 값을 그대로 읽는다.
        """
        if self.item is None or self._data is None or not self._data.sections_total:
            return
        self.item.sections_done = self._data.sections_done
        # 엔진에 넘기지 않고 뺀 구간도 받지 못한 구간이다
        self.item.sections_failed = self._data.sections_failed + self._excluded

    def _onEngineFinished(self) -> None:
        """정상 완료 후처리 (구 DownloadManager.finish의 잔여분).

        완료 전이·최종 진행 통지는 서비스가 이미 수행했다. 여기서는 소요 시간
        계산, 워커 종료 대기·참조 정리, 완료 Signal emit만 한다.
        """
        if self.handle is None:
            # 완료 직후 사용자가 중지·정리를 마친 경우 (구 finish의 task None 가드와 동일)
            return
        item = self.item
        self._syncSections()
        self._endPreparing()
        log_process_memory("다운로드 끝")
        self._dropHead()
        if self._excluded:
            # 넘긴 구간은 모두 만들었지만 뺀 구간이 있다 (#309) — 일부 실패로 끝낸다. 넘긴
            # 구간을 모두 끝낸 것으로 적어 둔다: 재시도해도 뺀 구간은 다시 빠지고, 만든 파일은
            # 다시 받지 않는다
            content = self._data.content
            item.section_retry = (
                self._resume_key,
                SectionResume(
                    selections=tuple(content.selections),
                    paths=tuple(content.selection_paths),
                    done=frozenset(range(len(content.selections))),
                ),
            )
            self.removeThreads()
            self.failed.emit(item, self._outsideVideoMessage())
            return
        item.section_retry = None  # 모든 구간을 만들었다 — 이어받을 것이 없다
        download_time = strftime("%H:%M:%S", gmtime(self.handle.elapsed_seconds()))
        self.removeThreads()
        self.finished.emit(item, download_time)

    def _onNothingToReceive(self, item: ContentItem) -> None:
        """받을 구간이 하나도 없는 카드를 실패로 끝낸다 (#309). 엔진은 돌지 않았다."""
        self.failed.emit(item, self._outsideVideoMessage())

    def _outsideVideoMessage(self) -> str:
        """받을 수 없어 뺀 구간이 있는 카드의 실패 사유 — 첫 줄이 카드에 오르고 전문은 툴팁이다."""
        return self.tr(
            "Section is outside the video · edit the sections\n"
            "Sections that start after the end of this resolution were skipped. "
            "Edit the sections or pick another resolution."
        )

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
        self._syncSections()  # 일부 구간만 실패했을 때 카드가 완료 · 실패 수를 보인다
        resume = self._data.section_resume if self._data is not None else None
        if resume is not None:
            # 엔진이 끝낸 구간과 받아 둔 데이터를 남겼다 — 다음 다운로드가 실패한 구간만 다시
            # 처리한다. 남기지 않은 실패(전송 실패 등)는 아이템에 있던 것을 그대로 둔다
            item.section_retry = (self._resume_key, resume)
            # 기록은 이제 카드가 든다 — 공유 데이터에 남겨 두면 다음 다운로드를 시작할 때까지
            # 이 뷰모델이 moov 색인을 하나 더 쥔다(카드가 기록을 지워도 풀리지 않는다)
            self._data.section_resume = None
        self._endPreparing()
        log_process_memory("다운로드 끝")
        self._dropHead()
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
            "Stream for the selected resolution not found": self.tr(
                "Stream not found · pick another resolution\n"
                "The stream for the selected resolution could not be found. "
                "Try another resolution."
            ),
            "Section cut failed": self.tr(
                "Could not cut the section · press retry\n"
                "Cutting the section failed. Retry processes only the failed sections."
            ),
            "Segment was received truncated": self.tr(
                "Video data arrived corrupted · try again later\n"
                "Part of the video kept arriving incomplete from the server. "
                "Try again later."
            ),
        }
        key = _failure_message_key(exc)
        return translated.get(key, "") if key is not None else ""


def _resume_key(item: ContentItem) -> tuple:
    """아이템이 지금 무엇을 받아 어디에 쓰려는지를 가리키는 값 (#309).

    받을 스트림(종류 · 주소 · 해상도 · 변형)과, 구간 파일이 놓일 자리(저장 폴더 · 파일명의
    바탕인 제목)다. 해상도 · 저장 폴더 · 제목 가운데 하나라도 바꾸면 달라진다.
    """
    return (
        item.content_type,
        item.base_url,
        item.resolution,
        getattr(item, "stream", None),
        item.download_path,
        item.title,
    )


def _unfit_sections(item: ContentItem, count: int) -> frozenset[int]:
    """엔진에 넘기지 않을 구간의 번호(0부터) — 카드에 받을 수 없다고 표시된 구간이다 (#309).

    조회로 확인된 길이를 기준으로 표시된 것만 뺀다. 길이를 확인하는 중이거나 확인하지 못한
    카드는 아무것도 빼지 않는다 — 그대로 넘기고 엔진이 실제 길이로 검사한다.
    """
    if getattr(item, "section_check", ""):
        return frozenset()
    return frozenset(n for n in getattr(item, "section_unfit", ()) if 0 <= n < count)


def _usable_resume(item: ContentItem, selections: tuple):
    """아이템에 남아 있는, 지금 시작하는 다운로드가 이어받을 수 있는 것을 돌려준다. 없으면 None.

    그때와 같은 스트림 · 같은 저장 폴더 · 같은 제목 · 같은 구간 목록일 때만 이어받는다.
    해상도를 바꿨으면 받아 둔 데이터와 만든 구간 파일이 다른 영상의 것이다. 저장 폴더나
    제목을 바꿨으면 남겨 둔 경로는 지금 고른 자리가 아니다 — 이어받으면 시작 전 쓰기 검사를
    거친 폴더가 아닌 곳에 쓰게 된다. 그때는 새 자리에 새 이름으로 처음부터 받는다.
    """
    retry = getattr(item, "section_retry", None)
    if retry is None:
        return None
    resume_key, resume = retry
    if resume_key != _resume_key(item):
        return None
    if resume.selections == selections:
        return resume
    return _resume_for_edited_sections(item, resume, selections)


def _resume_for_edited_sections(item: ContentItem, resume: SectionResume, selections: tuple):
    """구간을 편집한 카드가 이어받을 수 있는 것을 만든다. 이어받을 구간이 없으면 None (#309).

    **같은 번호에 같은 값(시작 · 끝 프레임)인 구간만** 끝낸 것으로 이어받는다 — 그 구간의 파일은
    다시 받지 않는다. 값이 바뀐 구간과 새로 생긴 구간은 새로 받는다. 번호와 값이 함께 맞는
    끝낸 구간이 하나도 없으면(순서를 바꾼 경우 등) None이다 — 기록을 버리고 처음부터 받는다.

    파일 이름은 번호를 따른다. 그때 있던 번호는 그때의 경로를 그대로 후보로 두고, 새로 생긴
    번호만 새로 배정한다. 후보 자리에 이미 파일이 있으면(값이 바뀐 구간의 옛 파일) 시작할 때
    ``reserve_section_output_paths``가 피한다 — 덮어쓰지 않는다.

    받아 둔 것 가운데 구간 목록에 묶인 것은 넘기지 않는다 — mp4의 임시 원본은 그때의 구간
    범위만 담고 있다. 구간과 무관한 것(moov · 플레이리스트 · 받아 둔 세그먼트)은 그대로 넘긴다.

    받을 수 없어 뺀 구간이 있는 카드는 대상이 아니다 — 뺀 뒤의 순서와 파일 번호가 어긋난다.
    """
    if _unfit_sections(item, len(item.selections)):
        return None
    fps = getattr(item, "section_frame_rate", None)

    def frames(selection) -> tuple:
        if fps is None:
            return (selection.start, selection.end)
        return (frame_index(selection.start, fps), frame_index(selection.end, fps))

    kept = frozenset(
        number
        for number in resume.done
        if number < len(selections)
        and frames(resume.selections[number]) == frames(selections[number])
    )
    if not kept:
        return None
    # 새로 생긴 번호의 이름만 얻는다 — 배정은 예약까지 하므로 곧바로 풀고, 시작할 때 다시 예약한다
    numbered = build_section_output_paths(
        item.download_path, item.title, item.resolution, len(selections)
    )
    release_output_paths(numbered)
    paths = tuple(
        resume.paths[number] if number < len(resume.paths) else numbered[number]
        for number in range(len(selections))
    )
    return dataclasses.replace(
        resume,
        selections=tuple(selections),
        paths=paths,
        done=kept,
        source_path=None,
        source_size=None,
        source_sections=None,
    )


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


def _cut_seconds(content) -> float:
    """이번 다운로드가 자를 구간들의 컷에 걸릴 시간의 어림(초). 구간이 없으면 0.

    이전 실행이 끝낸 구간(``section_resume.done``)은 자르지 않으므로 세지 않는다.
    """
    resume = getattr(content, "section_resume", None)
    done = resume.done if resume is not None else frozenset()
    todo = [
        selection
        for number, selection in enumerate(getattr(content, "selections", ()) or ())
        if number not in done
    ]
    length = sum(selection.end - selection.start for selection in todo)
    return len(todo) * CUT_SECONDS_PER_SECTION + length * CUT_SECONDS_PER_MEDIA_SECOND


class _SectionProgress:
    """구간 다운로드 하나의 진행 막대(%) — 지난 시간 ÷ 예상 전체 시간 (#309).

    전송 중에는 지금까지의 속도로 전송이 끝날 때를 어림하고, 그 뒤에 컷의 어림을 더한 것이
    전체다. 컷 단계에서는 전송에 실제로 걸린 시간과 컷의 어림이 전체이고, 컷의 진행만큼
    찬다. 내는 값은 줄지 않고, 100은 컷이 다 끝났을 때만 낸다. 엔진의 관측 스레드와 컷을
    돌리는 스레드가 함께 부른다.
    """

    def __init__(self, cut_seconds: float):
        self._cut_seconds = cut_seconds
        self._started = _clock()
        self._transfer_seconds: float | None = None  # 컷이 시작된 때까지 걸린 시간
        self._shown = 0
        self._lock = threading.Lock()

    def percent(self, transfer: float, cutting: bool, cut: float) -> int:
        """지금 막대에 보일 값(0~100).

        Args:
            transfer: 전송의 진행(0~1)
            cutting: 컷 단계에 들어갔는지
            cut: 컷의 진행(0~1) — 컷 단계에서만 쓴다
        """
        with self._lock:
            elapsed = _clock() - self._started
            if cutting:
                if self._transfer_seconds is None:
                    self._transfer_seconds = elapsed
                total = self._transfer_seconds + self._cut_seconds
                spent = self._transfer_seconds + self._cut_seconds * min(max(cut, 0.0), 1.0)
                value = 100 if cut >= 1.0 else min(int(100 * spent / total) if total else 0, 99)
            elif transfer > 0:
                # 전송 예상 시간 = 지난 시간 ÷ 전송의 진행. 지난 시간 ÷ (전송 예상 + 컷 예상)
                total = elapsed / transfer + self._cut_seconds
                value = min(int(100 * elapsed / total) if total else 0, 99)
            else:
                value = 0
            self._shown = max(self._shown, value)
            return self._shown


def _section_progress_args(
    event: ProgressEvent, data: DownloadData, item: ContentItem, sections: _SectionProgress
) -> tuple[str, str, str, int]:
    """구간 다운로드의 진행 변환 — 전송과 컷을 하나의 막대로 합친다 (#309).

    막대는 지난 시간 ÷ 예상 전체 시간이다(``_SectionProgress``). 전송의 진행은 전달 방식의
    단위 그대로다(파일은 받은 바이트 ÷ 받을 바이트, 세그먼트 기반은 받은 세그먼트 수 ÷ 받을
    세그먼트 수). 컷의 진행은 엔진이 적어 둔 값(``cut_progress`` — 구간 안에서도 오른다)이고,
    그 값이 없으면 (끝난 구간 수 ÷ 구간 수)다. 끝난 구간에는 자르지 못한 구간도 센다 — 그
    구간의 일은 끝났다.

    이전 실행이 끝낸 구간을 이어받은 다운로드는 이번에 처리할 구간만으로 센다 — 막대는 0에서
    다시 찬다. 카드의 완료 구간 수는 전체 기준 그대로다(2/3에서 이어진다).

    남은 시간 · 크기 · 속도는 전달 방식의 변환식 그대로다. 컷 단계에서는 카드가 그 값을
    쓰지 않고 단계 문구를 보인다.
    """
    if item.is_segment_based:
        remaining, size, speed, _percent = _segment_progress_args(event, data, item)
        transfer = data.completed_threads / data.max_threads if data.max_threads > 0 else 0.0
    else:
        remaining, size, speed, _percent = _file_progress_args(event)
        total_size = event.total_size or 0
        transfer = event.downloaded_size / total_size if total_size > 0 else 0.0
    cut = 0.0
    if item.post_process:
        todo = data.sections_total - data.sections_resumed
        handled = data.sections_done + data.sections_failed - data.sections_resumed
        cut = handled / todo if todo > 0 else 1.0
        reported = getattr(data, "cut_progress", None)
        if reported is not None and handled < todo:
            # 구간 안에서도 오르는 값. 다 찼다고 적혀 있어도 끝나지 않은 구간이 있으면 1로
            # 보지 않는다 — 100은 마지막 구간의 일이 끝났을 때(위의 1.0)만이다
            cut = min(reported, 0.999)
    transfer = min(max(transfer, 0.0), 1.0)
    progress = sections.percent(transfer, bool(item.post_process), cut)
    return remaining, size, speed, progress


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
