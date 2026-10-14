"""샘플별 값을 담는 읽기 전용 연속 배열과, 색인이 표를 거기에 담는지 (#309)."""

import array
import copy
import pickle
from bisect import bisect_left

import pytest

from core.api.mp4 import parse_moov
from core.models.sample_column import SampleColumn, count_column, float_column, offset_column
from core.utils.hybrid_cut import cut_frames_from_mp4
from tests.unit.core.mp4_builder import audio_spec, build_mp4, video_spec


def test_column_reads_like_a_tuple():
    """배열은 인덱스 · 음수 인덱스 · 길이 · 순회 · in · bisect · min/max/sorted가 튜플과 같아야 한다.

    값 [0.5, 0.1, 0.3]
    -> [0] == 0.5, [-1] == 0.3, len == 3, list == 값, 0.1 in, max == 0.5, sorted == [0.1, 0.3, 0.5],
       꺼낸 값의 타입은 float
    """
    column = float_column([0.5, 0.1, 0.3])

    assert (column[0], column[-1], len(column)) == (0.5, 0.3, 3)
    assert list(column) == [0.5, 0.1, 0.3]
    assert 0.1 in column and 0.2 not in column
    assert (min(column), max(column), sorted(column)) == (0.1, 0.5, [0.1, 0.3, 0.5])
    assert type(column[0]) is float
    assert bisect_left(float_column([0.0, 0.1, 0.2]), 0.1) == 1


def test_column_equals_a_tuple_or_list_with_the_same_values():
    """배열은 값이 같은 튜플 · 리스트 · 배열과 같고, 값이나 길이가 다르면 달라야 한다.

    값 [1, 2, 3]
    -> == (1, 2, 3), == [1, 2, 3], == 같은 값의 배열, != (1, 2), != (1, 2, 4), 빈 배열 == ()
    """
    column = count_column([1, 2, 3])

    assert column == (1, 2, 3)
    assert column == [1, 2, 3]
    assert column == offset_column([1, 2, 3])
    assert column != (1, 2)
    assert column != (1, 2, 4)
    assert not (column != (1, 2, 3))
    assert count_column([]) == ()


def test_slice_of_a_column_holds_the_same_values():
    """배열의 조각은 그 자리의 값을 그대로 담아야 한다(표준 배열로 나온다 — 견줄 때는 list로 바꾼다).

    값 [10, 20, 30, 40]의 [1:3] -> list == [20, 30], max == 30, 원래 배열은 그대로
    """
    column = count_column([10, 20, 30, 40])

    part = column[1:3]

    assert list(part) == [20, 30]
    assert max(part) == 30
    assert column == (10, 20, 30, 40)


def test_reading_one_value_does_not_go_through_python():
    """값 하나를 읽는 일은 표준 배열의 것 그대로여야 한다 — 파이썬에서 감싸지 않는다.

    -> SampleColumn.__getitem__ is array.array.__getitem__ (감싸면 수백만 번 읽는 해석이 몇 배 느려진다)
    """
    assert SampleColumn.__getitem__ is array.array.__getitem__


@pytest.mark.parametrize(
    "change",
    [
        lambda column: column.append(1),
        lambda column: column.extend([1]),
        lambda column: column.insert(0, 1),
        lambda column: column.pop(),
        lambda column: column.remove(1),
        lambda column: column.reverse(),
        lambda column: column.frombytes(b"\x00" * 8),
        lambda column: column.__setitem__(0, 9),
        lambda column: column.__delitem__(0),
        lambda column: column.__iadd__(column),
    ],
)
def test_column_cannot_be_changed(change):
    """배열을 고치는 호출은 TypeError를 내고 값이 그대로여야 한다.

    값 [1, 2, 3]에 append · extend · insert · pop · remove · reverse · frombytes · 대입 · 삭제 · +=
    -> TypeError, 값은 (1, 2, 3)
    """
    column = offset_column([1, 2, 3])

    with pytest.raises(TypeError):
        change(column)

    assert column == (1, 2, 3)


def test_column_hashes_and_copies_by_value():
    """값이 같은 배열은 해시가 같고, 복사 · 깊은 복사 · 피클을 거쳐도 같은 값의 배열이어야 한다.

    값 [0.25, 0.5]
    -> 같은 값의 두 배열의 hash가 같다, copy · deepcopy는 같은 객체, 피클을 거친 것은 SampleColumn이고 값이 같다
    """
    column = float_column([0.25, 0.5])

    assert hash(column) == hash(float_column([0.25, 0.5]))
    assert copy.copy(column) is column
    assert copy.deepcopy(column) is column
    restored = pickle.loads(pickle.dumps(column))
    assert isinstance(restored, SampleColumn)
    assert restored == (0.25, 0.5)


def test_count_column_widens_when_a_value_does_not_fit_32_bits():
    """count_column은 32비트에 드는 값은 4바이트로, 넘는 값이 있으면 8바이트로 담아야 한다.

    [1, 2] -> 값마다 4바이트 / [1, 2**32] -> 값마다 8바이트, 값은 그대로
    """
    narrow = count_column([1, 2])
    wide = count_column([1, 2**32])

    assert narrow.itemsize == 4
    assert wide.itemsize == 8
    assert wide == (1, 2**32)


def test_float_column_keeps_every_bit_of_the_values():
    """float_column에 담았다 꺼낸 값은 넣은 float와 비트까지 같아야 한다.

    0.1 + 0.2 · 1/3 · 1e-320(비정규) · -0.0 -> hex()가 넣은 값과 같다
    """
    values = [0.1 + 0.2, 1 / 3, 1e-320, -0.0]

    column = float_column(values)

    assert [value.hex() for value in column] == [value.hex() for value in values]


def test_index_keeps_every_per_sample_table_in_a_column():
    """해석한 색인은 샘플 · 프레임마다의 표를 모두 연속 배열로 들고 있어야 한다 — 튜플이 아니다.

    표준 재료(영상 12샘플 · 오디오 16샘플)를 해석
    -> 프레임 표 셋 · 트랙마다 표 일곱이 SampleColumn, 시각 · 길이 · 위치는 값마다 8바이트,
       크기 · 샘플 번호는 값마다 4바이트
    """
    index = parse_moov(build_mp4([video_spec(), audio_spec()]).moov)

    tables = [index.frame_pts, index.frame_samples, index.keyframes]
    for track in (index.video, index.audio):
        tables += [
            track.times,
            track.decode_times,
            track.durations,
            track.offsets,
            track.sizes,
            track.chunk_starts,
            track.sync_samples,
        ]
    assert all(isinstance(table, SampleColumn) for table in tables)
    assert {index.frame_pts.itemsize, index.video.times.itemsize, index.video.offsets.itemsize} == {
        8
    }
    assert {index.frame_samples.itemsize, index.video.sizes.itemsize} == {4}


def test_cut_frames_from_an_index_share_and_use_columns():
    """색인에서 뽑은 컷의 프레임 정보는 PTS를 색인과 함께 쓰고 DTS를 연속 배열로 들어야 한다.

    표준 재료를 해석해 cut_frames_from_mp4
    -> frame_pts is 색인의 frame_pts, frame_dts는 SampleColumn이고 길이가 프레임 수와 같다
    """
    index = parse_moov(build_mp4([video_spec(), audio_spec()]).moov)

    frames = cut_frames_from_mp4(index)

    assert frames.frame_pts is index.frame_pts
    assert isinstance(frames.frame_dts, SampleColumn)
    assert len(frames.frame_dts) == len(index.frame_pts) == 12
