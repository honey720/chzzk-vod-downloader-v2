"""창(`QWindow`) 경로 합성 포인터 이동이 hover를 세우게 하는 도우미 (#667).

Windows native 실행에서 확인한 두 가지 때문에 테스트 한 번의 합성 이동이 hover를 세우지 못한다.

- 바로 앞 합성 이동과 화면 좌표가 같은 이동은 창에 전달되지 않는다. 테스트 창은 매번 같은 자리에 뜨므로
  앞 테스트의 마지막 자리와 이번 테스트의 첫 자리가 같으면 그 이동이 버려진다.
- 새로 뜬 창이 Enter를 받기 전에 이동이 하나만 전달되면 어느 위젯도 hover 상태가 되지 않는다. 두 번째
  이동부터 hover가 선다. 실제 커서가 창 안에 있으면 OS가 보내는 Enter가 이것을 가려 줄 때가 있어
  결과가 실행마다 달라진다.

그래서 창의 첫 이동이거나 바로 앞 이동과 화면 좌표가 같을 때, 목표 자리에서 1px 옆으로 한 번 먼저
움직인다. 실제 커서(OS 커서)는 옮기지 않는다 — 창 경로 `QTest.mouseMove`는 합성 이벤트만 보낸다.
"""

import weakref
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QPoint
from PySide6.QtGui import QWindow

# 먼저 움직이는 자리 — 목표에서 오른쪽 1px(같은 위젯 안에 머무는 가장 작은 거리)
STEP = QPoint(1, 0)


class PointerMoves:
    """창 경로 합성 이동 앞에 필요하면 1px 옆 이동을 한 번 더한다.

    프로세스 하나에 하나만 둔다 — 버려지는 이동은 Qt가 프로세스 전체에서 기억하는 마지막 자리로 정해지므로
    테스트가 바뀌어도 마지막 자리를 잊지 않는다.
    """

    def __init__(self, move: Callable[..., Any]) -> None:
        """`move`는 원래 `QTest.mouseMove`다."""
        self._move = move
        self._last_window: weakref.ref[QWindow] | None = None
        self._last_global: QPoint | None = None

    def __call__(self, target: Any, pos: QPoint | None = None, *args: Any, **kwargs: Any) -> None:
        """`QTest.mouseMove`와 같은 인자를 받는다. 위젯 경로는 손대지 않고 그대로 넘긴다."""
        if not isinstance(target, QWindow):
            self._forget()
            self._call(target, pos, args, kwargs)
            return
        if pos is None or pos.isNull():
            # Qt가 빈 자리에 쓰는 창 가운데
            local = QPoint(target.width() // 2, target.height() // 2)
        else:
            local = pos
        goal = target.mapToGlobal(local)
        if self._needs_step(target, goal):
            step = local + STEP
            if target.mapToGlobal(step) == self._last_global:
                step = local - STEP
            self._move(target, step)
        self._call(target, pos, args, kwargs)
        self._last_window = weakref.ref(target)
        self._last_global = goal

    def _needs_step(self, window: QWindow, goal: QPoint) -> bool:
        last = self._last_window() if self._last_window is not None else None
        return last is not window or goal == self._last_global

    def _forget(self) -> None:
        # 위젯 경로는 실제 커서를 옮겨 마지막 자리를 바꾼다 — 다음 창 경로 이동은 새 창처럼 다룬다
        self._last_window = None
        self._last_global = None

    def _call(self, target: Any, pos: QPoint | None, args: tuple, kwargs: dict) -> None:
        if pos is None:
            self._move(target, *args, **kwargs)
        else:
            self._move(target, pos, *args, **kwargs)
