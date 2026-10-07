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
from collections.abc import Iterable

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


def float_column(values: Iterable[float]) -> SampleColumn:
    """float 값들을 double 배열로 담는다 — 시각 · 길이(초)."""
    return SampleColumn(_FLOAT, values)


def offset_column(values: Iterable[int]) -> SampleColumn:
    """파일 안의 위치(바이트)를 64비트 정수 배열로 담는다."""
    return SampleColumn(_WIDE, values)


def count_column(values: Iterable[int]) -> SampleColumn:
    """샘플 번호 · 샘플 크기처럼 0 이상이고 32비트에 드는 정수를 담는다.

    32비트를 넘는 값이 있으면(mp4의 칸으로는 나올 수 없다) 64비트 배열로 담는다.
    """
    values = values if isinstance(values, (list, tuple, array.array)) else list(values)
    try:
        return SampleColumn(_NARROW, values)
    except OverflowError:
        return SampleColumn(_WIDE, values)
