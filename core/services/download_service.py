"""다운로드 오케스트레이션 서비스 (#75, SPEC §5 DownloadService).

download/manager.py(DownloadManager)에 있던 오케스트레이션 —
Content 타입에 맞는 다운로더 선택, 실행, 큐잉·동시 실행 수 제한, 완료 처리 —
을 Qt 없이 수행한다. GUI 없이(CLI·헤드리스·테스트) 재사용할 수 있다.

- 통지는 core/models/events.py의 콜백 계약만 사용한다(진행/완료/실패).
  콜백은 다운로드 워커 스레드에서 호출된다 — Qt 어댑터는 콜백 안에서
  Signal emit까지만 수행해야 한다(스레드 규칙은 events.py docstring이 정본).
- 동시 실행 수 제한: 기본 1건 — 현행 GUI의 순차 다운로드 동작과 동일하다.
  초과 제출은 큐에 대기하고, 실행 중인 건이 끝나면 순서대로 시작된다.
- 상태 전이: 시작(WAITING→RUNNING)·완료(RUNNING→FINISHED)는 서비스가 수행한다.
  실패·중단 시의 상태 정리는 기존 흐름(앱 어댑터의 stop)을 보존하기 위해
  서비스가 강제하지 않는다 — 실패는 콜백 통지까지만 한다.

base_url_resolver 매개변수: m3u8 컨텐츠의 플레이리스트 URL 해석(쿠키 로드·
치지직 API 조회)은 네트워크 계층이 아직 앱 영역이라 core에서 직접 수행할 수
없다(core→app 의존 금지). 호출자가 Content를 받아 URL을 반환하는 콜러블을
주입한다 — #72의 MetadataApi 주입과 같은 방식이다. 해석은 다운로드 시작
시점(워커 스레드)에 수행된다(쿠키 변경 반영 — 구 DownloadM3U8Thread와 동일).

logger 매개변수: DownloadLogger 호환 객체를 건별로 주입받는다(파일 로그 경로가
앱 설정에 묶여 있어 core가 직접 만들지 않는다). 생략하면 무동작 로거를 쓴다.
"""

import logging
import threading
from collections import deque
from collections.abc import Callable

from core.downloaders.file_downloader import FileDownloader
from core.downloaders.m3u8_downloader import M3U8Downloader
from core.models.content import Content, ContentType
from core.models.download_data import DownloadData
from core.models.download_state import DownloadState
from core.models.download_task import InvalidStateTransitionError
from core.models.events import FailedCallback, FinishedCallback, ProgressCallback

logger = logging.getLogger(__name__)

# Content를 받아 m3u8 플레이리스트 URL을 반환한다. 실패는 예외로 던진다.
BaseUrlResolver = Callable[[Content], str]

# 서비스가 다운로드를 수행할 수 있는 컨텐츠 타입 (라이브 녹화는 Phase 5 소관)
_SUPPORTED_TYPES = frozenset(
    {ContentType.CHZZK_VIDEO, ContentType.CHZZK_VIDEO_M3U8, ContentType.CHZZK_CLIP}
)


class _NullDownloadLogger:
    """logger 미주입 시 사용하는 무동작 로거 (DownloadLogger 호환).

    엔진이 호출하는 모든 log_*·save_and_close 메서드를 no-op으로 받는다.
    """

    def __getattr__(self, name: str):
        return lambda *args, **kwargs: None


class DownloadHandle:
    """제출된 다운로드 한 건의 제어·조회 핸들.

    콜백(on_progress/on_finished/on_failed/on_merge_start)은 속성으로 보관하며
    호출 시점에 읽는다 — 제출 후 교체도 반영된다. 콜백은 워커 스레드에서
    호출된다(스레드 규칙은 core/models/events.py 참고).

    상태 전이 메서드(pause/resume/stop)는 core 규칙대로 엄격하다 — 허용되지
    않는 전이는 InvalidStateTransitionError를 던진다. UI 타이밍 레이스 흡수는
    앱 계층 어댑터(download/task.py의 DownloadTask)의 몫이다.
    """

    def __init__(
        self,
        content: Content,
        data: DownloadData,
        task_logger,
        on_progress: ProgressCallback | None = None,
        on_finished: FinishedCallback | None = None,
        on_failed: FailedCallback | None = None,
        on_merge_start: Callable[[], None] | None = None,
    ):
        self.content = content
        self.data = data
        self.logger = task_logger
        self.engine = None
        self.thread: threading.Thread | None = None
        self.on_progress = on_progress
        self.on_finished = on_finished
        self.on_failed = on_failed
        self.on_merge_start = on_merge_start
        self._cancelled = False
        self._done = threading.Event()

    @property
    def state(self) -> DownloadState:
        """현재 다운로드 상태 (DownloadTaskModel에 위임)."""
        return self.data.model.state

    def pause(self) -> bool:
        """다운로드 일시정지 (RUNNING→PAUSED)."""
        return self.data.model.pause()

    def resume(self) -> bool:
        """다운로드 재개 (PAUSED→RUNNING)."""
        return self.data.model.resume()

    def stop(self) -> bool:
        """다운로드 취소. 아직 시작 전(큐 대기)이면 실행 자체를 건너뛰게 한다."""
        self._cancelled = True
        return self.data.model.stop()

    def wait(self, timeout: float | None = None) -> bool:
        """다운로드 처리(완료·실패·중단·스킵)가 끝날 때까지 대기한다.

        Returns:
            bool: 제한 시간 안에 끝났으면 True
        """
        return self._done.wait(timeout)

    def elapsed_seconds(self) -> float:
        """다운로드 소요 시간(초). 시작·종료 시각은 엔진이 기록한다."""
        return self.data.end_time - self.data.start_time

    # ============ 엔진 콜백 중계 (워커 스레드에서 호출) ============

    def _relay_progress(self, event) -> None:
        """엔진 진행 콜백 → 핸들에 등록된 진행 콜백 (미등록 시 무시)."""
        cb = self.on_progress
        if cb is not None:
            cb(event)

    def _relay_finished(self) -> None:
        """완료 통지 → 핸들에 등록된 완료 콜백 (미등록 시 무시)."""
        cb = self.on_finished
        if cb is not None:
            cb()

    def _relay_failed(self, exc: BaseException) -> None:
        """엔진 실패 콜백 → 핸들에 등록된 실패 콜백. 예외 객체를 그대로 전달한다."""
        cb = self.on_failed
        if cb is not None:
            cb(exc)

    def _relay_merge_start(self) -> None:
        """엔진 병합 시작 콜백 → 핸들에 등록된 병합 시작 콜백 (m3u8 전용)."""
        cb = self.on_merge_start
        if cb is not None:
            cb()


class DownloadService:
    """Content를 받아 다운로더를 선택·실행하는 오케스트레이션 서비스."""

    # 현행 GUI와 동일하게 한 번에 한 건씩 내려받는다 (#75 — 기본값 유지)
    DEFAULT_MAX_CONCURRENT = 1

    def __init__(
        self,
        *,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        base_url_resolver: BaseUrlResolver | None = None,
    ):
        """서비스를 생성한다.

        Args:
            max_concurrent: 동시 실행 수 상한 (1 이상)
            base_url_resolver: m3u8 플레이리스트 URL 해석 콜러블 (m3u8 다운로드 시 필수)
        """
        if max_concurrent < 1:
            raise ValueError("max_concurrent는 1 이상이어야 한다")
        self._max_concurrent = max_concurrent
        self._base_url_resolver = base_url_resolver
        self._lock = threading.Lock()
        self._pending: deque[DownloadHandle] = deque()
        self._active: set[DownloadHandle] = set()

    def submit(
        self,
        content: Content,
        *,
        data: DownloadData | None = None,
        task_logger=None,
        on_state_change: Callable[[DownloadState], None] | None = None,
        on_progress: ProgressCallback | None = None,
        on_finished: FinishedCallback | None = None,
        on_failed: FailedCallback | None = None,
        on_merge_start: Callable[[], None] | None = None,
    ) -> DownloadHandle:
        """다운로드를 제출한다. 빈 슬롯이 있으면 즉시, 없으면 큐 대기 후 시작된다.

        Args:
            content: 다운로드할 컨텐츠 (output_path·resolution이 채워져 있어야 한다)
            data: 엔진 공유 데이터. 생략하면 content로부터 만든다. 어댑터가 진행
                변환 등에 같은 객체를 참조해야 할 때 직접 만들어 주입한다.
            task_logger: DownloadLogger 호환 로거. 생략하면 무동작 로거를 쓴다.
            on_state_change: 상태 전이를 받을 콜백 (예: UI 카드 상태 반영)
            on_progress: 진행 이벤트 콜백 (ProgressEvent)
            on_finished: 완료 콜백 (인자 없음)
            on_failed: 실패 콜백 (예외 객체 그대로)
            on_merge_start: m3u8 병합 시작 콜백

        Raises:
            ValueError: 지원하지 않는 컨텐츠 타입이거나 data가 content와 불일치
        """
        if content.content_type not in _SUPPORTED_TYPES:
            raise ValueError(f"지원하지 않는 컨텐츠 타입: {content.content_type}")
        if data is None:
            data = DownloadData.from_content(content)
        elif data.content is not content:
            raise ValueError("data.content가 제출한 content와 같은 객체여야 한다")
        if task_logger is None:
            task_logger = _NullDownloadLogger()
        if on_state_change is not None:
            data.model.set_on_state_change(on_state_change)

        handle = DownloadHandle(
            content,
            data,
            task_logger,
            on_progress=on_progress,
            on_finished=on_finished,
            on_failed=on_failed,
            on_merge_start=on_merge_start,
        )
        with self._lock:
            self._pending.append(handle)
            self._dispatch_locked()
        return handle

    @property
    def active_count(self) -> int:
        """현재 실행 중인 다운로드 수."""
        with self._lock:
            return len(self._active)

    @property
    def pending_count(self) -> int:
        """큐에서 대기 중인 다운로드 수."""
        with self._lock:
            return len(self._pending)

    # ============ 내부: 큐 디스패치와 워커 실행 ============

    def _dispatch_locked(self) -> None:
        """빈 슬롯만큼 대기 큐에서 꺼내 워커 스레드를 시작한다. self._lock 보유 상태에서 호출."""
        while self._pending and len(self._active) < self._max_concurrent:
            handle = self._pending.popleft()
            self._active.add(handle)
            # 스레드 이름은 구 QThread 어댑터와 동일하게 유지한다 (다운로드 로그 형식 보존)
            name = (
                "DownloadM3U8Thread"
                if handle.content.content_type is ContentType.CHZZK_VIDEO_M3U8
                else "DownloadThread"
            )
            handle.thread = threading.Thread(
                target=self._run_handle, args=(handle,), name=name, daemon=True
            )
            handle.thread.start()

    def _run_handle(self, handle: DownloadHandle) -> None:
        """워커 스레드 본체 — 상태 전이 후 엔진을 실행하고, 끝나면 다음 건을 디스패치한다."""
        try:
            if handle._cancelled:
                return
            state = handle.data.model.state
            if state is DownloadState.WAITING:
                try:
                    handle.data.model.start()
                except InvalidStateTransitionError as e:
                    # 시작 직전 취소 등과의 레이스 — 크래시 대신 실행 스킵
                    logger.warning("다운로드 시작 전이 무시: %s", e)
                    return
            elif state not in (DownloadState.RUNNING, DownloadState.PAUSED):
                # 호출자가 미리 RUNNING 전이를 마친 경우(또는 그 직후 일시정지)는
                # 그대로 실행하고, 이미 종결된 태스크는 실행하지 않는다
                logger.warning("이미 종결된 태스크는 실행하지 않는다: %s", state)
                return

            engine = self._create_engine(handle)
            handle.engine = engine

            if handle.content.content_type is ContentType.CHZZK_VIDEO_M3U8:
                # m3u8은 시작 시점에 플레이리스트 URL을 해석한다 (구 어댑터와 동일).
                # 실패는 엔진 실패와 같은 형식으로 콜백 통지·로깅한다
                try:
                    handle.data.base_url = self._resolve_base_url(handle.content)
                except Exception as e:
                    handle._relay_failed(e)
                    handle.logger.log_exception("Download failed", e)
                    handle.logger.save_and_close()
                    return

            engine.run()
        finally:
            handle._done.set()
            with self._lock:
                self._active.discard(handle)
                self._dispatch_locked()

    def _create_engine(self, handle: DownloadHandle):
        """컨텐츠 타입에 맞는 다운로더 엔진을 만들고 콜백을 배선한다."""
        if handle.content.content_type is ContentType.CHZZK_VIDEO_M3U8:
            return M3U8Downloader(
                data=handle.data,
                logger=handle.logger,
                on_progress=handle._relay_progress,
                on_finished=lambda: self._handle_finished(handle),
                on_failed=handle._relay_failed,
                on_merge_start=handle._relay_merge_start,
            )
        return FileDownloader(
            data=handle.data,
            logger=handle.logger,
            on_progress=handle._relay_progress,
            on_finished=lambda: self._handle_finished(handle),
            on_failed=handle._relay_failed,
        )

    def _resolve_base_url(self, content: Content) -> str:
        """주입된 resolver로 m3u8 플레이리스트 URL을 해석한다."""
        if self._base_url_resolver is None:
            raise RuntimeError("m3u8 다운로드에는 base_url_resolver 주입이 필요하다")
        return self._base_url_resolver(content)

    def _handle_finished(self, handle: DownloadHandle) -> None:
        """엔진 완료 콜백 — 완료 전이·최종 진행 통지 후 완료 콜백을 중계한다.

        순서는 구 DownloadManager.finish와 동일하다:
        finish 전이 → 최종 진행 통지(emit_progress) → 완료 통지.
        중단과의 레이스로 전이가 어긋나면 크래시 대신 경고 로그로 흡수한다.
        """
        try:
            handle.data.model.finish()
        except InvalidStateTransitionError as e:
            logger.warning("다운로드 완료 전이 무시: %s", e)
        else:
            if handle.engine is not None:
                handle.engine.emit_progress()
        handle._relay_finished()
