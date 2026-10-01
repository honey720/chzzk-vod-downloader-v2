"""받은 범위만 이어 붙인 부분 mp4(core/utils/mp4_partial.py) 단위 테스트 (#309).

핵심 계약:
- 받을 범위는 파일의 머리(0부터 첫 샘플 직전)와 주어진 범위이고, 겹치는 범위는 합친다
- 부분 파일의 크기는 받을 바이트의 합이다
- 고친 머리로 부분 파일을 다시 해석하면, 받은 샘플은 부분 파일 안의 제 바이트를 가리키고
  받지 않은 샘플은 파일의 끝을 가리킨다

입력은 합성 mp4다(tests/unit/core/mp4_builder.py). 부분 파일은 테스트가 원본 bytes에서
범위를 잘라 이어 붙여 만든다 — 다운로더를 거치지 않는다.
"""

import struct

import pytest

from core.api.mp4 import MP4_INVALID, MP4_UNSUPPORTED, Mp4Error, parse_moov, read_mp4_index
from core.models.plan import TimeRange
from core.utils.mp4_partial import merge_ranges, patch_head, plan_partial
from core.utils.mp4_ranges import selection_byte_ranges
from tests.unit.core.mp4_builder import audio_spec, build_mp4, video_spec


def _reader(data: bytes):
    def read(offset: int, size: int) -> bytes:
        return data[offset : offset + size]

    return read


def _built(**options):
    built = build_mp4([video_spec(), audio_spec()], **options)
    return built, read_mp4_index(_reader(built.data))


def _partial_bytes(built, index, layout) -> bytes:
    """원본에서 받을 범위를 잘라 이어 붙이고 머리를 고친 부분 파일."""
    pieces = [built.data[first : last + 1] for first, last in layout.ranges]
    pieces[0] = patch_head(pieces[0], layout, index.moov_range)
    return b"".join(pieces)


# ================================================================ 범위 합치기


@pytest.mark.parametrize(
    ("ranges", "expected"),
    [
        ([(10, 19), (30, 39)], ((10, 19), (30, 39))),  # 떨어져 있다
        ([(10, 19), (15, 29)], ((10, 29),)),  # 겹친다
        ([(10, 19), (20, 29)], ((10, 29),)),  # 맞닿는다
        ([(30, 39), (10, 19)], ((10, 19), (30, 39))),  # 순서가 뒤집혀 있다
        ([(10, 39), (15, 19)], ((10, 39),)),  # 하나가 다른 하나 안에 든다
        ([(10, 19), (15, 29), (25, 40), (50, 60)], ((10, 40), (50, 60))),  # 셋이 이어 겹친다
    ],
)
def test_merge_ranges_joins_overlapping_and_touching_ranges(ranges, expected):
    """merge_ranges는 겹치거나 맞닿은 범위를 하나로 합치고 오름차순으로 돌려줘야 한다.

    주석의 경우마다 (시작, 끝) 목록
    -> 합친 목록
    """
    assert merge_ranges(ranges) == expected


# ================================================================ 배치


def test_plan_partial_puts_the_file_head_first():
    """plan_partial은 0부터 첫 샘플 직전까지를 첫 범위로 두어야 한다.

    합성 mp4(moov가 앞), 구간 범위 하나
    -> 첫 범위 == (0, 첫 샘플의 위치 − 1)이고 moov를 다 담는다
    """
    built, index = _built()
    picked = selection_byte_ranges(index, TimeRange(1.0, 1.2))

    layout = plan_partial(index, picked.ranges)

    assert layout.ranges[0] == (0, built.mdat_body[0] - 1)
    assert layout.ranges[0][1] >= index.moov_range[1]
    assert layout.starts[0] == 0


def test_plan_partial_size_is_the_sum_of_ranges():
    """plan_partial의 size는 받을 범위의 바이트 수 합이고, 범위는 부분 파일에 빈틈없이 놓여야 한다.

    합성 mp4, 구간 범위 하나
    -> size == 머리 + 구간 범위, starts[1] == 머리의 길이
    """
    built, index = _built()
    picked = selection_byte_ranges(index, TimeRange(1.0, 1.2))

    layout = plan_partial(index, picked.ranges)

    head = built.mdat_body[0]
    assert layout.size == head + picked.total_size
    assert layout.starts == (0, head)
    assert layout.size < len(built.data)


def test_plan_partial_counts_overlapping_ranges_once():
    """plan_partial은 겹치는 범위를 합쳐 같은 바이트를 한 번만 세야 한다.

    합성 mp4, 서로 겹치는 구간 0.5~0.6초와 0.5~0.9초
    -> size == 뒤 구간 하나만 줬을 때의 size
    """
    _built_file, index = _built()
    narrow = selection_byte_ranges(index, TimeRange(0.5, 0.6)).ranges
    wide = selection_byte_ranges(index, TimeRange(0.5, 0.9)).ranges

    assert plan_partial(index, [*narrow, *wide]).size == plan_partial(index, wide).size


def test_layout_position_maps_received_offsets_and_rejects_the_rest():
    """PartialLayout.position은 받는 자리는 부분 파일 안의 위치로, 받지 않는 자리는 None으로 답해야 한다.

    합성 mp4, 구간 1.0~1.2초의 범위 (first, last)
    -> position(first) == 머리의 길이, position(last) == size − 1, position(first − 1) is None
    """
    built, index = _built()
    first, last = selection_byte_ranges(index, TimeRange(1.0, 1.2)).ranges[0]

    layout = plan_partial(index, [(first, last)])

    assert layout.position(0) == 0
    assert layout.position(first) == built.mdat_body[0]
    assert layout.position(last) == layout.size - 1
    assert layout.position(first - 1) is None
    assert layout.position(last + 1) is None


def test_plan_partial_rejects_moov_behind_the_samples():
    """plan_partial은 moov가 샘플보다 뒤에 있으면 미지원 키로 거부해야 한다.

    합성 mp4(ftyp · mdat · moov)
    -> Mp4Error(MP4_UNSUPPORTED)
    """
    _built_file, index = _built(moov_first=False)

    with pytest.raises(Mp4Error) as info:
        plan_partial(index, [(0, 10)])

    assert info.value.message_key == MP4_UNSUPPORTED


def test_plan_partial_rejects_index_without_moov_location():
    """plan_partial은 moov의 위치를 모르는 색인을 미지원 키로 거부해야 한다.

    parse_moov(bytes)로만 만든 색인(moov_range is None)
    -> Mp4Error(MP4_UNSUPPORTED)
    """
    built = build_mp4([video_spec(), audio_spec()])

    with pytest.raises(Mp4Error) as info:
        plan_partial(parse_moov(built.moov), [(0, 10)])

    assert info.value.message_key == MP4_UNSUPPORTED


# ================================================================ 머리 고치기


@pytest.mark.parametrize("co64", [False, True], ids=["stco", "co64"])
def test_patched_partial_file_points_received_samples_at_their_bytes(co64):
    """고친 부분 파일을 다시 해석하면 받은 샘플의 위치에 조립기가 쓴 그 샘플의 바이트가 있어야 한다.

    합성 mp4(stco · co64), 구간 0.5~0.6초 → 영상 샘플 3~7 · 오디오 샘플 0~7을 받는다
    -> 부분 파일의 색인에서 그 샘플들의 위치를 읽으면 원본의 그 샘플 바이트
    """
    built, index = _built(co64=co64)
    layout = plan_partial(index, selection_byte_ranges(index, TimeRange(0.5, 0.6)).ranges)

    partial = _partial_bytes(built, index, layout)
    moved = read_mp4_index(_reader(partial))

    assert len(partial) == layout.size
    for handler, samples, track in (
        (b"vide", range(3, 8), moved.video),
        (b"soun", range(0, 8), moved.audio),
    ):
        for sample in samples:
            start, size = track.offsets[sample], track.sizes[sample]
            assert partial[start : start + size] == built.sample_bytes(handler, sample, size)


def test_patched_partial_file_points_missing_chunks_at_end_of_file():
    """고친 부분 파일에서 받지 않은 청크의 위치는 부분 파일의 끝이어야 한다.

    합성 mp4, 구간 0.5~0.6초 → 영상 청크 (0~2) · (8~11)은 받지 않는다
    -> 부분 파일의 색인에서 영상 샘플 0과 8의 위치 == 부분 파일의 크기
    """
    built, index = _built()
    layout = plan_partial(index, selection_byte_ranges(index, TimeRange(0.5, 0.6)).ranges)

    moved = read_mp4_index(_reader(_partial_bytes(built, index, layout)))

    assert moved.video.offsets[0] == layout.size
    assert moved.video.offsets[8] == layout.size
    assert moved.audio.offsets[8] == layout.size


@pytest.mark.parametrize("large", [False, True], ids=["32bit", "64bit"])
def test_patched_partial_file_shrinks_mdat_to_the_end_of_file(large):
    """고친 부분 파일의 mdat 상자는 부분 파일의 끝에서 끝나야 한다.

    합성 mp4(mdat 머리 8바이트 · 16바이트), 구간 0.5~0.6초
    -> mdat의 시작 + 적힌 크기 == 부분 파일의 크기, 머리의 길이는 그대로
    """
    built, index = _built(large_mdat_header=large)
    layout = plan_partial(index, selection_byte_ranges(index, TimeRange(0.5, 0.6)).ranges)

    partial = _partial_bytes(built, index, layout)

    size, kind = struct.unpack_from(">I4s", partial, built.mdat_offset)
    if large:
        assert size == 1
        size = struct.unpack_from(">Q", partial, built.mdat_offset + 8)[0]
    assert kind == b"mdat"
    assert built.mdat_offset + size == len(partial) == layout.size


def test_patch_head_keeps_everything_but_offsets_and_mdat_size():
    """patch_head는 청크 위치 표와 mdat 크기 말고는 한 바이트도 바꾸지 않아야 한다.

    합성 mp4, 구간 0.5~0.6초
    -> 고친 머리의 길이 == 원래 길이, 고친 머리로 해석한 프레임 시각·키프레임·샘플 크기가 원본과 같다
    """
    built, index = _built()
    layout = plan_partial(index, selection_byte_ranges(index, TimeRange(0.5, 0.6)).ranges)
    head = built.data[: layout.ranges[0][1] + 1]

    patched = patch_head(head, layout, index.moov_range)
    moved = read_mp4_index(_reader(patched))

    assert len(patched) == len(head)
    assert moved.frame_pts == index.frame_pts
    assert moved.keyframes == index.keyframes
    assert moved.video.sizes == index.video.sizes
    assert moved.audio.times == index.audio.times


def test_patch_head_rejects_head_that_does_not_hold_the_moov():
    """patch_head는 머리에 moov가 다 들어 있지 않으면 손상 키로 거부해야 한다.

    합성 mp4의 머리를 moov 중간에서 자른 bytes
    -> Mp4Error(MP4_INVALID)
    """
    built, index = _built()
    layout = plan_partial(index, selection_byte_ranges(index, TimeRange(0.5, 0.6)).ranges)
    cut_short = built.data[: index.moov_range[1] - 10]

    with pytest.raises(Mp4Error) as info:
        patch_head(cut_short, layout, index.moov_range)

    assert info.value.message_key == MP4_INVALID
