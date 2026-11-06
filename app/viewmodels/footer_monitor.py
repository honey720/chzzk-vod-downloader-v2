"""하단바 모니터링 값 · 실행 결과 — 셸 S2 (#504).

모니터링(전체 속도 · 남은 시간 · 연결 수 · 메모리 · 여유 공간)은 엔진의 진행 통지에서 뜬 값
(`TransferReading`)을 1초마다 묶는다(`combine_readings`). 뷰모델은 엔진 공유 데이터를 직접 읽지
않는다(#484) — 통지의 `ProgressEvent` · `DownloadCounters`만 쓴다. 메모리 · 여유 공간은 앱이
스스로 잰다(`app/system/process_memory.py` · `app/system/free_space.py`).

실행 결과(`RunResult`)는 다운로드 작업이 끝났을 때(받는 중이던 것이 모두 끝나 다음 카드가 없는
순간)만 나타난다(설계 §2 메뉴 상태 버블과 같은 조건). 다운로드 없이 카드를 지워 완료만 남은
경우에는 나타나지 않는다.
"""

from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal

from app.viewmodels.data import ContentItem
from app.viewmodels.item_state import ItemState
from core.models.download_state import DownloadState
from core.models.events import DownloadCounters, ProgressEvent

_MEBIBYTE = 1024 * 1024  # 엔진 속도(MB/s)의 MB — 카드 남은 시간 계산과 같다


@dataclass(frozen=True)
class TransferReading:
    """받는 중인 다운로드 하나의 마지막 진행 통지에서 뜬 값 — 하단바가 합친다."""

    speed: float = 0.0  # 받는 속도(바이트/초)
    connections: int = 0  # 엔진이 지금 쓰는 연결 수
    remaining_bytes: float | None = None  # 남은 바이트의 어림 — 셀 수 없으면 None


@dataclass(frozen=True)
class MonitorSnapshot:
    """하단바 모니터링 한 번의 값 — 받는 것이 없으면 `receiving`이 거짓이고 여유 공간만 쓴다."""

    receiving: bool  # 받는 중인 다운로드가 있는지
    speed: float = 0.0  # 전체 속도(바이트/초)
    remaining_seconds: float | None = None  # 전체 남은 시간(초) — 셀 수 없는 다운로드가 섞이면 None
    connections: int = 0  # 전체 연결 수
    memory: int | None = None  # 앱 프로세스 메모리(바이트) — 읽지 못하면 None
    free: int | None = None  # 저장 위치 여유 공간(바이트) — 모르면 None


def transfer_reading(
    event: ProgressEvent, counters: DownloadCounters, is_segment_based: bool
) -> TransferReading:
    """진행 통지 하나에서 하단바 값을 뜬다 — 엔진의 관측 스레드에서도 불린다(순수 계산).

    남은 바이트는 카드의 남은 시간과 같은 식이다(`download_viewmodel._file_progress_args` ·
    `_segment_progress_args`). 파일은 받을 크기 − 받은 크기, 세그먼트 기반은 받은 세그먼트의 평균
    크기 × 남은 세그먼트 수다. 받을 크기를 모르는 파일 · 아직 받은 세그먼트가 없는 세그먼트 기반 ·
    후처리(병합 · 컷) 단계는 셀 수 없다(None).
    """
    speed = (event.speed or 0.0) * _MEBIBYTE
    connections = event.active_threads or 0
    remaining: float | None = None
    if not counters.merging:
        if is_segment_based:
            received = counters.completed_threads - counters.reused_threads
            if received > 0:
                left = counters.max_threads - counters.completed_threads
                remaining = event.downloaded_size / received * max(left, 0)
        elif event.total_size:
            remaining = float(max(event.total_size - event.downloaded_size, 0))
    return TransferReading(speed, connections, remaining)


def combine_readings(
    readings: list[TransferReading], memory: int | None, free: int | None
) -> MonitorSnapshot:
    """받는 중인 다운로드들의 값을 하단바 한 번의 값으로 묶는다.

    전체 남은 시간 = 남은 바이트의 합 ÷ 전체 속도. 남은 바이트를 셀 수 없는 다운로드가 하나라도
    있거나 전체 속도가 0이면 None이다 — 섞인 것을 빼고 낸 시간은 실제보다 짧다.
    """
    if not readings:
        return MonitorSnapshot(receiving=False, free=free)
    speed = sum(reading.speed for reading in readings)
    remaining = None
    if speed > 0 and all(reading.remaining_bytes is not None for reading in readings):
        remaining = sum(reading.remaining_bytes for reading in readings) / speed
    return MonitorSnapshot(
        receiving=True,
        speed=speed,
        remaining_seconds=remaining,
        connections=sum(reading.connections for reading in readings),
        memory=memory,
        free=free,
    )


class RunResult(QObject):
    """하단바의 실행 결과(「완료 N · 실패 N」)가 보이는지와 그 숫자를 맡는다.

    나타나는 때: 다운로드 작업이 끝났다(`finish` — 다음 카드가 없다). 그때 목록에 대기 · 조회 중
    카드가 있거나 목록이 비었으면 나타나지 않는다. 사라지는 때: 대기 · 조회 중 카드가 생겼다, 목록이
    비었다. 다음 실행은 대기 카드가 있어야 시작하므로 시작 전에 이미 사라져 있다(설계 §2 「다음 실행
    전까지」). 사라진 뒤에는 다음 작업이 끝날 때까지 다시 나타나지 않는다. 숫자는 목록 전체 카드의
    지금 상태다 — 보이는 동안 카드를 지우면 숫자가 바뀐다.

    시그널:
        changed: 보임이나 숫자가 바뀌었을 수 있다
    """

    changed = Signal()

    def __init__(self, model, order, parent: QObject | None = None):
        """결과를 만든다.

        Args:
            model: 카드 목록(`ContentListModel`) — 숫자를 셀 때 읽는다
            order: 칩 숫자(`CardOrder`) — 숫자가 바뀌면 사라질지 다시 본다
        """
        super().__init__(parent)
        self._model = model
        self._shown = False
        order.countsChanged.connect(self._onCountsChanged)

    def isShown(self) -> bool:
        """지금 결과가 보이는지."""
        return self._shown

    def counts(self) -> tuple[int, int]:
        """(완료, 실패) 카드 수 — 목록 전체 카드의 지금 상태."""
        finished = failed = 0
        for item in self._model.items:
            if item.downloadState == DownloadState.FINISHED:
                finished += 1
            elif item.downloadState == DownloadState.FAILED:
                failed += 1
        return finished, failed

    def finish(self) -> None:
        """다운로드 작업이 끝났다(다음 카드가 없다) — 목록이 결과만 남았으면 결과를 보인다."""
        self._shown = not self._blocked()
        self.changed.emit()

    def _blocked(self) -> bool:
        """결과를 보일 수 없는 목록인지 — 비었거나 대기 · 조회 중 카드가 있다."""
        items = self._model.items
        return not items or any(_waits(item) for item in items)

    def _onCountsChanged(self) -> None:
        """칩 숫자가 바뀌었다(카드 더하기 · 지우기 · 상태 바뀜) — 보이는 동안만 다시 본다."""
        if not self._shown:
            return
        if self._blocked():
            self._shown = False
        self.changed.emit()


def _waits(item: ContentItem) -> bool:
    """받을 차례를 기다리는 카드인지 — 대기 · 조회 중."""
    return item.downloadState in (DownloadState.WAITING, ItemState.LOADING)
