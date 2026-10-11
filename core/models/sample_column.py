"""샘플마다 값 하나씩을 담는 읽기 전용 연속 배열 (#309).

mp4 색인은 샘플마다 시각 · 위치 · 크기를 든다. 긴 영상은 샘플이 수백만 개라, 값을 하나씩
파이썬 객체로 들면(튜플 속의 float · int) 색인 하나가 1GB를 넘는다 — 12.9시간 60fps
영상에서 약 1.2GB였다. 같은 값을 C 배열 하나에 이어 담으면 값마다 4~8바이트다.

쓰는 쪽에서는 튜플과 같다: 인덱스 · 조각 · 길이 · 순회 · ``in`` · ``bisect`` · ``min``/``max``/
``sorted``가 되고, 값이 같은 튜플 · 리스트와 ``==``이며, 고칠 수 없다. 꺼낸 값은 파이썬의
float · int다 — double은 파이썬 float와 같은 표현이라 값이 한 비트도 달라지지 않는다.

다른 점 하나: **조각(``column[a:b]``)은 표준 배열(``array.array``)이다.** 값을 읽는 데는
차이가 없지만 튜플과 ``==``로 견주려면 ``tuple(...)`` · ``list(...)``로 바꿔야 한다. 조각까지
같은 종류로 돌려주려면 인덱스 읽기(``__getitem__``)를 파이썬에서 감싸야 하는데, 그러면 값
하나를 읽을 때마다 파이썬 호출이 끼어 수백만 번 읽는 해석과 컷 준비가 몇 배 느려진다.
"""

import array
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from itertools import islice

from core.utils.interaction import rest_if_user_interacting
from core.utils.stop import check_stop

# 긴 표를 한 번에 채우거나 훑는 C 반복을 이 개수씩 잘라 돈다. C 안에서 도는 반복은 끝날 때까지
# GIL을 놓지 않는다 — 샘플이 180만 개인 표 하나를 통째로 돌면 0.1~0.3초 동안 다른 스레드(GUI)가
# 멈춘다(#309 실기: 백그라운드에서 색인을 만드는 2초 남짓 앱이 클릭을 받지 못했다). 조각 사이에서는
# 파이썬 코드로 돌아오므로 GIL을 넘겨줄 자리가 생긴다.
#
# 크기는 잰 값으로 정했다(약 184만 프레임의 합성 moov, 10ms 간격의 GUI 틱): 2**15 · 2**16개에서
# 틱의 최대 간격이 50ms 아래이고(한 번에 돌면 약 340ms), 2**17개부터 50ms를 넘는다. 더 잘게
# 잘라도 간격은 더 줄지 않는다. 잘라 도는 데 드는 시간은 크기와 거의 무관하다(해석 전체의 약 4%)
#
# 조각 사이는 일을 그만둘 수 있는 틈이기도 하다 — 그만두라는 답을 받았으면 여기서 멈춘다
# (``core.utils.stop``). 그만둘지 묻는 값을 걸어 두지 않은 스레드에서는 아무 일도 없다
CHUNK_ITEMS = 1 << 15
# 색인을 만드는 동안에는 더 잘게 자른다 (#309). 사용자가 조작하는 동안 조각 사이에서 쉬는데
# (``core.utils.interaction``), 조각이 길면 쉬는 틈이 드물어 화면 일이 그 사이에 걸린다 — 잰 값:
# 2**15개 · 1ms 쉼이면 화면 일의 95백분위가 약 32ms, 2**13개면 약 14ms다(쉴 때 약 7ms).
# 쉬지 않을 때의 해석 시간은 두 크기에서 같다
INDEX_CHUNK_ITEMS = 1 << 13

# 스레드마다 — 지금 색인을 만드는 중인지. 그 스레드에서만 잘게 자르고 조각 사이에서 쉰다
_local = threading.local()


@contextmanager
def building_index() -> Iterator[None]:
    """블록이 도는 동안 이 스레드의 조각을 ``INDEX_CHUNK_ITEMS``개로 줄이고 조각 사이에서 쉴 수 있게 한다.

    색인 해석(``core.api.mp4.parse_moov``)이 쓴다. 다른 곳의 조각 단위는 그대로다.
    """
    outer = getattr(_local, "building_index", False)
    _local.building_index = True
    try:
        yield
    finally:
        _local.building_index = outer


def chunk_items(items: int) -> int:
    """지금 이 스레드에서 쓸 조각의 크기 — 색인을 만드는 중이면 ``INDEX_CHUNK_ITEMS``를 넘지 않는다.

    Args:
        items: 부르는 모듈의 ``CHUNK_ITEMS`` — 모듈마다 호출 시점에 조회한 값을 넘긴다
    """
    if getattr(_local, "building_index", False):
        return min(items, INDEX_CHUNK_ITEMS)
    return items


def between_chunks() -> None:
    """조각 사이의 틈 — 그만둘지 묻고, 색인을 만드는 중이면 사용자가 조작하는 동안 잠깐 쉰다.

    Raises:
        StopRequested: 이 스레드에 걸어 둔 값이 그만두라고 답한 경우
    """
    check_stop()
    if getattr(_local, "building_index", False):
        rest_if_user_interacting()


_FLOAT = "d"  # C double — 파이썬 float와 같은 표현
_WIDE = "q"  # 64비트 정수 — 파일 안의 위치(co64는 64비트다)
_NARROW = "I"  # 32비트 부호 없는 정수 — 샘플 번호 · 샘플 크기(mp4의 그 칸들이 32비트다)


class SampleColumn(array.array):
    """값이 정해진 뒤 바뀌지 않는 연속 배열. 튜플처럼 읽고 튜플 · 리스트와 견준다."""

    __slots__ = ()

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (tuple, list)):
            return len(self) == len(other) and all(
                mine == theirs for mine, theirs in zip(self, other)
            )
        return array.array.__eq__(self, other)

    def __ne__(self, other: object) -> bool:
        equal = self.__eq__(other)
        return equal if equal is NotImplemented else not equal

    def __hash__(self) -> int:
        return hash((self.typecode, self.tobytes()))

    def __copy__(self) -> "SampleColumn":
        return self

    def __deepcopy__(self, memo: dict) -> "SampleColumn":
        return self

    def _read_only(self, *args, **kwargs):
        raise TypeError("SampleColumn은 고칠 수 없다")

    __setitem__ = __delitem__ = __iadd__ = __imul__ = _read_only
    append = extend = insert = pop = remove = reverse = byteswap = _read_only
    fromlist = frombytes = fromfile = fromunicode = _read_only


def fill_in_chunks(target: array.array, values: Iterable) -> array.array:
    """values를 ``CHUNK_ITEMS``개씩 잘라 target의 끝에 잇는다 — 한 번에 이은 것과 값이 같다.

    values는 한 번만 훑는다 — 누적(``accumulate``)처럼 앞의 값을 이어 가는 이터레이터도 조각의
    경계에서 끊기지 않는다. target이 ``SampleColumn``이어도 채운다(만드는 동안에만 쓴다).
    """
    source = iter(values)
    extend = array.array.extend  # SampleColumn은 스스로의 extend를 막아 둔다 — 기본 클래스의 것
    items = chunk_items(CHUNK_ITEMS)
    while True:
        between_chunks()
        before = len(target)
        extend(target, islice(source, items))
        if len(target) - before < items:
            return target


def array_in_chunks(typecode: str, values: Iterable) -> array.array:
    """``array.array(typecode, values)``와 같은 배열을 ``CHUNK_ITEMS``개씩 잘라 만든다."""
    return fill_in_chunks(array.array(typecode), values)


def in_chunks(values: array.array) -> Iterator[array.array]:
    """배열을 ``CHUNK_ITEMS``개씩의 조각으로 낸다 — 최솟값 · 최댓값 · 세기처럼 훑기만 하는 일에 쓴다."""
    items = chunk_items(CHUNK_ITEMS)
    for start in range(0, len(values), items):
        between_chunks()
        yield values[start : start + items]


def float_column(values: Iterable[float]) -> SampleColumn:
    """float 값들을 double 배열로 담는다 — 시각 · 길이(초). 긴 값들은 잘라서 채운다."""
    return fill_in_chunks(SampleColumn(_FLOAT), values)


def offset_column(values: Iterable[int]) -> SampleColumn:
    """파일 안의 위치(바이트)를 64비트 정수 배열로 담는다."""
    return SampleColumn(_WIDE, values)


def count_column(values: Iterable[int]) -> SampleColumn:
    """샘플 번호 · 샘플 크기처럼 0 이상이고 32비트에 드는 정수를 담는다.

    32비트를 넘는 값이 있으면(mp4의 칸으로는 나올 수 없다) 64비트 배열로 담는다. 긴 값들은
    잘라서 채운다 — 종류가 다른 배열에서 옮길 때도 값마다 바꾸는 반복이 C에서 돈다.
    """
    values = values if isinstance(values, (list, tuple, array.array)) else list(values)
    if isinstance(values, array.array) and values.typecode == _NARROW:
        return SampleColumn(_NARROW, values)  # 같은 종류의 배열 — 바이트를 통째로 옮긴다
    try:
        return fill_in_chunks(SampleColumn(_NARROW), values)
    except OverflowError:
        return fill_in_chunks(SampleColumn(_WIDE), values)
