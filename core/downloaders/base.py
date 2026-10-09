"""다운로더 공통 실행 엔진 — BaseDownloader 추상 (#82, SPEC §6).

file(#73)·m3u8(#74) 엔진이 평행 중복으로 갖고 있던 실행 엔진을 이 클래스
한 곳으로 흡수했다: 워커 풀 관리·작업 큐잉·실패/저속 재큐잉, 적응형 스레드
스케일링과 관측 스레드, 일시정지/재개 처리, 진행률 집계 → ProgressEvent 통지.
로직·수식은 두 엔진에서 식 그대로 옮겼다 — 규칙은 기존 박제 테스트
(tests/unit/core/test_file_downloader_rules.py / test_m3u8_downloader_rules.py)가
시나리오·단언 무수정으로 고정한다.

하위 다운로더가 구현·오버라이드하는 것 (새 다운로더를 추가할 때 보는 목록):

필수 (추상):
- ``supports(content)``: 이 다운로더가 처리할 컨텐츠 타입 판정 — 서비스의
  선택 로직이 구체 클래스 분기 없이 이 답을 따른다
- ``prepare(content)``: "무엇을 받을지"를 DownloadPlan으로 만든다 (#83 —
  items는 file: 바이트 범위, m3u8: (index, 세그먼트) 튜플). 총 크기 조회·
  매니페스트 파싱 등 타입 고유 사전 조회는 여기서 한다. 계획의 총 크기·
  후처리 필요 여부는 run()이 계획에서 읽는다 — 실행 중 추측하지 않는다
- ``_download_item(item, part_num)``: 작업 1건의 다운로드 (재시도 판정 포함)
- ``_log_item_start(part_num, item)`` / ``_download_start_log_args()``:
  타입별 로그 형식 유지용
- ``_prepare_output()``: 수신 준비 (file: 빈 파일, m3u8: 임시 폴더·초기화 세그먼트)
- ``_cleanup_partial()``: 실패·중단 시 부분 산출물 정리

선택 (기본 구현 있음):
- ``postprocess()``: 다운로드 완료 후 마무리 (m3u8: 병합). 계획의
  requires_postprocess가 참일 때만 run()이 호출한다 (#83)
- ``_initial_queue(items)``: 시작 시 작업 큐 구성 (기본: 목록 그대로)
- ``_cleanup_after_run()``: 정상 경로 종료 후 정리 (기본 no-op)
- ``_progress_total_size()``: ProgressEvent.total_size (기본: 전체 크기,
  m3u8은 미리 알 수 없어 None)
- 클래스 속성: ``run_thread_name``(서비스 워커 스레드 이름),
  ``worker_pool_prefix``(풀 스레드 이름), ``requires_base_url_resolution``
  (다운로드 시작 전 base_url 해석 필요 여부 — 서비스가 resolver를 주입·실행),
  ``_failure_exceptions``(run이 실패로 처리할 예외 — 그 외는 전파)

스레드·콜백 규칙 (#72~#75와 동일):
- 관측(속도 측정·스레드 조정·진행 통지)은 엔진이 소유하는 일반 스레드
  (_monitor_loop)가 수행하고, core/models/events.py의 ProgressEvent 콜백으로
  보고한다. 완료·실패도 같은 계약의 콜백으로 알린다.
- 관측 스레드는 **전송 단계 동안만** 산다 (#89) — 전송이 끝나면 run()이
  정지시키고, 후처리(병합·remux) 진행 통지는 _remux_streamed의 공급 루프가
  같은 ProgressEvent 콜백으로 보고한다.
- 일시정지·중단은 DownloadTaskModel의 상태와 pause_event를 그대로 사용한다.
- 콜백은 작업 스레드에서 호출된다 — 어댑터는 Signal emit까지만 해야 한다.

호출 규약: 소유자(서비스·스크립트)가 DownloadTaskModel.start()로 RUNNING
전이를 마친 뒤 run()을 호출한다. run()은 완료·중단·실패까지 블로킹한다.
data·logger는 DownloadData/DownloadLogger 호환 객체를 주입받는다.
"""

import os
import re
import statistics
import threading
import time as tm
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import requests

from core.downloaders.thread_control import TARGET_CAP, TICK_SECONDS, ThreadController
from core.models.content import Content
from core.models.download_state import DownloadState
from core.models.events import (
    FailedCallback,
    FinishedCallback,
    ProgressCallback,
    ProgressEvent,
)
from core.models.plan import DownloadPlan
from core.utils.ffmpeg import FFmpegError, read_in_chunks, remux_stream


# ============ 저속 판정 (#347) ============
# 응답 직후의 몇십 KB로 판정하면 잠깐 멎었다 풀리는 연결까지 끊는다. 시간 창으로 본다 —
# 응답 시작 뒤 이 시간(초)은 판정하지 않고, 그 뒤로는 최근 이 시간의 속도로 판정한다
_SLOW_WINDOW_SECONDS = 3.0
# 회선 전체가 느려진 순간에는 모든 연결이 임계 아래로 내려간다 — 그때 전부 끊으면 받던 것을
# 버리고 다시 붙는 일만 는다. 다른 연결들보다 유독 느린 연결만 끊는다
_SLOW_PEER_RATIO = 0.3  # 다른 연결들의 중앙 속도의 이 비율보다 느려야 저속으로 본다
_SLOW_MIN_PEERS = 3  # 견줄 연결이 이보다 적으면 끊지 않는다 — 다시 받는 쪽의 속도를 알 수 없다
_PEER_FRESH_SECONDS = 2.0  # 이보다 오래 속도를 알리지 않은 연결과는 견주지 않는다
# 느린 연결이라도 끊는 것이 이득일 때만 끊는다 — 그대로 두면 남은 양을 받는 데 걸릴 시간이,
# 끊고 다시 받는 데 걸릴 시간보다 뚜렷이 길 때다. 세그먼트는 처음부터 다시 받아야 해서
# 조금 느린 정도로는 끊는 쪽이 더 늦다
_RESTART_CONNECT_SECONDS = 1.0  # 새 요청의 첫 바이트까지 걸리는 시간(초)으로 셈하는 값
_RESTART_MARGIN = (
    1.5  # 그대로 두는 쪽이 이 배수를 넘게 오래 걸려야 끊는다 — 순간 속도가 크게 흔들린다
)


# ============ 요청 타임아웃 (#320) ============
# 다운로드 경로의 모든 요청(총 크기 조회 · 플레이리스트 · 초기화 세그먼트 · 범위 ·
# 세그먼트)이 쓰는 값이다. 타임아웃 없는 요청은 서버가 응답하지 않으면 끝나지 않아
# 다운로드가 실패하지도 못하고 멈춘다. 값은 범위 · 세그먼트 요청이 써 오던 그대로다.
REQUEST_TIMEOUT = 30  # 초 — 연결과 응답 대기 각각의 한도
_MONITOR_POLL_SECONDS = 0.1  # 관측 루프가 틱을 기다리며 정지 · 상태 변화를 살피는 간격(초)

# ============ 오류 재큐 상한 (#131) ============
# 상한이 없으면 영구 오류(404·403)가 무한 재큐돼 다운로드가 끝나지도,
# 실패하지도 않는다. 상한은 예외로 인한 재큐(_requeue_failed)에만 건다 —
# 저속 재큐(_requeue_slow)는 잘 동작하는 회선에서도 정상적으로 여러 번
# 발동하는 규칙이라, 함께 세면 느린 회선의 정상 다운로드가 실패한다.
_PERMANENT_ERROR_REQUEUE_LIMIT = 2  # 항목별 최대 재큐 횟수 — 총 3회 시도
_TRANSIENT_ERROR_REQUEUE_LIMIT = 10  # 항목별 최대 재큐 횟수 — 총 11회 시도
# 4xx 중 일시적일 수 있는 상태 — 요청 타임아웃(408)·과요청(429)은 재시도가 유효하다
_TRANSIENT_HTTP_STATUSES = frozenset({408, 429})


def _is_permanent_error(exc: BaseException) -> bool:
    """재시도해도 결과가 같은 오류인지 판정한다 (#131).

    4xx 응답(408·429 제외)은 같은 요청에 같은 답이 돌아온다 — 세그먼트
    소실(404)·권한/만료(403)가 대표다. 그 외(5xx·타임아웃·연결 끊김)는
    일시적일 수 있어 재시도 여지를 더 준다.
    """
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        status = exc.response.status_code
        return 400 <= status < 500 and status not in _TRANSIENT_HTTP_STATUSES
    return False


class PostprocessError(Exception):
    """후처리(remux) 실패 (#92).

    다운로드 자체는 완결된 상태라, 이 실패로 세그먼트(임시 폴더)를 지우지
    않는다 — 수십 분짜리 재다운로드를 강요하지 않기 위함이다. 불완전한
    산출물은 후처리 단계가 이미 삭제했다. run()이 이 예외를 일반 실패와
    구분해 처리한다.
    """


class _PostprocessAborted(Exception):
    """후처리 공급 루프의 사용자 중단 신호 — 실패가 아니라 중단 경로로 보낸다."""


class _SlowWatch:
    """요청 하나의 저속 판정 창 (#347).

    응답을 받기 시작할 때 만들고(``BaseDownloader._watch_slow``), 조각을 받을 때마다
    ``is_slow``에 그때까지의 경과 시간과 받은 양을 넣는다. 경과 시간은 일시정지한 시간을 뺀
    단조 시계 값이어야 한다.
    """

    def __init__(
        self, engine: "BaseDownloader", part_num: int, expected: int | None, resumes: bool
    ):
        self._engine = engine
        self._part_num = part_num
        self._expected = expected  # 이 응답이 줄 본문의 길이(바이트). 모르면 None
        self._resumes = resumes  # 끊은 뒤 받은 데서 이어받는가(아니면 처음부터 다시 받는다)
        self._window: deque[tuple[float, int]] = deque([(0.0, 0)])  # (경과 시간, 받은 양)

    def is_slow(self, elapsed: float, received: int) -> bool:
        """이 연결을 지금 저속으로 끊을지 답한다.

        응답 시작 뒤 ``_SLOW_WINDOW_SECONDS``가 지나기 전에는 판정하지 않는다. 그 뒤로는
        최근 그 시간 동안의 속도가 임계 미만이고, 다른 연결들의 중앙 속도보다도 뚜렷이
        느리고, 끊고 다시 받는 쪽이 뚜렷이 빠를 때만 참이다(``_restart_pays``). 견줄 연결이
        모자라거나 남은 양을 모르면 그 셈을 할 수 없어 끊지 않는다.
        """
        window = self._window
        window.append((elapsed, received))
        while len(window) > 1 and window[1][0] <= elapsed - _SLOW_WINDOW_SECONDS:
            window.popleft()
        span = elapsed - window[0][0]
        if span <= 0:
            return False
        speed_kb_s = (received - window[0][1]) / span / 1024
        engine = self._engine
        now = engine._now()
        engine._publish_speed(self._part_num, speed_kb_s, now)
        if elapsed < _SLOW_WINDOW_SECONDS:
            return False
        if speed_kb_s >= engine._slow_speed_threshold_kb_s:
            return False
        peers = engine._peer_speeds(self._part_num, now)
        if len(peers) < _SLOW_MIN_PEERS:
            return False
        peer_speed_kb_s = statistics.median(peers)
        if speed_kb_s >= _SLOW_PEER_RATIO * peer_speed_kb_s:
            return False  # 다른 연결들도 느리다 — 회선 전체의 일이다
        return self._restart_pays(received, speed_kb_s, peer_speed_kb_s)

    def _restart_pays(self, received: int, speed_kb_s: float, peer_speed_kb_s: float) -> bool:
        """끊고 다시 받는 쪽이 그대로 두는 쪽보다 뚜렷이 빨리 끝나는가.

        그대로 두면 남은 양 ÷ 이 연결의 최근 속도, 끊으면 다시 받을 양 ÷ 다른 연결들의 중앙
        속도 + 새 요청의 시작 비용이 든다. 다시 받을 양은 이어받는 경로면 남은 양, 아니면
        응답 전체다.
        """
        if self._expected is None or peer_speed_kb_s <= 0:
            return False
        remaining = self._expected - received
        if remaining <= 0:
            return False
        if speed_kb_s <= 0:
            return True  # 최근 창에 한 바이트도 오지 않았다 — 그대로 두면 끝나지 않는다
        redo = remaining if self._resumes else self._expected
        keep_seconds = remaining / 1024 / speed_kb_s
        restart_seconds = redo / 1024 / peer_speed_kb_s + _RESTART_CONNECT_SECONDS
        return keep_seconds > _RESTART_MARGIN * restart_seconds


class BaseDownloader(ABC):
    """작업 목록 기반 멀티스레드 다운로드의 공통 실행 엔진."""

    # 서비스가 이 다운로더의 run()을 실행할 워커 스레드 이름 (다운로드 로그 형식 보존)
    run_thread_name: str = "DownloadThread"
    # ThreadPoolExecutor 풀 스레드 이름 접두사
    worker_pool_prefix: str = "DownloadWorker"
    # 다운로드 시작 전 base_url 해석(resolver 주입·실행)이 필요한지 — 서비스가 참조
    requires_base_url_resolution: bool = False
    # 복호화 키 리졸버 주입이 필요한지 — 서비스가 참조해 set_key_resolver를 호출한다 (#57).
    # 키 취득은 유저 쿠키가 필요해 core가 직접 할 수 없다(core→app 의존 금지)
    requires_key_resolution: bool = False
    # 후처리 시작 로그(#110)에 남길 작업 이름 — 현행 세그먼트 경로는 모두 remux다
    postprocess_kind: str = "remux"
    # run()이 실패 콜백으로 환원할 예외 타입 — 그 외 예외는 전파한다
    _failure_exceptions: tuple[type[BaseException], ...] = (Exception,)
    # 저속 재큐 판정 임계(KB/s) — 기본값 100은 구 규칙 그대로 불변.
    # 속성으로 둔 이유(#160): pause/resume 통합 테스트가 느린 CI 러너에서
    # 정상적인 저속 재큐를 유발해 간헐 실패했다(3회 중 2회). 테스트가 0으로
    # 두면 판정이 비활성화되어 러너 속도와 무관해진다. 제품 동작 무변경
    _slow_speed_threshold_kb_s: float = 100.0

    def __init__(
        self,
        data,
        logger,
        on_progress: ProgressCallback | None = None,
        on_finished: FinishedCallback | None = None,
        on_failed: FailedCallback | None = None,
        on_merge_start: Callable[[], None] | None = None,
    ):
        """엔진을 생성한다.

        Args:
            data: DownloadData 호환 공유 데이터 (진행률·엔진 변수·model 보유)
            logger: DownloadLogger 호환 로거
            on_progress: 관측 주기마다 ProgressEvent를 받는 콜백
            on_finished: 정상 완료 시 인자 없이 호출되는 콜백
            on_failed: 실패 시 예외 객체를 그대로 받는 콜백
            on_merge_start: 후처리(병합) 시작 시 인자 없이 호출되는 콜백.
                후처리가 없는 다운로더는 호출하지 않는다
        """
        self.s = data
        self.model = data.model
        self.logger = logger
        self.lock = threading.Lock()
        self.future_dict: dict = {}
        # 항목별 오류 재큐 횟수 (#131) — 상한 판정용. 저속 재큐는 세지 않는다
        self._error_requeues: dict = {}
        # 전송 종료 시 관측 스레드를 깨워 끝내는 신호 — 후처리는 관측 대상이 아니다 (#89)
        self._monitor_stop = threading.Event()
        # 전송 중 관측된 정점 동시 스레드 수 — 전송 종료 요약 로그용 (#110)
        self._peak_threads = 0
        # 목표 스레드 조정기 (#112 · #347) — 첫 관측 틱에 만든다(_adjust_threads)
        self._threads: ThreadController | None = None
        self._measured_at: float | None = None  # 속도를 마지막으로 잰 시각 (#347)
        # 슬롯 → (알린 시각, KB/s). 저속 판정이 다른 연결과 견주는 데 쓴다 (#347)
        self._conn_speeds: dict[int, tuple[float, float]] = {}
        self._on_progress: ProgressCallback = on_progress or (lambda event: None)
        self._on_finished: FinishedCallback = on_finished or (lambda: None)
        self._on_failed: FailedCallback = on_failed or (lambda exc: None)
        self._on_merge_start: Callable[[], None] = on_merge_start or (lambda: None)
        # requires_key_resolution인 다운로더에 서비스가 주입한다 (#57)
        self._key_resolver = None

    @property
    def state(self) -> DownloadState:
        """현재 다운로드 상태 (DownloadTaskModel에 위임)."""
        return self.model.state

    @property
    def adjust_count(self) -> int:
        """정체 중 붕괴 방향으로 센 틱 수(음수). 조정기가 아직 없으면 0이다."""
        return self._threads.collapse_count if self._threads is not None else 0

    def set_on_progress(self, callback: ProgressCallback) -> None:
        """진행 이벤트 콜백을 등록한다 (어댑터가 생성 후 연결하는 경우용)."""
        self._on_progress = callback

    def set_key_resolver(self, resolver) -> None:
        """복호화 키 리졸버를 등록한다 (requires_key_resolution인 다운로더용, #57).

        리졸버는 ``(content, key_uri) -> bytes``다. 쿠키 로드·인증 요청은 앱
        계층이 수행하며 core는 호출만 한다.
        """
        self._key_resolver = resolver

    # ============ 시계 · 저속 판정 도우미 (#347) ============

    def _now(self) -> float:
        """단조 시계(초). 속도 측정과 저속 판정이 쓴다 — 벽시계는 시각 보정에 흔들린다."""
        return tm.perf_counter()

    def _wait_while_paused(self) -> float:
        """일시정지 중이면 풀릴 때까지(재개 · 중단) 기다리고, 기다린 시간(초)을 돌려준다.

        받는 스레드는 이 시간을 속도 판정에서 뺀다 — 빼지 않으면 일시정지한 만큼 속도가
        낮게 나와 재개 직후 파트가 느린 속도로 끊긴다.
        """
        if self.state != DownloadState.PAUSED:
            return 0.0
        started = self._now()
        self.s._pause_event.wait()
        return self._now() - started

    def _watch_slow(
        self, part_num: int, expected: int | None = None, resumes: bool = False
    ) -> _SlowWatch:
        """요청 하나의 저속 판정 창을 만든다 — 응답을 받기 시작할 때 부른다.

        Args:
            expected: 이 응답이 줄 본문의 길이(바이트). 모르면 None — 그러면 저속으로 끊지 않는다
            resumes: 끊은 뒤 받은 데서 이어받는 경로인가. 아니면 처음부터 다시 받는 것으로 셈한다
        """
        self._conn_speeds.pop(part_num, None)
        return _SlowWatch(self, part_num, expected, resumes)

    def _publish_speed(self, part_num: int, speed_kb_s: float, now: float) -> None:
        """이 슬롯의 지금 속도를 알린다 — 다른 연결의 저속 판정이 견준다."""
        self._conn_speeds[part_num] = (now, speed_kb_s)

    def _peer_speeds(self, part_num: int, now: float) -> list[float]:
        """이 슬롯을 뺀, 방금(now 기준) 속도를 알린 연결들의 속도(KB/s)."""
        return [
            speed
            for slot, (at, speed) in list(self._conn_speeds.items())
            if slot != part_num and now - at <= _PEER_FRESH_SECONDS
        ]

    def _abandon_response(self, part_num: int, response) -> None:
        """받다 만 응답을 닫고 그 슬롯의 속도 기록을 지운다 — 저속 재시작으로 빠져나올 때 부른다.

        닫지 않으면 본문이 남은 연결이 참조가 풀릴 때까지 열려 있다.
        """
        self._conn_speeds.pop(part_num, None)
        close = getattr(response, "close", None)
        if close is not None:
            close()

    # ============ 하위 다운로더의 책임 (추상) ============

    @classmethod
    @abstractmethod
    def supports(cls, content: Content) -> bool:
        """이 다운로더가 해당 컨텐츠를 처리할 수 있는지 판정한다."""

    @abstractmethod
    def prepare(self, content: Content) -> DownloadPlan:
        """다운로드 계획(DownloadPlan)을 만든다 (타입 고유 사전 조회 포함).

        계획의 part_count가 max_threads·total_ranges·진행 배열의 기준이 되고,
        total_size·requires_postprocess도 run()이 계획에서 읽는다 (#83).
        """

    @abstractmethod
    def _download_item(self, item, part_num: int):
        """작업 1건을 다운로드한다 (저속 재시도·일시정지·중단 핸들링 포함).

        풀 스레드에서 실행된다. part_num 반환이 완료 콜백의 슬롯 해제 신호다.
        """

    @abstractmethod
    def _log_item_start(self, part_num: int, item) -> None:
        """작업 시작 로그를 남긴다 (타입별 로그 메서드·형식 유지)."""

    @abstractmethod
    def _download_start_log_args(self) -> tuple:
        """log_download_start 인자 (타입별 로그 형식 유지)."""

    @abstractmethod
    def _prepare_output(self) -> None:
        """수신 준비 — 결과 파일·임시 폴더 등 산출물 자리를 만든다."""

    @abstractmethod
    def _cleanup_partial(self) -> None:
        """실패·중단 시 부분 산출물을 정리한다."""

    # ============ 하위 다운로더가 선택적으로 오버라이드 ============

    def postprocess(self) -> None:
        """다운로드 완료 후 마무리. m3u8은 여기서 병합한다.

        계획(DownloadPlan)의 requires_postprocess가 참일 때만 run()이 호출한다.
        """

    def _list_segment_files(self, extensions: tuple[str, ...]) -> list[str]:
        """임시 폴더에서 우리가 만든 세그먼트 파일만 화이트리스트로 골라 정렬해 반환한다.

        os.listdir()은 임시 폴더의 모든 항목을 무차별로 반환한다 — macOS가
        xattr을 지원하지 않는 파일시스템(exFAT 등)에 쓸 때 만드는 AppleDouble
        사이드카(``._세그먼트명``)나 Finder의 ``.DS_Store`` 같은 잡파일이 하나만
        섞여도 이름이 '.'로 시작해 사전순 정렬에서 항상 맨 앞으로 온다 — 그게
        진짜 세그먼트보다 먼저 ffmpeg stdin에 들어가 파이프 첫 바이트를
        오염시킨다(#180 — 오너 실기에서 세그먼트 수만큼 실물 확인, 임시
        폴더가 exFAT 외장 SD 카드 위였다).

        블랙리스트(예: '.'로 시작하는 것만 제외)는 다른 잡파일 유형(동기화
        도구의 충돌 사본, 백신 격리 사본 등)에 또 뚫린다 — 우리가 직접 만든
        세그먼트 이름 패턴(숫자 + 다운로더별 확장자)에 맞는 것만 받는다.
        걸러진 항목은 조용히 넘기지 않고 경고 로그로 남긴다 — 다음에 다른
        종류의 오염이 왔을 때 또 못 보는 사태를 막기 위함이다.
        """
        pattern = re.compile(r"^\d+(?:" + "|".join(re.escape(ext) for ext in extensions) + r")$")
        entries = os.listdir(self.temp_dir)
        matched = sorted(e for e in entries if pattern.match(e))
        skipped = sorted(set(entries) - set(matched))
        if skipped:
            # 오염 항목이 세그먼트 수만큼(수천 개) 쏟아지면(#180류) 파일명을
            # 전부 한 줄에 찍는 게 로그를 수십 KB로 부풀려 읽기 어렵게 만든다
            # (#191 실기에서 AppleDouble 1011개로 실측) — 개수 + 샘플 몇 개만 남긴다
            sample_size = 10
            sample = skipped[:sample_size]
            more = f" 외 {len(skipped) - sample_size}개 더" if len(skipped) > sample_size else ""
            self.logger.warning(
                f"임시 폴더에서 세그먼트가 아닌 항목 {len(skipped)}개를 건너뜀"
                f"(병합에서 제외): {sample!r}{more}"
            )
        return matched

    def _remux_streamed(self, segment_paths: list[str]) -> None:
        """세그먼트 파일들을 순서대로 ffmpeg stdin에 흘려 산출물을 만든다 (#92).

        중간 병합 파일을 만들지 않는 단일 패스다 — fMP4·TS는 바이트 연결이
        곧 유효한 스트림이라 파이프 공급이 병합 의미를 정확히 보존한다.
        세그먼트 파일은 여기서 지우지 않는다(#92 — 후처리 실패로 재다운로드를
        강요하지 않기 위함). 성공 후 정리는 run()의 _cleanup_after_run이 한다.

        remux가 실패하면 폴백 없이 PostprocessError로 명확히 실패한다 —
        바이트 연결본은 #88이 고치려던 결함품이라 조용히 대체하지 않는다.
        일시정지·중단은 공급 루프가 세그먼트 병합과 같은 규칙으로 처리한다.

        후처리 구간의 진행 통지도 이 공급 루프가 담당한다 (#89) — 관측
        스레드는 전송 종료 시점에 정지되므로, 파이프 공급 진행(병합된
        세그먼트 수)이 곧 진행 신호다.
        """
        total_segments = len(segment_paths)
        last_percent = -1

        def feed():
            nonlocal last_percent
            for path in segment_paths:
                for chunk in read_in_chunks(path):
                    # 다운로드 중지 상태라면 중단 — 공급을 끊고 산출물을 지운다
                    if self.state == DownloadState.WAITING:
                        raise _PostprocessAborted
                    # 일시정지 상태라면 대기 (ffmpeg는 stdin을 기다리며 멈춘다)
                    if self.state == DownloadState.PAUSED:
                        self.s._pause_event.wait()
                    yield chunk
                self.s.merged_segments += 1
                # 어댑터가 표시하는 정수 %(분모 = 병합 대상 수 = 이 목록 길이)가
                # 바뀔 때만 통지한다 — 세그먼트 수천 개짜리 VOD에서도 통지가
                # 최대 ~101회로 묶인다. 속도 0·활성 0은 구 관측 스레드가 병합
                # 구간에서 내보내던 값과 동일해 UI 표시 결과가 변하지 않는다
                percent = int(self.s.merged_segments / total_segments * 100)
                if percent != last_percent:
                    last_percent = percent
                    self._on_progress(
                        ProgressEvent(
                            downloaded_size=self.s.total_downloaded_size,
                            total_size=self._progress_total_size(),
                            speed=0.0,
                            active_threads=0,
                        )
                    )

        try:
            remux_stream(feed(), self.s.output_path)
        except _PostprocessAborted:
            # 중단 정리(임시 폴더·산출물 삭제)는 run()의 중단 경로가 수행한다
            return
        except FFmpegError as e:
            self.logger.log_error("Remux failed — segments preserved for retry", e)
            raise PostprocessError(f"후처리(remux) 실패: {e}") from e

    def _initial_queue(self, items: list) -> list:
        """시작 시 작업 큐를 구성한다 (기본: 계획의 items 그대로)."""
        return list(items)

    def _cleanup_after_run(self) -> None:
        """정상 경로(비예외) 종료 후 정리 (기본 no-op). m3u8은 임시 폴더를 지운다."""

    def _progress_total_size(self) -> int | None:
        """ProgressEvent에 실을 전체 크기. 미리 알 수 없는 다운로더는 None."""
        return self.s.total_size

    # ============ 실행 파이프라인 (구 file/m3u8 run의 공통 골격) ============

    def run(self) -> None:
        """다운로드 파이프라인을 실행한다. 관측 스레드도 여기서 소유·시작한다."""
        monitor = threading.Thread(target=self._monitor_loop, name="DownloadMonitor", daemon=True)
        try:
            self.s.start_time = tm.time()
            plan = self.prepare(self.s.content)
            if plan.selections:
                # 구간 해석은 #83 범위 밖 — 모양만 정의하고 명시적으로 거부한다
                raise NotImplementedError("구간 선택 다운로드(selections)는 아직 지원하지 않는다")
            if plan.total_size is not None:
                # 총 크기는 계획에서 읽는다 — 진행 통지·파트 로그가 참조한다
                self.s.total_size = plan.total_size
            items = list(plan.items)

            self.s.max_threads = self.s.total_ranges = plan.part_count
            self.s.adjust_threads = min(self.s.adjust_threads, self.s.max_threads)
            self.s.threads_progress = [0] * self.s.total_ranges
            self.logger.log_download_start(*self._download_start_log_args())

            self._prepare_output()

            # 진행률 배열이 준비된 뒤에 관측을 시작한다
            monitor.start()

            with ThreadPoolExecutor(
                max_workers=self.s.max_threads, thread_name_prefix=self.worker_pool_prefix
            ) as executor:
                self.s.remaining_ranges = self._initial_queue(items)
                # 재사용 시 초기화 필수
                with self.lock:
                    self.s.future_count = 0
                    self.future_dict = {}
                    self._error_requeues = {}

                while not self.state == DownloadState.WAITING:
                    # (1) 현재 활성 스레드 수보다 적으면 -> 추가 스레드 할당
                    while self.s.future_count < self.s.adjust_threads and self.s.remaining_ranges:
                        for part_num in range(self.s.adjust_threads):
                            if not self.s.remaining_ranges:
                                break
                            submitted = None
                            with self.lock:
                                if part_num not in self.future_dict:
                                    item = self.s.remaining_ranges.pop(0)
                                    self.s.future_count += 1
                                    # 전송 종료 요약(#110)용 정점 동시 스레드 수
                                    self._peak_threads = max(
                                        self._peak_threads, self.s.future_count
                                    )
                                    self._log_item_start(part_num, item)
                                    submitted = executor.submit(self._download_item, item, part_num)
                                    self.future_dict[part_num] = (item, submitted)
                            if submitted is not None:
                                # 콜백 등록은 락 밖에서 한다 (#147) — 이미 끝난 future의
                                # 콜백은 등록 시점에 이 스레드에서 동기 실행되는데,
                                # 콜백의 슬롯 정리가 같은 락을 잡으므로(비재진입 락)
                                # 락 안에서 등록하면 즉시 실패하는 작업에서 교착한다
                                submitted.add_done_callback(self._download_completed_callback)

                    # (2) 주기적으로 상태 확인 (non-blocking)
                    tm.sleep(0.1)

                    # (3) 남은 작업이 없고 스레드도 없으면 종료
                    if not self.s.remaining_ranges and not self.future_dict:
                        break

            # 전송이 끝났다 — 후처리는 관측 대상이 아니므로 관측 스레드를 먼저
            # 정지한다 (#89). 병합 중 스레드 수가 절반씩 줄어드는 로그 꼬리와
            # 무의미한 speed 0.00 관측이 사라진다. 후처리 진행 통지는
            # _remux_streamed의 공급 루프가 담당한다
            self._stop_monitor(monitor)
            # 전송 구간 소요는 관측 정지까지 포함해 여기서 확정한다 (#110) —
            # 이후 구간(후처리)과 합이 전체와 어긋나지 않게 하기 위함이다
            transfer_elapsed = tm.time() - self.s.start_time

            if self.state == DownloadState.RUNNING:
                # 전송 단계 종료 요약 한 줄 (#110)
                self.logger.log_transfer_complete(
                    transfer_elapsed,
                    self.s.total_downloaded_size,
                    self.s.failed_threads + self.s.restart_threads,
                    self._peak_threads,
                )
                # (4) 다운로드 완료 후 타입별 마무리(병합 등) 후 완료 통지 —
                # 후처리 필요 여부는 실행 중 추측하지 않고 계획이 답한다 (#83)
                postprocess_elapsed = None
                if plan.requires_postprocess:
                    self.logger.log_postprocess_start(self.postprocess_kind)
                    postprocess_started = tm.time()
                    self.postprocess()
                    if self.state != DownloadState.WAITING:
                        postprocess_elapsed = tm.time() - postprocess_started
                        self.logger.log_postprocess_complete(
                            postprocess_elapsed, os.path.getsize(self.s.output_path)
                        )
                # 후처리 중 중단(stop)됐다면 완료 통지를 생략한다 (#92) —
                # WAITING → FINISHED는 허용되지 않는 전이라, 무조건 완료
                # 처리하면 전이 예외가 실패 콜백으로 둔갑한다 (구 병합 코드의
                # 잠재 결함이 중단 테스트로 드러난 것)
                if self.state != DownloadState.WAITING:
                    self.s.end_time = tm.time()
                    total_time = self.s.end_time - self.s.start_time
                    self.logger.log_download_complete(total_time)
                    # 전체 = 전송 + 후처리 구분 (#110) — 위 완료 줄(형식 불변)이
                    # 전체 시간임을 새 줄이 드러낸다
                    self.logger.log_total_breakdown(transfer_elapsed, postprocess_elapsed)
                    self.logger.save_and_close()
                    self._on_finished()

            # (5) 정상 경로 종료 후 정리 (중단으로 빠져나온 경우 포함)
            self._cleanup_after_run()

            # 사용자가 강제로 중단한 경우 부분 산출물 삭제 (#185 — try 안으로 이동).
            # 여기 있어야 하는 이유: 이 블록은 예외 없이 끝난 경로에서만 닿는다.
            # 예전엔 이 체크가 try/except/finally 전체 바깥에 있어, PostprocessError
            # 등 except 분기가 이미 각자 정리 여부를 결정한 뒤에도 다시 실행됐다.
            # 실패 콜백(_on_failed)이 비동기로(Qt 큐드 시그널 등) task.stop()을
            # 불러 상태가 WAITING으로 바뀌면, 그 바깥 체크가 "유저가 중단한
            # 경우"로 오인해 PostprocessError가 보존하려던 세그먼트를 도로
            # 지웠다(#92 정책 위반, #185 실측 확인) — 실패로 인한 stop()과 유저
            # 중단을 같은 신호(WAITING)로 뭉뚱그려 구분하지 못한 게 뿌리였다
            # (#135와 같은 자리: 엔진 종료 신호와 실패 처리를 분리해야 한다).
            if self.state == DownloadState.WAITING:
                self._cleanup_partial()

        except PostprocessError as e:
            # 후처리 실패 (#92) — 다운로드는 완결됐으므로 세그먼트(임시 폴더)를
            # 보존한다. 불완전한 산출물은 후처리 단계가 이미 삭제했다.
            # _cleanup_partial()을 부르지 않는 것이 이 분기의 존재 이유다
            self._on_failed(e)
            self.logger.log_exception("Postprocess failed", e)
            self.logger.save_and_close()

        except self._failure_exceptions as e:
            # 오류 발생 시 부분 산출물 삭제
            self._cleanup_partial()
            self._on_failed(e)
            self.logger.log_exception("Download failed", e)
            self.logger.save_and_close()

        finally:
            # 실패·전파 예외 경로에서도 관측 스레드를 남기지 않는다 (#89).
            # 실패 후 상태가 RUNNING인 채 남는 경로(헤드리스 등)에서는 기존
            # 관측 루프가 상태 변화만 기다리며 영원히 돌았다
            self._stop_monitor(monitor)

    # ============ 다운로드 조정 및 콜백 메서드 ============

    def _download_completed_callback(self, future):
        """future 종료 콜백 — 성공·실패 모든 경로에서 슬롯을 반드시 정리한다 (#147 E2).

        정리를 finally로 보장하는 이유: 실패 경로에서 future_dict 항목이
        남으면 실행 루프의 종료 조건(잔여 작업·활성 future 없음)이 영원히
        성립하지 않아, 실패를 통지하고도 루프가 무한 회전했다(#146 감사에서
        워커 OSError 주입으로 실측). 실패 시 part_num을 얻을 수 없으므로
        future 동일성으로 등록 항목을 찾는다.
        """
        part_num = None
        try:
            part_num = future.result()
        except Exception as e:
            # 여기까지 오는 예외는 워커가 잡지 않은 것 — 네트워크 오류는
            # 워커가 이미 잡아 재큐 규칙(#131)을 태우므로, 남는 것은 출력
            # 파일 OSError(디스크 부족·마운트 해제·잠금) 등 파일시스템류다.
            # 재시도해도 같은 결과라 재큐 상한(#131)에 태우지 않고 즉시
            # 전체 실패로 보낸다 — 상한(10회)까지 도는 것은 유저가 보는
            # 멈춤 시간일 뿐이다.
            self._fail_fatally(e, "Thread failed — failing download")
        finally:
            with self.lock:
                for pn, (_item, fut) in list(self.future_dict.items()):
                    if fut is future:
                        del self.future_dict[pn]
                        self.s.future_count -= 1
                        break
        if part_num is not None:
            self.update_progress()  # 즉각적 진행도 반영

    def _requeue_failed(self, item, part_num: int, exc: BaseException) -> None:
        """예외 발생 시 작업을 다시 다운로드할 수 있도록 remaining_ranges에 등록.

        항목별 재큐 횟수에 상한을 둔다 (#131) — 영구 오류(4xx)는 짧게,
        일시 오류(5xx·타임아웃·연결 끊김)는 길게 허용한다. 상한 도달 시
        재큐 대신 원인을 로그에 남기고 다운로드 전체를 실패로 끝낸다.
        저속 재큐(_requeue_slow)는 이 상한에 세지 않는다.
        """
        count = self._error_requeues.get(item, 0)
        permanent = _is_permanent_error(exc)
        limit = _PERMANENT_ERROR_REQUEUE_LIMIT if permanent else _TRANSIENT_ERROR_REQUEUE_LIMIT
        if count >= limit:
            kind = "permanent" if permanent else "transient"
            self._fail_fatally(
                exc,
                f"Part {part_num} requeue limit reached "
                f"({count} requeues, {kind} error) — giving up",
            )
            return
        self._error_requeues[item] = count + 1
        self.s.failed_threads += 1
        self.s.threads_progress[part_num] = 0
        self.s.remaining_ranges.append(item)

    def _fail_fatally(self, exc: BaseException, log_message: str) -> None:
        """워커에서 회복 불가능한 오류가 났을 때 다운로드 전체를 중단시킨다.

        상태를 중단으로 돌려 실행 루프·관측 루프가 빠져나오게 하고, 실패를
        통지한다. 부분 산출물 정리는 run()의 중단 경로가 수행한다.
        (hls_aes 복호화 실패 경로에서 승격 — #131부터 재큐 상한 도달도 쓴다)
        """
        self.logger.log_error(log_message, exc)
        self._on_failed(exc)
        self.model.stop()

    def _requeue_slow(self, item, part_num: int, diagnostic: str = "") -> None:
        """저속으로 중도 중단한 작업을 재시작하도록 등록.

        diagnostic — 임시 진단용(#191 실기 확인). write_elapsed/total_elapsed
        비율을 문자열로 받아 경고 로그에 덧붙인다. 디스크 쓰기 시간을 뺀
        수정(#191)이 실기에서 재큐를 얼마나 줄이는지 애매했던 재현 결과
        (70→62회, 11% 감소)의 원인이 "write()가 OS 페이지 캐시에 즉시
        반환돼 write_elapsed가 애초에 작다"인지, 다른 요인인지 실측으로
        가리기 위함 — 최종 커밋에 남길지는 실기 결과를 보고 판단한다.
        """
        self.s.restart_threads += 1
        self.s.threads_progress[part_num] = 0
        self.s.remaining_ranges.append(item)
        suffix = f" ({diagnostic})" if diagnostic else ""
        self.logger.warning(f"Part {part_num} stopped due to slow speed, will retry{suffix}")

    def _check_speed_and_update_progress(
        self, part_num: int, downloaded_size: int, total_size: int, speed_kb_s: float
    ):
        """
        스레드가 다운로드 중일 때 속도 체크 및 진행 상황 업데이트.
        """
        with self.lock:
            self.s.threads_progress[part_num] = downloaded_size
            self.update_progress()

    # ============ 관측 루프 (구 Monitor 스레드 — 엔진이 흡수) ============

    def _stop_monitor(self, monitor: threading.Thread) -> None:
        """관측 스레드를 정지시키고 종료를 기다린다 (#89). 여러 번 불려도 무해하다.

        전송 종료 직후 run()이 호출한다 — 후처리(병합·remux)가 관측 스레드
        정지 이후에 시작됨을 보장한다. join 타임아웃은 일시정지 대기
        (_pause_event.wait)에 막혀 있는 극단 경로 대비다(데몬 스레드라
        프로세스를 붙잡지는 않는다).
        """
        self._monitor_stop.set()
        if monitor.is_alive():
            monitor.join(timeout=2)

    def _monitor_loop(self):
        """주기(1초)마다 속도 측정·스레드 수 조정·진행 통지를 수행하는 관측 루프.

        전송 단계 동안만 산다 — 전송이 끝나면 run()이 _monitor_stop으로
        정지시킨다. 후처리(병합·remux)는 관측 대상이 아니다 (#89).

        틱의 시각은 단조 시계의 마감(직전 마감 + 1초)으로 잡는다 — 틱마다 한 일의 시간만큼
        주기가 늘어지지 않는다 (#347). 속도를 먼저 재고 그 값으로 조정한다.
        """
        self._measured_at = self._now()
        self._monitor_stop.wait(TICK_SECONDS)
        deadline = self._now()
        while self._monitoring():
            if not self.s._pause_event.is_set():
                paused_at = self._now()
                self.s._pause_event.wait()
                self.measure_speed(since_pause=True)
                # 재개 직후 궤적 앵커 (#78) — 관측 로그가 스레드 수 변화 시에만
                # 남으면 재개 후 재상승 여부를 로그로 확인할 수 없다. 기존
                # 조정 로그와 같은 형식으로 현재 목표·속도를 한 줄 남긴다.
                # 일시정지 구간이 섞인 이 측정으로는 조정 판단을 하지 않는다
                self.logger.log_thread_adjust(self.s.adjust_threads, self.s.speed_mb)
                now = self._now()
                if self._threads is not None:
                    self._threads.shift(now - paused_at)
                    self._threads.skip(now, TICK_SECONDS)
            else:
                self.measure_speed()
                self._adjust_threads()
                self.emit_progress()
            deadline += TICK_SECONDS
            if deadline < self._now():
                # 틱 하나가 주기보다 오래 걸렸다 — 몰아서 돌지 않고 지금부터 한 주기를 다시 센다
                deadline = self._now() + TICK_SECONDS
            while self._monitoring():
                remaining = deadline - self._now()
                if remaining <= 0:
                    break
                self._monitor_stop.wait(min(_MONITOR_POLL_SECONDS, remaining))

    def _monitoring(self) -> bool:
        """관측을 이어 갈 상태인가 — 정지 신호가 없고 전송 중(일시정지 포함)이다."""
        return not self._monitor_stop.is_set() and self.state in [
            DownloadState.RUNNING,
            DownloadState.PAUSED,
        ]

    def _adjust_threads(self):
        """직전 틱의 총 처리량(speed_mb)으로 목표 스레드 수를 조정한다 (#112 · #347).

        판단은 ThreadController가 한다 — 규칙과 근거는 그 모듈에 있다. 여기서는 틱마다
        시각과 속도를 넘기고, 목표가 바뀌면 공유 데이터와 로그에 옮긴다.

        판단 신호가 스레드당 속도가 아니라 총 처리량인 이유 (#112 실측):
        - 치지직은 연결당 처리량을 제한한다(144p 연결당 ~0.95 MB/s). 총량은
          스레드 수에 선형(4→3.85, 8→7.64, 16→15.06, 32→29.57 MB/s)이라
          "스레드당 속도가 낮으면 줄인다"는 총 처리량만 떨어뜨린다 — 방향이
          반대다
        - 평균의 분모가 관측 순간의 활성 수(future_count)였다. 세그먼트가
          잘게 쪼개진 저해상도에서는 빈 슬롯 순간(활성 0)이 '저속'으로
          오판돼 실제 4.7 MB/s로 받는 중에도 4→2→1로 붕괴했다(로그 실측).
          총 처리량은 활성 수와 무관해 이 오판이 구조적으로 사라진다
        """
        total_speed = self.s.speed_mb
        if self._threads is None:
            cap = min(self.s.max_threads, TARGET_CAP)
            self._threads = ThreadController(self.s.adjust_threads, cap)
        before = self._threads.target
        self._threads.step(self._now(), total_speed)
        if self._threads.target != before:
            self.s.adjust_threads = self._threads.target
            self.logger.log_thread_adjust(self.s.adjust_threads, total_speed)

    def measure_speed(self, since_pause: bool = False):
        """직전 측정 뒤로 받은 바이트를 그 사이의 실제 시간으로 나눠 속도(MB/s)를 계산한다.

        관측 틱은 0.1초 대기 열 번이라 1초보다 길다 — 받은 양을 그대로 MB/s로 읽으면 실제보다
        크게 나오고, 틱 길이의 흔들림이 그대로 속도의 흔들림이 된다 (#347). 직전 측정 시각이
        없으면(관측 루프 밖에서 처음 부른 경우) 나누지 않는다.

        Args:
            since_pause: 재개 직후의 측정이다 — 직전 측정 뒤의 시간에 일시정지가 섞여 있어
                나누지 않는다. 이 측정으로는 조정 판단을 하지 않는다
        """
        current_size = self.s.total_downloaded_size
        speed = current_size - self.s.prev_size
        self.s.prev_size = current_size
        now = self._now()
        if not since_pause and self._measured_at is not None and now > self._measured_at:
            speed = speed / (now - self._measured_at)
        self._measured_at = now

        with self.lock:
            future_count = self.s.future_count
        # MB/s로 변환
        self.s.speed_mb = speed / (1024 * 1024)
        avg_speed = self.s.speed_mb / future_count if future_count > 0 else 0
        self.logger.log_thread_debug(future_count, self.s.speed_mb, avg_speed)

    # ============ 진행 상황 업데이트 ============

    def update_progress(self):
        """
        다운로드된 총량을 저장한다.
        """
        if self.state in [DownloadState.PAUSED, DownloadState.WAITING]:  # 중단 플래그 확인
            return

        active_downloaded_size = sum(self.s.threads_progress)
        self.s.total_downloaded_size = self.s.completed_progress + active_downloaded_size

    def emit_progress(self):
        """진행 상태를 집계해 ProgressEvent 콜백으로 통지한다 (구 Monitor.update_progress)."""
        active_downloaded_size = sum(self.s.threads_progress)
        self.s.total_downloaded_size = self.s.completed_progress + active_downloaded_size

        with self.lock:
            future_count = self.s.future_count

        self._on_progress(
            ProgressEvent(
                downloaded_size=self.s.total_downloaded_size,
                total_size=self._progress_total_size(),
                speed=self.s.speed_mb,
                active_threads=future_count,
            )
        )
