"""오래 도는 일을 밖에서 그만두게 하는 길 — 그만둘지 묻는 값을 받아 틈마다 묻는다 (#309).

긴 영상의 색인 해석은 몇 초가 걸린다. 그 결과를 쓸 곳이 사라지면(카드의 해상도를 또 바꿨다 ·
카드를 지웠다) 끝까지 돌 까닭이 없다 — 도는 동안 다른 스레드가 느려진다.

부르는 쪽이 "그만둘까?"에 답하는 값(``should_stop``)을 넘긴다. core는 그 값이 무엇을 보는지
모른다 — 앱의 상태를 import하지 않는다. 일을 하는 쪽은 스스로 끊을 수 있는 틈(조각 하나를
끝낸 자리 · 단계 사이)에서 ``check_stop()``을 부른다.

그 값은 일을 돌리는 **스레드에** 걸어 둔다(``stoppable``). 해석은 여러 함수가 조각마다 도는
구조라, 인자로 넘기려면 그 모든 함수에 인자가 하나씩 늘어난다. 스레드에 걸어 두면 조각을
자르는 한 곳(``core.models.sample_column``)만 묻고, 걸지 않은 스레드에서는 아무 일도 없다.
"""

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

# 스레드마다 걸어 둔 "그만둘까?" 값 — 걸지 않았으면 속성이 없다
_local = threading.local()


class StopRequested(Exception):
    """그만두라는 답을 받아 일을 멈췄다 — 실패가 아니다.

    ``Mp4Error`` 같은 해석 실패와 섞이지 않게 ``Exception``을 바로 잇는다. 멈춘 일은 처음부터
    다시 할 수 있다(중간 결과를 남기지 않는다).
    """


@contextmanager
def stoppable(should_stop: Callable[[], bool] | None) -> Iterator[None]:
    """이 스레드에서 도는 일이 ``should_stop``에 물어 그만둘 수 있게 한다.

    겹쳐 쓰면 안쪽 것이 이긴다. 나갈 때 바깥 것으로 되돌린다. None을 주면 바깥 것을 그대로 둔다.

    Args:
        should_stop: 참을 돌려주면 그만둔다. 어느 스레드에서 불려도 되는 값이어야 한다 —
            조각마다 불리므로 가벼워야 한다(``threading.Event.is_set`` 등)
    """
    if should_stop is None:
        yield
        return
    outer = getattr(_local, "should_stop", None)
    _local.should_stop = should_stop
    try:
        yield
    finally:
        _local.should_stop = outer


def stop_requested() -> bool:
    """이 스레드에 걸어 둔 값이 그만두라고 답하는지. 걸어 둔 것이 없으면 거짓이다."""
    should_stop = getattr(_local, "should_stop", None)
    return should_stop is not None and bool(should_stop())


def check_stop() -> None:
    """그만두라는 답이면 ``StopRequested``를 던진다. 끊어도 되는 틈에서만 부른다.

    Raises:
        StopRequested: 이 스레드에 걸어 둔 값이 참을 돌려준 경우
    """
    if stop_requested():
        raise StopRequested
