"""사용자가 조작하는 동안에만 색인 해석과 색인 없는 읽기가 조각 사이에서 쉬는지 잰다 (#309).

쉬는 일(``core.utils.interaction._rest``)은 횟수만 세는 대역으로 바꾼다 — 실제로 자지 않는다.
"""

import array
import struct
import sys
import threading
from dataclasses import replace
from itertools import accumulate

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
    reading_tables,
)
from core.utils.interaction import (
    REST_SECONDS,
    rest_if_user_interacting,
    rest_totals,
    resting_while,
)
from core.utils.mp4_ranges import sections_head_size, sections_sizes
from core.utils.mp4_section_sizes import (
    moov_head_size,
    moov_sections_file_size,
    moov_sections_sizes,
)
from core.utils.stop import StopRequested, stoppable
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


def test_sample_positions_rest_at_least_once_per_index_chunk_of_samples(monkeypatch):
    """청크마다 샘플이 여럿인 표에서 샘플 위치를 만드는 동안 쉼 사이에 놓이는 샘플 수가 조각 하나 남짓이어야 한다.

    영상 20,000샘플 · 청크당 7 + 오디오 9,000샘플 · 청크당 5. resting_while(늘 참)으로 parse_moov.
    쉴 때마다 그때까지 _sample_offsets가 만든 위치의 수를 적음(시작 0 · 끝 = 샘플 수 포함)
    -> 트랙마다 이웃한 두 값의 차 <= INDEX_CHUNK_ITEMS + 청크당 샘플 수
    """
    from tests.unit.core.test_mp4_section_sizes import _audio, _head_only, _video

    raw = _head_only(
        [_video(20_000, seed=3, per_chunk=7, b_frames=True), _audio(9_000, seed=4, per_chunk=5)]
    )
    placed: list[list[int]] = []  # 트랙마다 쉴 때의 위치 수
    original = mp4_module._sample_offsets

    def recording(data, boxes, sizes):
        placed.append([0])
        offsets, chunk_starts = original(data, boxes, sizes)
        placed[-1].append(len(offsets))
        return offsets, chunk_starts

    def rest():
        frame = sys._getframe(1)
        while frame is not None and frame.f_code is not original.__code__:
            frame = frame.f_back
        # _sample_offsets 안에서 위치를 만들기 시작한 뒤에 쉰 것만 — 그때까지 만든 위치의 수.
        # 그 앞의 stsc 읽기(_u32_table)에서 쉰 것은 다음 테스트가 잰다
        if frame is not None and "offsets" in frame.f_locals:
            placed[-1].append(len(frame.f_locals["offsets"]))

    monkeypatch.setattr(mp4_module, "_sample_offsets", recording)
    monkeypatch.setattr(interaction, "_rest", rest)

    with resting_while(lambda: True):
        parse_moov(raw.moov)

    assert [track[-1] for track in placed] == [20_000, 9_000]  # 전제: 두 트랙의 위치를 다 만들었다
    gaps = [max(b - a for a, b in zip(track, track[1:])) for track in placed]
    assert gaps[0] <= INDEX_CHUNK_ITEMS + 7
    assert gaps[1] <= INDEX_CHUNK_ITEMS + 5


def test_stsc_read_rests_at_least_once_per_index_chunk_of_entries(monkeypatch):
    """stsc 항목이 많은 표를 읽는 동안 쉼 사이에 놓이는 항목 수가 조각 하나 이하여야 한다 (#408).

    영상 30,000샘플 · 청크당 1과 2가 번갈아(stsc 20,000항목). resting_while(늘 참)으로 parse_moov.
    쉴 때마다 그때까지 _u32_table이 읽은 항목의 수를 적음(시작 0 · 끝 = 항목 수 포함)
    -> 이웃한 두 값의 차 <= INDEX_CHUNK_ITEMS
    """
    from tests.unit.core.test_mp4_section_sizes import _head_only, _video

    video = _video(30_000, seed=5, per_chunk=1, b_frames=False)
    raw = _head_only([replace(video, chunks=[1, 2] * 10_000)])
    read: list[list[int]] = []  # 표마다 쉴 때의 항목 수
    original = mp4_module._u32_table

    def recording(data, span, columns):
        read.append([0])
        rows = original(data, span, columns)
        read[-1].append(len(rows))
        return rows

    def rest():
        frame = sys._getframe(1)
        while frame is not None and frame.f_code is not original.__code__:
            frame = frame.f_back
        if frame is not None:  # _u32_table 안에서 쉰 것만 — 그때까지 읽은 항목의 수
            read[-1].append(len(frame.f_locals["rows"]))

    monkeypatch.setattr(mp4_module, "_u32_table", recording)
    monkeypatch.setattr(interaction, "_rest", rest)

    with resting_while(lambda: True):
        parse_moov(raw.moov)

    assert [table[-1] for table in read] == [20_000]  # 전제: stsc 항목을 다 읽었다
    assert max(b - a for a, b in zip(read[0], read[0][1:])) <= INDEX_CHUNK_ITEMS


def test_ctts_read_rests_at_least_once_per_index_chunk_of_entries(monkeypatch):
    """ctts 항목이 많은 표를 읽는 동안 쉼 사이에 놓이는 항목 수가 조각 하나 이하여야 한다 (#408).

    영상 20,000샘플 · ctts 값이 2000과 0으로 번갈아(ctts 20,000항목). resting_while(늘 참)으로
    parse_moov. 쉴 때마다 그때까지 _composition_offsets가 읽은 항목의 수를 적음(시작 0 · 끝 포함)
    -> 이웃한 두 값의 차 <= INDEX_CHUNK_ITEMS
    """
    from tests.unit.core.test_mp4_section_sizes import _head_only, _video

    video = _video(20_000, seed=6, per_chunk=4, b_frames=False)
    raw = _head_only([replace(video, composition=[2000, 0] * 10_000)])
    read: list[list[int]] = []  # 표마다 쉴 때의 항목 수
    original = mp4_module._composition_offsets

    def recording(data, span, count):
        read.append([0])
        offsets = original(data, span, count)
        read[-1].append(struct.unpack_from(">I", data, span[0] + 4)[0])  # 끝 = 상자의 항목 수
        return offsets

    def rest():
        frame = sys._getframe(1)
        while frame is not None and frame.f_code is not original.__code__:
            frame = frame.f_back
        if frame is not None:  # _composition_offsets 안에서 쉰 것만 — 그때까지 읽은 항목의 수
            read[-1].append(len(frame.f_locals["counts"]))

    monkeypatch.setattr(mp4_module, "_composition_offsets", recording)
    monkeypatch.setattr(interaction, "_rest", rest)

    with resting_while(lambda: True):
        parse_moov(raw.moov)

    assert [table[-1] for table in read] == [20_000]  # 전제: ctts 표 하나 · 항목 20,000개
    assert max(b - a for a, b in zip(read[0], read[0][1:])) <= INDEX_CHUNK_ITEMS


def test_one_chunk_larger_than_a_piece_rests_inside_the_chunk(monkeypatch):
    """청크 하나의 샘플이 조각보다 많으면 그 청크 안에서도 쉼 사이의 샘플 수가 조각 하나 이하여야 한다 (#408).

    영상 20,000샘플 · 청크 하나. resting_while(늘 참)으로 parse_moov.
    쉴 때마다 그때까지 _sample_offsets가 만든 위치의 수를 적음(시작 0 · 끝 = 샘플 수 포함)
    -> 이웃한 두 값의 차 <= INDEX_CHUNK_ITEMS · 위치 = 청크 위치 + 앞 샘플들의 크기 합
    """
    from tests.unit.core.test_mp4_section_sizes import _head_only, _video

    video = _video(20_000, seed=7, per_chunk=20_000, b_frames=False)
    raw = _head_only([video])
    placed: list[int] = [0]  # 쉴 때의 위치 수
    original = mp4_module._sample_offsets

    def rest():
        frame = sys._getframe(1)
        while frame is not None and frame.f_code is not original.__code__:
            frame = frame.f_back
        if frame is not None and "offsets" in frame.f_locals:
            placed.append(len(frame.f_locals["offsets"]))

    monkeypatch.setattr(interaction, "_rest", rest)

    with resting_while(lambda: True):
        index = parse_moov(raw.moov)

    offsets = list(index.video.offsets)
    placed.append(len(offsets))
    assert len(offsets) == 20_000  # 전제: 샘플마다 위치가 있다
    assert max(b - a for a, b in zip(placed, placed[1:])) <= INDEX_CHUNK_ITEMS
    start = offsets[0]
    assert offsets == [start + before for before in accumulate(video.sizes[:-1], initial=0)]


@pytest.mark.parametrize(
    ("site", "per_chunk"),
    [
        ("_sample_offsets", 7),
        ("_extend_in_pieces", 20_000),  # 청크 하나 — 샘플이 조각보다 많다
        ("_u32_table", 7),
        ("_composition_offsets", 7),
    ],
)
def test_index_stops_at_each_rest_point_added_for_408(site, per_chunk):
    """색인을 만드는 도중 그만두라는 답이 site의 틈에서만 나오면 parse_moov가 StopRequested로 멈춰야 한다.

    영상 20,000샘플 · 청크당 per_chunk · B프레임(#408). 그만둘지 묻는 값이 between_chunks를 바로 부른
    함수가 site일 때만 참
    -> StopRequested · 참을 낸 것이 site의 틈에서 한 번
    """
    from tests.unit.core.test_mp4_section_sizes import _head_only, _video

    raw = _head_only([_video(20_000, seed=8, per_chunk=per_chunk, b_frames=True)])
    target = getattr(mp4_module, site).__code__
    gap = column_module.between_chunks.__code__
    answered: list[str] = []

    def should_stop() -> bool:
        frame = sys._getframe(1)
        while frame is not None and frame.f_code is not gap:
            frame = frame.f_back
        if frame is not None and frame.f_back.f_code is target:
            answered.append(site)
            return True
        return False

    with stoppable(should_stop), pytest.raises(StopRequested):
        parse_moov(raw.moov)

    assert answered == [site]


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


# ================================================================ 색인 없이 읽기 (요약 · 구간의 크기)


def _long_raw():
    """영상 20,000샘플(B프레임 재배열 있음) · 오디오 9,000샘플의 moov — 표마다 8192개 조각이 셋이다."""
    from tests.unit.core.test_mp4_section_sizes import _audio, _head_only, _video

    return _head_only(
        [
            _video(20_000, seed=3, per_chunk=7, b_frames=True),
            _audio(9_000, seed=4, per_chunk=5),
        ]
    )


_WANTED = [TimeRange(10.0, 60.0), TimeRange(200.0, 320.0)]  # 영상 길이 약 333초 안의 두 구간
# 이름 → 색인을 만들지 않고 moov의 표를 읽는 일
_LIGHT_READS = {
    "프레임률 · 길이": lambda raw: summarize_moov(raw.moov),
    "구간의 크기": lambda raw: moov_sections_sizes(raw, _WANTED),
    "구간 파일 크기의 합": lambda raw: moov_sections_file_size(raw, _WANTED),
    "머리 길이": lambda raw: moov_head_size(raw),
}
_light_read = pytest.mark.parametrize("read", _LIGHT_READS.values(), ids=_LIGHT_READS)


def test_chunks_are_smaller_while_reading_tables_and_back_to_usual_after():
    """reading_tables() 안에서는 조각이 8192개여야 하고 나오면 그대로여야 한다.

    값 20,000개의 배열을 in_chunks로 자름 -> 안: 조각 길이 [8192, 8192, 3616], 나온 뒤: [20000]
    """
    values = array.array("q", range(20_000))

    with reading_tables():
        inside = [len(chunk) for chunk in in_chunks(values)]
    outside = [len(chunk) for chunk in in_chunks(values)]

    assert inside == [8192, 8192, 3616]
    assert outside == [20_000]


@_light_read
def test_light_read_rests_at_every_gap_while_the_user_is_interacting(rests, read):
    """조작 중이라고 답하는 동안 색인 없이 읽는 일은 조각 사이마다 쉬어야 한다.

    영상 20,000샘플. resting_while(늘 참) 안에서 읽음 -> 쉰 횟수 == 조작 중인지 물은 횟수 >= 3
    """
    asked = _First(10**9)

    with resting_while(asked):
        read(_long_raw())

    assert asked.asked >= 3, "전제: 조각 사이에서 여러 번 묻는다"
    assert len(rests) == asked.asked


@_light_read
def test_light_read_does_not_rest_while_the_user_is_not_interacting(rests, read):
    """조작 중이 아니라고 답하면 색인 없이 읽는 일은 쉬지 않아야 한다.

    영상 20,000샘플. resting_while(늘 거짓 — 물은 횟수를 셈) 안에서 읽음
    -> 물은 횟수 >= 3, 쉰 횟수 == 0
    """
    asked = _First(0)

    with resting_while(asked):
        read(_long_raw())

    assert asked.asked >= 3
    assert rests == []


@_light_read
def test_light_read_rests_only_for_the_gaps_where_the_user_is_interacting(rests, read):
    """조작 중이라는 답이 끊기면 그 뒤의 조각 사이에서는 쉬지 않아야 한다.

    영상 20,000샘플. 처음 2번의 물음에만 조작 중이라고 답함 -> 쉰 횟수 == 2, 물은 횟수 >= 3
    """
    asked = _First(2)

    with resting_while(asked):
        read(_long_raw())

    assert asked.asked >= 3
    assert len(rests) == 2


@_light_read
def test_light_read_does_not_rest_when_no_value_is_given(rests, read):
    """조작 중인지 묻는 값을 주지 않으면 색인 없이 읽는 일은 쉬지 않아야 한다(헤드리스).

    영상 20,000샘플. 값을 걸지 않고 읽음 -> 쉰 횟수 == 0.
    같은 moov를 resting_while(늘 참)으로 다시 읽으면 쉰 횟수 > 0(대역이 살아 있음)
    """
    read(_long_raw())
    assert rests == []

    with resting_while(lambda: True):
        read(_long_raw())

    assert len(rests) > 0


@_light_read
def test_light_read_gives_the_same_value_whether_or_not_it_rests(read):
    """쉬어 가며 읽은 값은 쉬지 않고 읽은 값과 같아야 한다.

    영상 20,000샘플. 실제로 잔다(1ms). resting_while(늘 참)으로 읽은 값 == 값을 걸지 않고 읽은 값
    """
    plain = read(_long_raw())

    with resting_while(lambda: True):
        rested = read(_long_raw())

    assert rested == plain


def test_section_sizes_read_while_resting_equal_the_ones_counted_from_the_index():
    """쉬어 가며 센 구간의 크기 · 머리 길이는 색인으로 센 값과 같아야 한다.

    영상 20,000샘플, 구간 10~60초 · 200~320초. 실제로 잔다(1ms).
    resting_while(늘 참)으로 moov_sections_sizes · moov_head_size
    -> mp4_ranges.sections_sizes(색인) · sections_head_size(색인)과 같다. 받을 크기 > 0
    """
    raw = _long_raw()
    index = index_mp4(raw).index
    expected = sections_sizes(index, _WANTED)

    with resting_while(lambda: True):
        sizes = moov_sections_sizes(raw, _WANTED)
        head = moov_head_size(raw)

    assert sizes == expected
    assert head == sections_head_size(index) == expected[1]
    assert sizes[0] > 0


def test_summary_read_while_resting_equals_the_one_in_the_index():
    """쉬어 가며 읽은 프레임률 · 길이는 색인의 값과 같아야 한다.

    영상 20,000샘플. 실제로 잔다(1ms). resting_while(늘 참)으로 summarize_moov
    -> (fps, duration, frames) == (색인의 fps, 색인의 duration, 20000)
    """
    raw = _long_raw()
    index = index_mp4(raw).index

    with resting_while(lambda: True):
        summary = summarize_moov(raw.moov)

    assert (summary.fps, summary.duration, summary.frames) == (index.fps, index.duration, 20_000)


# ================================================================ 쉰 횟수 · 시간


class _Ticking:
    """time 모듈의 대역 — sleep을 부르면 시계가 그만큼이 아니라 정한 값만큼 간다."""

    def __init__(self, step: float):
        self.now = 50.0
        self.step = step

    def perf_counter(self) -> float:
        return self.now

    def sleep(self, _seconds: float) -> None:
        self.now += self.step


def test_rest_totals_count_the_rests_and_the_time_they_took(monkeypatch):
    """rest_totals()는 이 스레드가 쉰 횟수와 쉬는 데 실제로 흐른 시간의 합을 돌려줘야 한다.

    쉴 때마다 시계가 0.25초 가는 대역. resting_while(늘 참) 안에서 3번 쉼
    -> 앞뒤의 차 == (3, 0.75). 조작 중이 아니라고 답한 틈 2번은 세지 않는다
    """
    monkeypatch.setattr(interaction, "time", _Ticking(0.25))
    before = rest_totals()

    with resting_while(lambda: True):
        for _ in range(3):
            rest_if_user_interacting()
    with resting_while(lambda: False):
        for _ in range(2):
            rest_if_user_interacting()
    after = rest_totals()

    assert (after[0] - before[0], after[1] - before[1]) == (3, 0.75)


def test_rest_totals_belong_to_the_thread_that_rested(rests):
    """다른 스레드가 쉰 것은 이 스레드의 rest_totals()에 들지 않아야 한다.

    다른 스레드가 resting_while(늘 참)으로 2번 쉼 -> 그 스레드의 횟수 == 2, 이 스레드의 횟수는 그대로
    """
    before = rest_totals()
    seen: list[int] = []

    def other() -> None:
        with resting_while(lambda: True):
            rest_if_user_interacting()
            rest_if_user_interacting()
        seen.append(rest_totals()[0])

    thread = threading.Thread(target=other)
    thread.start()
    thread.join()

    assert seen == [2]
    assert rest_totals() == before
