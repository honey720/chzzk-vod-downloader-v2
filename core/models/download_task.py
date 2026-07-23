"""다운로드 태스크 도메인 모델 — 상태·진행률 보유와 전이 (#60, SPEC §4.2).

Qt 등 UI 프레임워크에 의존하지 않는 순수 상태 머신이다.
상태 전이는 반드시 start/pause/resume/stop/finish/fail 메서드를 통해서만 일어나고
락으로 동기화된다. UI 계층에는 on_state_change 콜백으로만 변경을 알린다
(app → core 단방향 의존 유지).

기존 download/task.py의 DownloadTask에서 순수 부분만 분리했다. Qt 계층 연결
(ContentItem 상태 반영, 다운로드 로깅)은 기존 위치의 어댑터가 담당한다.
"""

import threading
import time
from collections.abc import Callable

from core.models.download_state import DownloadState


class InvalidStateTransitionError(RuntimeError):
    """허용되지 않는 상태 전이를 시도했을 때 발생한다."""


# 전이 메서드별 허용 출발 상태. 현재 상태가 목표 상태와 같으면 전이 전에
# 멱등 no-op으로 처리되므로, 여기에는 "실제로 상태가 바뀌는" 출발 상태만 적는다.
_ALLOWED_SOURCES: dict[str, frozenset[DownloadState]] = {
    "start": frozenset({DownloadState.WAITING}),
    "pause": frozenset({DownloadState.RUNNING}),
    "resume": frozenset({DownloadState.PAUSED}),
    "stop": frozenset({DownloadState.RUNNING, DownloadState.PAUSED}),
    "finish": frozenset({DownloadState.RUNNING, DownloadState.PAUSED}),
    "fail": frozenset({DownloadState.WAITING, DownloadState.RUNNING, DownloadState.PAUSED}),
}


class DownloadTaskModel:
    """다운로드 한 건의 상태·진행률을 보유하고 전이 규칙을 강제하는 모델.

    - 상태는 `state` 프로퍼티로만 읽고, 전이 메서드로만 바꾼다.
    - `pause_event`는 다운로드 스레드가 일시정지를 대기(wait)하는 Event다.
      set 상태가 "진행 가능"을 뜻한다. 기존 엔진(DownloadData._pause_event)과
      같은 Event를 공유할 수 있도록 외부 주입을 허용한다.
    """

    def __init__(
        self,
        pause_event: threading.Event | None = None,
        on_state_change: Callable[[DownloadState], None] | None = None,
    ):
        """모델을 초기 상태(WAITING)로 생성한다.

        Args:
            pause_event: 일시정지 대기용 Event. 생략하면 새로 만든다.
            on_state_change: 상태가 실제로 바뀔 때 새 상태를 받는 콜백.
        """
        self._lock = threading.Lock()
        self._state = DownloadState.WAITING
        self._on_state_change = on_state_change

        self.pause_event = pause_event if pause_event is not None else threading.Event()
        self.pause_event.set()  # set 상태 = 진행 가능

        # 진행률 관련 필드. 값 갱신은 엔진(Phase 3에서 이주 예정)이 담당한다.
        self.total_size = 0
        self.threads_progress: list[int] = []
        self.start_time = 0.0
        self.end_time = 0.0

    @property
    def state(self) -> DownloadState:
        """현재 다운로드 상태."""
        return self._state

    def _transition(self, method: str, new_state: DownloadState) -> bool:
        """전이 공통 처리. 이미 목표 상태면 no-op(False), 전이 성공 시 True.

        허용되지 않는 출발 상태면 InvalidStateTransitionError를 던진다.
        콜백은 락 밖에서 호출한다 — 콜백이 다시 모델을 만져도 교착이 없게 하기 위함.
        """
        with self._lock:
            if self._state is new_state:
                return False
            if self._state not in _ALLOWED_SOURCES[method]:
                raise InvalidStateTransitionError(
                    f"{method}() 불가: {self._state.name} → {new_state.name} 전이는 허용되지 않는다"
                )
            self._state = new_state
        if self._on_state_change is not None:
            self._on_state_change(new_state)
        return True

    # ============ 상태 전이 ============

    def start(self) -> bool:
        """WAITING → RUNNING. 시작 시각을 기록한다."""
        changed = self._transition("start", DownloadState.RUNNING)
        if changed:
            self.start_time = time.time()
        return changed

    def pause(self) -> bool:
        """RUNNING → PAUSED. 다운로드 스레드를 대기 상태로 보낸다."""
        changed = self._transition("pause", DownloadState.PAUSED)
        if changed:
            self.pause_event.clear()
        return changed

    def resume(self) -> bool:
        """PAUSED → RUNNING. 대기 중인 다운로드 스레드를 깨운다."""
        changed = self._transition("resume", DownloadState.RUNNING)
        if changed:
            self.pause_event.set()
        return changed

    def stop(self) -> bool:
        """RUNNING/PAUSED → WAITING (취소). 이미 WAITING이면 no-op.

        일시정지 대기 중인 스레드가 있으면 깨워서 종료 경로로 보내야 하므로
        no-op 여부와 무관하게 Event는 항상 set한다.
        """
        changed = self._transition("stop", DownloadState.WAITING)
        self.pause_event.set()
        return changed

    def finish(self) -> bool:
        """RUNNING/PAUSED → FINISHED. 종료 시각을 기록한다."""
        changed = self._transition("finish", DownloadState.FINISHED)
        if changed:
            self.end_time = time.time()
            self.pause_event.set()
        return changed

    def fail(self) -> bool:
        """WAITING/RUNNING/PAUSED → FAILED. 종료 시각을 기록한다."""
        changed = self._transition("fail", DownloadState.FAILED)
        if changed:
            self.end_time = time.time()
            self.pause_event.set()
        return changed

    def is_running(self) -> bool:
        """현재 상태가 RUNNING인지 여부."""
        return self._state is DownloadState.RUNNING

    # ============ 진행률 집계 ============

    def init_threads_progress(self, thread_count: int) -> None:
        """스레드별 진행 바이트 배열을 0으로 초기화한다."""
        with self._lock:
            self.threads_progress = [0] * thread_count

    def set_thread_progress(self, index: int, downloaded: int) -> None:
        """index번 스레드의 누적 다운로드 바이트를 갱신한다."""
        with self._lock:
            self.threads_progress[index] = downloaded

    @property
    def downloaded_size(self) -> int:
        """스레드별 진행 바이트의 합 = 전체 다운로드된 크기."""
        with self._lock:
            return sum(self.threads_progress)

    @property
    def progress(self) -> float:
        """전체 진행률(0.0~100.0). total_size를 모르면(0 이하) 0.0."""
        total = self.total_size
        if total <= 0:
            return 0.0
        # 재시도로 total_size보다 많이 받은 경우에도 100을 넘기지 않는다
        return min(self.downloaded_size / total * 100.0, 100.0)
