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
- ``_get_standard_speed()``: 스레드 스케일링 기준 속도 (기본 4 MB/s — 구 파일
  엔진의 고정 임계 4/2와 동일. m3u8은 해상도별 테이블로 오버라이드)
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
import threading
import time as tm
from abc import ABC, abstractmethod
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

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


class PostprocessError(Exception):
    """후처리(remux) 실패 (#92).

    다운로드 자체는 완결된 상태라, 이 실패로 세그먼트(임시 폴더)를 지우지
    않는다 — 수십 분짜리 재다운로드를 강요하지 않기 위함이다. 불완전한
    산출물은 후처리 단계가 이미 삭제했다. run()이 이 예외를 일반 실패와
    구분해 처리한다.
    """


class _PostprocessAborted(Exception):
    """후처리 공급 루프의 사용자 중단 신호 — 실패가 아니라 중단 경로로 보낸다."""


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
        self.adjust_count = 0
        # 전송 종료 시 관측 스레드를 깨워 끝내는 신호 — 후처리는 관측 대상이 아니다 (#89)
        self._monitor_stop = threading.Event()
        # 전송 중 관측된 정점 동시 스레드 수 — 전송 종료 요약 로그용 (#110)
        self._peak_threads = 0
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

    def set_on_progress(self, callback: ProgressCallback) -> None:
        """진행 이벤트 콜백을 등록한다 (어댑터가 생성 후 연결하는 경우용)."""
        self._on_progress = callback

    def set_key_resolver(self, resolver) -> None:
        """복호화 키 리졸버를 등록한다 (requires_key_resolution인 다운로더용, #57).

        리졸버는 ``(content, key_uri) -> bytes``다. 쿠키 로드·인증 요청은 앱
        계층이 수행하며 core는 호출만 한다.
        """
        self._key_resolver = resolver

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

    def _get_standard_speed(self) -> float:
        """스레드 스케일링 기준 속도(MB/s). 기본 4 — 구 파일 엔진의 고정 임계와 동일."""
        return 4.0

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

                while not self.state == DownloadState.WAITING:
                    # (1) 현재 활성 스레드 수보다 적으면 -> 추가 스레드 할당
                    while self.s.future_count < self.s.adjust_threads and self.s.remaining_ranges:
                        for part_num in range(self.s.adjust_threads):
                            if not self.s.remaining_ranges:
                                break
                            with self.lock:
                                if part_num not in self.future_dict:
                                    item = self.s.remaining_ranges.pop(0)
                                    self.s.future_count += 1
                                    # 전송 종료 요약(#110)용 정점 동시 스레드 수
                                    self._peak_threads = max(
                                        self._peak_threads, self.s.future_count
                                    )
                                    self._log_item_start(part_num, item)
                                    future = executor.submit(self._download_item, item, part_num)
                                    future.add_done_callback(self._download_completed_callback)
                                    self.future_dict[part_num] = (item, future)

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

        # 사용자가 강제로 중단한 경우 부분 산출물 삭제
        if self.state == DownloadState.WAITING:
            self._cleanup_partial()

    # ============ 다운로드 조정 및 콜백 메서드 ============

    def _download_completed_callback(self, future):
        """
        특정 future(스레드)가 끝났을 때 호출되는 콜백.
        """
        try:
            part_num = future.result()
            # part_num 식별 후 future_dict에서 제거
            with self.lock:
                if part_num in self.future_dict:
                    del self.future_dict[part_num]
                    self.s.future_count -= 1
            self.update_progress()  # 즉각적 진행도 반영

        except Exception as e:
            # 일부 스레드가 오류로 중단된 경우
            self._on_failed(e)
            self.logger.log_error("Thread failed", e)

    def _requeue_failed(self, item, part_num: int) -> None:
        """예외 발생 시 작업을 다시 다운로드할 수 있도록 remaining_ranges에 등록."""
        self.s.failed_threads += 1
        self.s.threads_progress[part_num] = 0
        self.s.remaining_ranges.append(item)

    def _requeue_slow(self, item, part_num: int) -> None:
        """저속으로 중도 중단한 작업을 재시작하도록 등록."""
        self.s.restart_threads += 1
        self.s.threads_progress[part_num] = 0
        self.s.remaining_ranges.append(item)
        self.logger.warning(f"Part {part_num} stopped due to slow speed, will retry")

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
        """주기(1초)마다 스레드 수 조정·속도 측정·진행 통지를 수행하는 관측 루프.

        전송 단계 동안만 산다 — 전송이 끝나면 run()이 _monitor_stop으로
        정지시킨다. 후처리(병합·remux)는 관측 대상이 아니다 (#89).
        """
        self._monitor_stop.wait(1)
        while not self._monitor_stop.is_set() and self.state in [
            DownloadState.RUNNING,
            DownloadState.PAUSED,
        ]:
            if not self.s._pause_event.is_set():
                self.s._pause_event.wait()
                self.measure_speed()
            else:
                self._adjust_threads()
                self.measure_speed()
                self.emit_progress()
            total_sleep = 1.0  # 총 1초 대기
            interval = 0.1  # 0.1초씩 대기
            elapsed = 0.0
            while (
                elapsed < total_sleep
                and not self._monitor_stop.is_set()
                and self.state in [DownloadState.RUNNING, DownloadState.PAUSED]
            ):
                self._monitor_stop.wait(interval)
                elapsed += interval

    def _adjust_threads(self):
        """
        다운로드 진행 중, 속도 등에 따라 스레드 수를 동적으로 조정한다.

        기준 속도(_get_standard_speed) 초과 틱이 쌓이면 +4, 기준/2 미만 틱이
        쌓이면 절반으로. 중간 대역은 카운터를 0으로 감쇠한다 (히스테리시스).
        """
        with self.lock:
            future_count = self.s.future_count

        avg_active_speed = self.s.speed_mb / future_count if future_count > 0 else 0

        standard_speed = self._get_standard_speed()

        if avg_active_speed > standard_speed:
            self.adjust_count += 1
        elif avg_active_speed < standard_speed / 2:
            self.adjust_count -= 1
        else:
            if self.adjust_count > 0:
                self.adjust_count -= 1
            elif self.adjust_count < 0:
                self.adjust_count += 1

        if self.adjust_count > 1:
            self.s.adjust_threads = min(self.s.max_threads, self.s.adjust_threads + 4)
            self.logger.log_thread_adjust(
                self.s.adjust_threads, self.s.speed_mb
            )  # 스레드 조정 로그
            self.adjust_count = 0
        elif self.adjust_count < -4:
            self.s.adjust_threads = max(1, self.s.adjust_threads // 2)
            self.logger.log_thread_adjust(
                self.s.adjust_threads, self.s.speed_mb
            )  # 스레드 조정 로그
            self.adjust_count = 0

    def measure_speed(self):
        """직전 틱 대비 다운로드 바이트 증가량으로 속도(MB/s)를 계산한다."""
        current_size = self.s.total_downloaded_size
        speed = current_size - self.s.prev_size
        self.s.prev_size = current_size

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
