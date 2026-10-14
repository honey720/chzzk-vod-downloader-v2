"""받은 범위만 이어 붙인 부분 mp4(core/utils/mp4_partial.py) 단위 테스트 (#309).

핵심 계약:
- 받을 범위는 주어진 범위뿐이고(머리는 받지 않는다), 겹치는 범위는 합친다
- 부분 파일의 크기는 머리의 길이(원본에서 첫 샘플이 놓인 위치) + 받을 바이트의 합이다
- 머리는 이미 받은 moov 바이트로 만든다. 그 머리로 부분 파일을 다시 해석하면, 받은
  샘플은 부분 파일 안의 제 바이트를 가리키고 받지 않은 샘플은 파일의 끝을 가리킨다

입력은 합성 mp4다(tests/unit/core/mp4_builder.py). 부분 파일은 테스트가 원본 bytes에서
범위를 잘라 머리 뒤에 이어 붙여 만든다 — 다운로더를 거치지 않는다.
"""

import struct

import pytest

import core.api.mp4 as mp4_module
import core.utils.mp4_partial as partial_module
from core.api.mp4 import MP4_INVALID, MP4_UNSUPPORTED, Mp4Error, parse_moov, read_mp4_head
from core.models.plan import TimeRange
from core.utils.mp4_partial import build_head, merge_ranges, plan_partial
from core.utils.mp4_ranges import selection_byte_ranges
from tests.unit.core.mp4_builder import audio_spec, build_mp4, video_spec


def _reader(data: bytes):
    def read(offset: int, size: int) -> bytes:
        return data[offset : offset + size]

    return read


def _built(**options):
    """(조립 결과, Mp4Head)."""
    built = build_mp4([video_spec(), audio_spec()], **options)
    return built, read_mp4_head(_reader(built.data))


def _partial_bytes(built, head, layout) -> bytes:
    """만든 머리 뒤에 원본에서 자른 받을 범위를 이어 붙인 부분 파일."""
    pieces = [build_head(head.data, layout, head.index.moov_range)]
    pieces += [built.data[first : last + 1] for first, last in layout.ranges]
    return b"".join(pieces)


def _index_of(data: bytes):
    return read_mp4_head(_reader(data)).index


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


def test_plan_partial_downloads_only_the_given_ranges():
    """plan_partial은 주어진 범위만 받을 범위로 두고 머리는 받을 범위에 넣지 않아야 한다.

    합성 mp4(moov가 앞), 구간 범위 하나
    -> ranges == 그 범위, head_size == 첫 샘플의 위치, 어느 범위도 moov와 겹치지 않는다
    """
    built, head = _built()
    picked = selection_byte_ranges(head.index, TimeRange(1.0, 1.2))

    layout = plan_partial(head.index, picked.ranges)

    assert layout.ranges == picked.ranges
    assert layout.head_size == built.mdat_body[0]
    assert all(first > head.index.moov_range[1] for first, _last in layout.ranges)


def test_plan_partial_size_is_head_plus_ranges():
    """plan_partial의 size는 머리의 길이 + 받을 바이트의 합이고, 범위는 머리 뒤에 빈틈없이 놓여야 한다.

    합성 mp4, 구간 범위 하나
    -> size == 머리 + 구간 범위, download_size == 구간 범위, starts == (머리의 길이,)
    """
    built, head = _built()
    picked = selection_byte_ranges(head.index, TimeRange(1.0, 1.2))

    layout = plan_partial(head.index, picked.ranges)

    head_size = built.mdat_body[0]
    assert layout.size == head_size + picked.total_size
    assert layout.download_size == picked.total_size
    assert layout.starts == (head_size,)
    assert layout.size < len(built.data)


def test_plan_partial_counts_overlapping_ranges_once():
    """plan_partial은 겹치는 범위를 합쳐 같은 바이트를 한 번만 세야 한다.

    합성 mp4, 서로 겹치는 구간 0.5~0.6초와 0.5~0.9초
    -> size == 뒤 구간 하나만 줬을 때의 size
    """
    _built_file, head = _built()
    narrow = selection_byte_ranges(head.index, TimeRange(0.5, 0.6)).ranges
    wide = selection_byte_ranges(head.index, TimeRange(0.5, 0.9)).ranges

    assert plan_partial(head.index, [*narrow, *wide]).size == plan_partial(head.index, wide).size


def test_layout_position_maps_received_offsets_and_rejects_the_rest():
    """PartialLayout.position은 받는 자리는 부분 파일 안의 위치로, 받지 않는 자리는 None으로 답해야 한다.

    합성 mp4, 구간 1.0~1.2초의 범위 (first, last)
    -> position(first) == 머리의 길이, position(last) == size − 1, 그 밖(머리 포함)은 None
    """
    built, head = _built()
    first, last = selection_byte_ranges(head.index, TimeRange(1.0, 1.2)).ranges[0]

    layout = plan_partial(head.index, [(first, last)])

    assert layout.position(first) == built.mdat_body[0]
    assert layout.position(last) == layout.size - 1
    assert layout.position(0) is None
    assert layout.position(first - 1) is None
    assert layout.position(last + 1) is None


def test_plan_partial_rejects_moov_behind_the_samples():
    """plan_partial은 moov가 샘플보다 뒤에 있으면 미지원 키로 거부해야 한다.

    합성 mp4(ftyp · mdat · moov)
    -> Mp4Error(MP4_UNSUPPORTED)
    """
    _built_file, head = _built(moov_first=False)

    with pytest.raises(Mp4Error) as info:
        plan_partial(head.index, [(0, 10)])

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


# ================================================================ 머리 만들기


@pytest.mark.parametrize("co64", [False, True], ids=["stco", "co64"])
def test_built_partial_file_points_received_samples_at_their_bytes(co64):
    """만든 부분 파일을 다시 해석하면 받은 샘플의 위치에 조립기가 쓴 그 샘플의 바이트가 있어야 한다.

    합성 mp4(stco · co64), 구간 0.5~0.6초 → 영상 샘플 3~7 · 오디오 샘플 0~7을 받는다
    -> 부분 파일의 색인에서 그 샘플들의 위치를 읽으면 원본의 그 샘플 바이트
    """
    built, head = _built(co64=co64)
    layout = plan_partial(head.index, selection_byte_ranges(head.index, TimeRange(0.5, 0.6)).ranges)

    partial = _partial_bytes(built, head, layout)
    moved = _index_of(partial)

    assert len(partial) == layout.size
    for handler, samples, track in (
        (b"vide", range(3, 8), moved.video),
        (b"soun", range(0, 8), moved.audio),
    ):
        for sample in samples:
            start, size = track.offsets[sample], track.sizes[sample]
            assert partial[start : start + size] == built.sample_bytes(handler, sample, size)


def test_built_partial_file_points_missing_chunks_at_end_of_file():
    """만든 부분 파일에서 받지 않은 청크의 위치는 부분 파일의 끝이어야 한다.

    합성 mp4, 구간 0.5~0.6초 → 영상 청크 (0~2) · (8~11)은 받지 않는다
    -> 부분 파일의 색인에서 영상 샘플 0과 8의 위치 == 부분 파일의 크기
    """
    built, head = _built()
    layout = plan_partial(head.index, selection_byte_ranges(head.index, TimeRange(0.5, 0.6)).ranges)

    moved = _index_of(_partial_bytes(built, head, layout))

    assert moved.video.offsets[0] == layout.size
    assert moved.video.offsets[8] == layout.size
    assert moved.audio.offsets[8] == layout.size


@pytest.mark.parametrize("large", [False, True], ids=["8byte-gap", "16byte-gap"])
def test_built_head_writes_mdat_that_ends_at_the_end_of_file(large):
    """만든 머리는 moov 바로 뒤에 mdat 머리를 쓰고, 그 mdat는 부분 파일의 끝에서 끝나야 한다.

    합성 mp4(원본의 mdat 머리 8바이트 · 16바이트), 구간 0.5~0.6초
    -> 머리의 길이 == 첫 샘플의 위치, moov 끝의 상자가 mdat이고 시작 + 크기 == 부분 파일의 크기,
       남는 자리는 0
    """
    built, head = _built(large_mdat_header=large)
    layout = plan_partial(head.index, selection_byte_ranges(head.index, TimeRange(0.5, 0.6)).ranges)

    made = build_head(head.data, layout, head.index.moov_range)

    moov_end = head.index.moov_range[1] + 1
    size, kind = struct.unpack_from(">I4s", made, moov_end)
    assert len(made) == layout.head_size == built.mdat_body[0]
    assert kind == b"mdat"
    assert moov_end + size == layout.size
    assert made[moov_end + 8 :] == bytes(layout.head_size - moov_end - 8)


def test_built_head_marks_mdat_as_running_to_end_of_file_when_too_large(monkeypatch):
    """만든 머리는 mdat의 크기가 32비트 칸에 담기지 않으면 크기를 0("파일 끝까지")으로 적어야 한다.

    32비트 상한을 100바이트로 줄임, 구간 0.5~0.6초 (mdat가 100바이트보다 크다)
    -> mdat의 크기 칸 == 0
    """
    monkeypatch.setattr(partial_module, "_MAX_BOX_SIZE_32", 100)
    _built_file, head = _built()
    layout = plan_partial(head.index, selection_byte_ranges(head.index, TimeRange(0.5, 0.6)).ranges)

    made = build_head(head.data, layout, head.index.moov_range)

    size, kind = struct.unpack_from(">I4s", made, head.index.moov_range[1] + 1)
    assert (size, kind) == (0, b"mdat")


def test_built_head_keeps_everything_but_chunk_offsets():
    """build_head는 0부터 moov 끝까지에서 청크 위치 표 말고는 바꾸지 않아야 한다.

    합성 mp4, 구간 0.5~0.6초
    -> 만든 머리로 해석한 프레임 시각·키프레임·샘플 크기·오디오 시각이 원본과 같고, moov의 위치도 같다
    """
    built, head = _built()
    layout = plan_partial(head.index, selection_byte_ranges(head.index, TimeRange(0.5, 0.6)).ranges)

    moved = _index_of(build_head(head.data, layout, head.index.moov_range))

    assert moved.moov_range == head.index.moov_range
    assert moved.frame_pts == head.index.frame_pts
    assert moved.keyframes == head.index.keyframes
    assert moved.video.sizes == head.index.video.sizes
    assert moved.audio.times == head.index.audio.times


def test_build_head_rejects_data_that_does_not_hold_the_moov():
    """build_head는 받은 바이트에 moov가 다 들어 있지 않으면 손상 키로 거부해야 한다.

    합성 mp4의 앞부분을 moov 중간에서 자른 bytes
    -> Mp4Error(MP4_INVALID)
    """
    _built_file, head = _built()
    layout = plan_partial(head.index, selection_byte_ranges(head.index, TimeRange(0.5, 0.6)).ranges)

    with pytest.raises(Mp4Error) as info:
        build_head(head.data[:-10], layout, head.index.moov_range)

    assert info.value.message_key == MP4_INVALID


# ================================================================ 받은 앞부분 (Mp4Head)


def test_read_mp4_head_keeps_the_bytes_up_to_the_end_of_moov():
    """read_mp4_head는 moov가 파일 앞에 있으면 0부터 moov 끝까지의 바이트를 돌려줘야 한다.

    ftyp · moov · mdat
    -> data == 원본의 [0, moov 끝], index.moov_range == moov의 위치
    """
    built, head = _built()

    moov_end = built.moov_offset + len(built.moov)

    assert head.data == built.data[:moov_end]
    assert head.index.moov_range == (built.moov_offset, moov_end - 1)


def test_read_mp4_head_reads_a_moov_larger_than_the_first_read(monkeypatch):
    """read_mp4_head는 moov가 첫 읽기보다 길어 두 번에 나눠 받아도 0부터 moov 끝까지를 이어 돌려줘야 한다.

    첫 읽기를 64바이트로 줄임(moov는 그보다 길다)
    -> 읽기 2회, data == 원본의 [0, moov 끝]
    """
    monkeypatch.setattr(mp4_module, "_FIRST_READ_BYTES", 64)
    built = build_mp4([video_spec(), audio_spec()])
    log = []

    def read(offset: int, size: int) -> bytes:
        log.append((offset, size))
        return built.data[offset : offset + size]

    head = read_mp4_head(read)

    assert len(log) == 2
    assert head.data == built.data[: built.moov_offset + len(built.moov)]


def test_read_mp4_head_has_no_bytes_when_moov_is_not_at_the_front(monkeypatch):
    """read_mp4_head는 moov가 mdat 뒤에 있으면 앞부분의 바이트 없이 색인만 돌려줘야 한다.

    ftyp · mdat · moov, 읽기 단위를 24바이트로 줄여 moov를 뒤 읽기에서 찾게 함
    -> data is None, 프레임 12개
    """
    monkeypatch.setattr(mp4_module, "_FIRST_READ_BYTES", 24)
    monkeypatch.setattr(mp4_module, "_HEADER_READ_BYTES", 24)
    built = build_mp4([video_spec(), audio_spec()], moov_first=False)

    head = read_mp4_head(_reader(built.data))

    assert head.data is None
    assert len(head.index.frame_pts) == 12
