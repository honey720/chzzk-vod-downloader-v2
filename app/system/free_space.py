"""저장 위치의 여유 공간을 워커 스레드에서 잰다 — 하단바 모니터링 (#504).

`shutil.disk_usage`는 무응답 네트워크 마운트에서 오래 멈출 수 있다 — 메인 스레드에서 부르면 창이
멈춘다(#136, 쓰기 프로브와 같은 까닭). 재는 일은 워커 스레드가 하고, 값은 큐 연결 Signal로 메인
스레드에 넘긴다. 같은 경로의 앞 측정이 아직 끝나지 않았으면 새로 재지 않는다 — 1초마다 불러도 멈춘
측정이 스레드를 쌓지 않는다. 재는 중인지는 경로마다 따로 둔다 — 한 경로(무응답 마운트)의 측정이 멈춰
있어도 저장 위치를 다른 경로로 바꾸면 새 경로를 잰다. 도는 스레드 수의 상한은 서로 다른 경로 수다.
"""

import shutil
import threading

from PySide6.QtCore import QObject, Signal

from app.system.thread_handoff import MainCall


class FreeSpaceProbe(QObject):
    """한 경로의 여유 공간(바이트)을 워커 스레드에서 재고 마지막 값을 들고 있는다.

    시그널:
        measured(path, free): 잰 값이 메인 스레드에 닿았다 — 재지 못했으면 free가 None
    """

    measured = Signal(str, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()  # _busy를 고치는 동안 잡는다
        self._busy: set[str] = set()  # 지금 재는 중인 경로
        self._values: dict[str, int | None] = {}  # 경로 → 마지막으로 잰 값
        # 워커의 결과는 `MainCall`이 메인 스레드에서 이 Signal로 낸다 — 바운드 메서드로 잇는다
        self._finished.connect(self._onFinished)

    _finished = Signal(str, object)  # 내부 전용: 잰 값이 메인 스레드에 닿는 입구

    def value(self, path: str) -> int | None:
        """그 경로를 마지막으로 잰 여유 공간 — 아직 재지 않았거나 재지 못했으면 None."""
        return self._values.get(path)

    def request(self, path: str) -> bool:
        """그 경로를 다시 재게 한다. 같은 경로의 앞 측정이 도는 중이면 아무것도 하지 않고 False."""
        if not path:
            return False
        with self._lock:
            if path in self._busy:
                return False
            self._busy.add(path)
        # 워커에는 잠금 · 재는 중인 경로와 `MainCall`만 넘긴다 — 측정기를 잡으면 측정기가 워커
        # 스레드에서 지워질 수 있다(#564)
        threading.Thread(
            target=_measure,
            args=(MainCall(self), self._lock, self._busy, path),
            name="FreeSpaceProbe",
            daemon=True,
        ).start()
        return True

    def _onFinished(self, path: str, free: int | None) -> None:
        """메인 스레드 — 값을 적고 알린다."""
        self._values[path] = free
        self.measured.emit(path, free)


def _measure(probe: MainCall, lock: threading.Lock, busy: set[str], path: str) -> None:
    """워커 스레드 — 재고 결과를 측정기의 내부 Signal로 넘긴다. 측정기의 값은 만지지 않는다.

    측정기는 `MainCall`로만 가리킨다 — 재는 사이에 측정기가 지워졌으면 결과는 버려진다.
    """
    try:
        free: int | None = shutil.disk_usage(path).free
    except (OSError, ValueError):
        free = None  # 사라진 경로 · 권한 없음 · 쓸 수 없는 경로 글
    finally:
        with lock:
            busy.discard(path)
    probe("_finished", path, free)
