"""사용자가 화면을 만지는 동안 오래 도는 일이 틈마다 잠깐 쉬게 하는 길 (#309).

긴 영상의 색인 해석은 몇 초 동안 CPU를 쓴다. 그동안 화면 스레드는 파이썬으로 들어올 때마다
차례를 기다려, 창 크기를 끄는 한 걸음이 100ms를 넘는다(실측: 쉴 때 약 7ms → 해석 중 약 120ms).
해석하는 쪽이 조각 사이에서 1ms 자면 그 사이에 화면 스레드가 한 걸음을 끝낸다(약 8.5ms).
항상 자면 해석이 3~4배 걸리므로 **사용자가 조작하는 동안에만** 잔다.

부르는 쪽이 "지금 사용자가 조작 중인가?"에 답하는 값(``user_interacting``)을 넘긴다. core는 그
값이 무엇을 보는지 모른다 — 화면을 import하지 않는다. 그만둘지 묻는 값(``core.utils.stop``)과
같이 일을 돌리는 **스레드에** 걸어 둔다. 걸지 않은 스레드는 쉬지 않는다(헤드리스 · 테스트).
"""

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

# 조작 중일 때 조각 사이에서 쉬는 시간(초). 잰 값으로 정했다(6시간 60fps의 합성 moov, 60Hz로
# 창 폭을 바꾸는 화면 일): 1ms면 화면 일의 중앙값이 쉴 때와 거의 같고, 2ms는 조금 더 부드럽지만
# 해석이 1.5배 더 걸린다
REST_SECONDS = 0.001

# 스레드마다 걸어 둔 "조작 중인가?" 값 — 걸지 않았으면 속성이 없다
_local = threading.local()


@contextmanager
def resting_while(user_interacting: Callable[[], bool] | None) -> Iterator[None]:
    """이 스레드에서 도는 일이 ``user_interacting``이 참인 동안 틈마다 쉬게 한다.

    겹쳐 쓰면 안쪽 것이 이긴다. 나갈 때 바깥 것으로 되돌린다. None을 주면 바깥 것을 그대로 둔다.

    Args:
        user_interacting: 참을 돌려주면 그 틈에서 쉰다. 어느 스레드에서 불려도 되는 값이어야
            한다 — 조각마다 불리므로 가벼워야 한다
    """
    if user_interacting is None:
        yield
        return
    outer = getattr(_local, "user_interacting", None)
    _local.user_interacting = user_interacting
    try:
        yield
    finally:
        _local.user_interacting = outer


def rest_if_user_interacting() -> None:
    """이 스레드에 걸어 둔 값이 조작 중이라고 답하면 ``REST_SECONDS`` 쉰다. 없으면 쉬지 않는다."""
    user_interacting = getattr(_local, "user_interacting", None)
    if user_interacting is not None and user_interacting():
        _rest()


def _rest() -> None:
    """잠깐 잔다 — 자는 동안 GIL을 놓는다."""
    time.sleep(REST_SECONDS)
