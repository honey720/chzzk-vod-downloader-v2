"""사용자가 조작하는 동안에만 색인 해석이 조각 사이에서 쉬는지 잰다 (#309).

쉬는 일(``core.utils.interaction._rest``)은 횟수만 세는 대역으로 바꾼다 — 실제로 자지 않는다.
"""

import array
import threading

import pytest

import core.api.mp4 as mp4_module
import core.models.sample_column as column_module
import core.utils.interaction as interaction
from core.api.mp4 import index_mp4, parse_moov, read_mp4_raw, summarize_moov
from core.models.plan import TimeRange
from core.models.sample_column import (
    CHUNK_ITEMS,
    INDEX_CHUNK_ITEMS,
    array_in_chunks,
    building_index,
    in_chunks,
)
from core.utils.interaction import REST_SECONDS, rest_if_user_interacting, resting_while
from core.utils.mp4_section_sizes import moov_sections_sizes
from tests.unit.core.mp4_builder import audio_spec, build_mp4, video_spec
from tests.unit.core.test_mp4_chunks import _columns


def _raw():
    """실제 mp4 바이트(10fps 12프레임)에서 받은 moov."""
    data = build_mp4([video_spec(), audio_spec()]).data
    return read_mp4_raw(lambda offset, size: data[offset : offset + size])


@pytest.fixture
def rests(monkeypatch):
    """쉰 횟수를 담는 리스트 — 쉬는 일을 자지 않고 세기만 하는 대역으로 바꾼다."""
    counted: list[int] = []
    monkeypatch.setattr(interaction, "_rest", lambda: counted.append(1))
    return counted


@pytest.fixture
def tiny_chunks(monkeypatch):
    """조각을 2개씩으로 줄인다 — 12프레임짜리 moov도 여러 조각으로 돈다."""
    monkeypatch.setattr(column_module, "CHUNK_ITEMS", 2)
    monkeypatch.setattr(mp4_module, "CHUNK_ITEMS", 2)


class _First:
    """처음 n번의 물음에만 조작 중이라고 답한다. 물은 횟수를 센다."""

    def __init__(self, n: int):
        self.n = n
        self.asked = 0

    def __call__(self) -> bool:
        self.asked += 1
        return self.asked <= self.n


# ================================================================ 조작 중인지 묻기


def test_rest_does_nothing_when_nothing_is_set_on_the_thread(rests):
    """조작 중인지 묻는 값을 걸지 않은 스레드에서는 쉬지 않아야 한다.

    걸어 둔 값 없음 -> rest_if_user_interacting() 뒤 쉰 횟수 == 0
    """
    rest_if_user_interacting()

    assert rests == []


@pytest.mark.parametrize(("answer", "expected"), [(True, 1), (False, 0)], ids=["조작 중", "아님"])
def test_rest_follows_the_value_set_on_the_thread(rests, answer, expected):
    """걸어 둔 값이 조작 중이라고 답할 때만 쉬어야 한다.

    resting_while(늘 참 / 늘 거짓) 안에서 rest_if_user_interacting() 한 번
    -> 쉰 횟수 == 1 / 0. 블록을 나온 뒤 한 번 더 불러도 쉰 횟수가 그대로다
    """
    with resting_while(lambda: answer):
        rest_if_user_interacting()
    rest_if_user_interacting()

    assert len(rests) == expected


def test_inner_value_wins_and_the_outer_one_comes_back(rests):
    """겹쳐 걸면 안쪽 값을 따르고, 안쪽을 나오면 바깥 값으로 돌아와야 한다.

    바깥 = 늘 참, 안쪽 = 늘 거짓. 안쪽에서 한 번 · 안쪽을 나와 한 번 부름 -> 쉰 횟수 == 0 → 1
    """
    with resting_while(lambda: True):
        with resting_while(lambda: False):
            rest_if_user_interacting()
            assert rests == []
        rest_if_user_interacting()

    assert len(rests) == 1


def test_resting_while_given_none_keeps_the_outer_value(rests):
    """None을 걸면 바깥 값을 그대로 따라야 한다.

    바깥 = 늘 참, 안쪽 = None. 안쪽에서 한 번 부름 -> 쉰 횟수 == 1
    """
    with resting_while(lambda: True):
        with resting_while(None):
            rest_if_user_interacting()

    assert len(rests) == 1


def test_value_set_on_one_thread_does_not_reach_another(rests):
    """한 스레드에 건 값은 다른 스레드의 일을 쉬게 하지 않아야 한다.

    메인 스레드 = 늘 참을 건 채로, 다른 스레드에서 rest_if_user_interacting() -> 쉰 횟수 == 0.
    그 뒤 메인 스레드에서 부르면 1(값이 걸려 있음을 같은 테스트에서 확인)
    """
    with resting_while(lambda: True):
        other = threading.Thread(target=rest_if_user_interacting)
        other.start()
        other.join()
        assert rests == []
        rest_if_user_interacting()

    assert len(rests) == 1


def test_rest_sleeps_for_the_rest_time(monkeypatch):
    """쉬는 일은 REST_SECONDS(1ms)만큼 자야 한다.

    time.sleep을 받은 값을 적는 대역으로 바꾸고 resting_while(늘 참) 안에서 한 번 부름
    -> 적힌 값 == [0.001]
    """
    slept: list[float] = []
    monkeypatch.setattr(interaction.time, "sleep", slept.append)

    with resting_while(lambda: True):
        rest_if_user_interacting()

    assert slept == [REST_SECONDS] == [0.001]


# ================================================================ 조각의 크기


def test_chunks_are_smaller_only_while_building_an_index():
    """색인을 만드는 동안에만 조각이 INDEX_CHUNK_ITEMS(8192)개여야 하고 밖에서는 그대로여야 한다.

    값 20,000개의 배열을 in_chunks로 자름
    -> building_index() 안: 조각 길이 [8192, 8192, 3616], 밖: [20000] (CHUNK_ITEMS 32,768보다 짧다)
    """
    values = array.array("q", range(20_000))
    assert (INDEX_CHUNK_ITEMS, CHUNK_ITEMS) == (8192, 32768)

    with building_index():
        inside = [len(chunk) for chunk in in_chunks(values)]
    outside = [len(chunk) for chunk in in_chunks(values)]

    assert inside == [8192, 8192, 3616]
    assert outside == [20_000]


def test_filling_rests_once_per_chunk_only_while_building_an_index(rests):
    """배열을 채우는 일은 색인을 만드는 동안에만 조각마다 한 번 쉬어야 한다.

    resting_while(늘 참). 값 20,000개를 array_in_chunks로 채움
    -> building_index() 안: 쉰 횟수 3(8192 · 8192 · 3616), 밖: 0. 채운 값은 어느 쪽도 range(20000)
    """
    with resting_while(lambda: True):
        outside = array_in_chunks("q", range(20_000))
        assert rests == []
        with building_index():
            inside = array_in_chunks("q", range(20_000))

    assert len(rests) == 3
    assert list(inside) == list(outside) == list(range(20_000))


def test_building_index_restores_the_outer_state(rests):
    """building_index()를 나오면 그 스레드의 조각이 다시 쉬지 않아야 한다.

    resting_while(늘 참). building_index() 안에서 값 3개를 채우고(쉰 횟수 1), 나와서 다시 채움
    -> 쉰 횟수 == 1 그대로
    """
    with resting_while(lambda: True):
        with building_index():
            array_in_chunks("q", range(3))
        assert len(rests) == 1
        array_in_chunks("q", range(3))

    assert len(rests) == 1


def test_parser_passes_rest_once_per_index_chunk_only_while_building_an_index(rests):
    """해석기의 세 반복(정렬 · 최댓값 · 자리 찾기)도 색인을 만드는 동안에만 8192개마다 쉬어야 한다.

    resting_while(늘 참). 값 20,000개(오름차순)로 _presentation_order · _largest · _positions_in을 부름
    -> building_index() 안: 반복마다 쉰 횟수 3, 밖: 0. 결과는 어느 쪽도 같다
    """
    ticks = array.array("q", range(20_000))
    wanted = frozenset(range(0, 20_000, 7))

    def passes() -> tuple[list[int], list]:
        counts, results = [], []
        for run in (
            lambda: mp4_module._presentation_order(ticks, ticks, 0),
            lambda: mp4_module._largest(map(float, ticks)),
            lambda: mp4_module._positions_in(ticks, wanted),
        ):
            before = len(rests)
            results.append(run())
            counts.append(len(rests) - before)
        return counts, results

    with resting_while(lambda: True):
        outside_counts, outside = passes()
        with building_index():
            inside_counts, inside = passes()

    assert outside_counts == [0, 0, 0]
    assert inside_counts == [3, 3, 3]
    assert inside == outside


# ================================================================ 색인 해석


def test_index_rests_between_chunks_while_the_user_is_interacting(tiny_chunks, rests):
    """조작 중이라고 답하는 동안 색인 해석은 조각 사이마다 쉬어야 한다.

    조각 2개씩. resting_while(늘 참) 안에서 index_mp4 -> 쉰 횟수 == 조작 중인지 물은 횟수 > 10
    """
    asked = _First(10**9)

    with resting_while(asked):
        index_mp4(_raw())

    assert asked.asked > 10, "전제: 조각 사이에서 여러 번 묻는다"
    assert len(rests) == asked.asked


def test_index_rests_only_for_the_gaps_where_the_user_is_interacting(tiny_chunks, rests):
    """조작 중이라는 답이 끊기면 그 뒤의 조각 사이에서는 쉬지 않아야 한다.

    조각 2개씩. 처음 5번의 물음에만 조작 중이라고 답함 -> 쉰 횟수 == 5, 물은 횟수 > 10
    """
    asked = _First(5)

    with resting_while(asked):
        index_mp4(_raw())

    assert asked.asked > 10
    assert len(rests) == 5


def test_index_does_not_rest_when_no_value_is_given(tiny_chunks, rests):
    """조작 중인지 묻는 값을 주지 않으면 색인 해석은 쉬지 않아야 한다(헤드리스).

    조각 2개씩. 값을 걸지 않고 index_mp4 -> 쉰 횟수 == 0.
    같은 moov를 resting_while(늘 참)으로 다시 해석하면 쉰 횟수 > 0(대역이 살아 있음)
    """
    index_mp4(_raw())
    assert rests == []

    with resting_while(lambda: True):
        index_mp4(_raw())

    assert len(rests) > 0


@pytest.mark.parametrize("items", [2, 3, 5])
def test_index_is_the_same_whether_or_not_it_rests(monkeypatch, items):
    """쉬어 가며 만든 색인은 쉬지 않고 만든 색인과 모든 값이 같아야 한다.

    조각 items개씩. 실제로 잔다(1ms). resting_while(늘 참)으로 만든 색인의 모든 표(종류 · 바이트)
    -> 값을 걸지 않고 만든 색인의 것과 같다
    """
    monkeypatch.setattr(column_module, "CHUNK_ITEMS", items)
    monkeypatch.setattr(mp4_module, "CHUNK_ITEMS", items)
    plain = _columns(index_mp4(_raw()).index)

    with resting_while(lambda: True):
        rested = _columns(index_mp4(_raw()).index)

    assert rested == plain


def test_index_cut_at_the_index_chunk_size_equals_one_cut_at_the_usual_size(monkeypatch):
    """색인의 조각 크기(8192)로 만든 색인은 다른 곳의 조각 크기(32,768)로 만든 것과 같아야 한다.

    영상 20,000샘플(B프레임 재배열 있음). INDEX_CHUNK_ITEMS를 2**40으로 바꿔 한 조각으로 만든 색인
    -> 제품 값(8192 — 표마다 세 조각)으로 만든 색인과 모든 표가 같다
    """
    from tests.unit.core.test_mp4_section_sizes import _audio, _head_only, _video

    raw = _head_only(
        [
            _video(20_000, seed=3, per_chunk=7, b_frames=True),
            _audio(9_000, seed=4, per_chunk=5),
        ]
    )
    cut = _columns(parse_moov(raw.moov))
    monkeypatch.setattr(column_module, "INDEX_CHUNK_ITEMS", 1 << 40)

    whole = _columns(parse_moov(raw.moov))

    assert cut == whole


def test_reading_without_an_index_does_not_rest(tiny_chunks, rests):
    """색인을 만들지 않는 읽기(프레임률 · 길이, 받을 크기)는 조작 중이어도 쉬지 않아야 한다.

    조각 2개씩. resting_while(늘 참) 안에서 summarize_moov · moov_sections_sizes(구간 0.2~0.8초)
    -> 쉰 횟수 == 0. 같은 블록에서 index_mp4를 부르면 쉰 횟수 > 0(대역이 살아 있음)
    """
    raw = _raw()

    with resting_while(lambda: True):
        summarize_moov(raw.moov)
        moov_sections_sizes(raw, [TimeRange(0.2, 0.8)])
        assert rests == []
        index_mp4(raw)

    assert len(rests) > 0
