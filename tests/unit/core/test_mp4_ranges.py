"""구간 → 받을 바이트 범위(core/utils/mp4_ranges.py) 단위 테스트 (#178, #309).

핵심 계약:
- 범위는 구간 첫 프레임보다 SOURCE_LEAD_SECONDS 앞의 시각을 덮는 키프레임에서 시작한다
- 영상은 그 키프레임부터 끝 프레임을 디코드하는 데 필요한 샘플까지다
- 오디오는 그 키프레임의 DTS부터 끝 프레임이 끝날 때까지와 겹치는 샘플이다
- 양 끝은 청크 단위로 넓힌다
- 결과는 연속 범위 하나이고 total_size는 그 바이트 수다

입력은 합성 mp4다 — 영상 10fps 12프레임(키프레임 0·4·8, 디코드 순서 I P B B, DTS는
PTS보다 0.1초 앞선다), 영상 청크는 샘플 0~2 · 3~5 · 6~7 · 8~11. 오디오는 0.128초짜리
샘플이며 샘플 n의 표시 시각은 (n − 1) × 0.128초, 청크는 샘플 4개씩이다.
"""

import pytest

from core.api.mp4 import index_mp4, parse_moov, read_mp4_raw
from core.models.plan import TimeRange
from core.utils.hybrid_cut import SOURCE_LEAD_SECONDS
from core.utils.mp4_ranges import (
    estimated_section_file_size,
    sections_download_size,
    sections_file_size,
    sections_head_size,
    selection_byte_ranges,
)
from core.utils.selections import reaches_end, validate_selections
from tests.unit.core.mp4_builder import audio_spec, build_mp4, video_spec

VIDEO_SIZES = [40 + 3 * n for n in range(12)]  # 조립기 video_spec의 샘플 크기
AUDIO_SIZE = 7  # 조립기 audio_spec의 샘플 크기


def _video_only():
    built = build_mp4([video_spec()])
    return built, parse_moov(built.moov)


def _with_audio():
    built = build_mp4([video_spec(), audio_spec()])
    return built, parse_moov(built.moov)


def test_selection_byte_ranges_starts_at_keyframe_before_first_frame():
    """selection_byte_ranges는 구간 첫 프레임 앞의 키프레임이 든 청크부터 범위를 잡아야 한다.

    영상만, 구간 0.5~0.6초 (프레임 5~6, 앞 키프레임 = 프레임 4 = 샘플 4, 그 청크는 샘플 3~5)
    -> 범위 시작 == 샘플 3의 위치, keyframe == 4, first_frame == 5
    """
    built, index = _video_only()

    result = selection_byte_ranges(index, TimeRange(0.5, 0.6))

    assert result.ranges[0][0] == built.sample_offsets[b"vide"][3]
    assert (result.keyframe, result.first_frame, result.last_frame) == (4, 5, 6)


def test_selection_byte_ranges_starts_one_keyframe_earlier_when_first_frame_is_a_keyframe():
    """selection_byte_ranges는 구간 첫 프레임이 키프레임이면 그 앞 키프레임부터 범위를 잡아야 한다.

    영상만, 구간 0.4~0.6초 (첫 프레임 4는 키프레임. 0.4 − SOURCE_LEAD_SECONDS를 덮는 키프레임은 프레임 0)
    -> keyframe == 0, 범위 시작 == 샘플 0의 위치
    """
    built, index = _video_only()

    result = selection_byte_ranges(index, TimeRange(0.4, 0.6))

    assert 0.0 < SOURCE_LEAD_SECONDS < 0.1  # 한 프레임(0.1초)보다 짧아야 이 입력이 성립한다
    assert (result.keyframe, result.first_frame) == (0, 4)
    assert result.ranges[0][0] == built.sample_offsets[b"vide"][0]


def test_selection_byte_ranges_starts_at_first_keyframe_when_nothing_precedes_it():
    """selection_byte_ranges는 구간이 파일의 첫 프레임에서 시작하면 첫 키프레임부터 범위를 잡아야 한다.

    영상만, 구간 0.0~0.1초
    -> keyframe == 0, first_frame == 0
    """
    _built, index = _video_only()

    result = selection_byte_ranges(index, TimeRange(0.0, 0.1))

    assert (result.keyframe, result.first_frame) == (0, 0)


def test_selection_byte_ranges_ends_at_last_sample_needed_in_decode_order():
    """selection_byte_ranges는 끝 프레임보다 먼저 표시되지만 나중에 디코드되는 샘플이 든 청크까지 범위에 넣어야 한다.

    영상만, 구간 0.0~0.3초 (프레임 0~3 = 샘플 0, 2, 3, 1 — 가장 뒤 샘플 3의 청크는 샘플 3~5)
    -> 범위 == (샘플 0의 위치, 샘플 5의 마지막 바이트)
    """
    built, index = _video_only()
    offsets = built.sample_offsets[b"vide"]

    result = selection_byte_ranges(index, TimeRange(0.0, 0.3))

    assert result.ranges == ((offsets[0], offsets[5] + VIDEO_SIZES[5] - 1),)


def test_selection_byte_ranges_total_size_equals_whole_chunks_for_video_only():
    """selection_byte_ranges의 total_size는 영상만 있는 파일에서 필요한 샘플이 든 청크 크기의 합이어야 한다.

    영상만, 구간 0.5~0.6초 → 필요한 샘플 4~7 → 청크 (3~5) · (6~7)
    -> total_size == 샘플 3~7 크기의 합, 범위 하나
    """
    _built, index = _video_only()

    result = selection_byte_ranges(index, TimeRange(0.5, 0.6))

    assert len(result.ranges) == 1
    assert result.total_size == sum(VIDEO_SIZES[3:8])
    assert result.ranges[0][1] - result.ranges[0][0] + 1 == result.total_size


def test_selection_byte_ranges_includes_last_frame_when_end_is_duration():
    """selection_byte_ranges는 구간 끝이 영상 길이이면 마지막 프레임까지 범위에 넣어야 한다.

    영상만, 구간 1.0~1.2초 (프레임 10~11, 앞 키프레임 = 프레임 8 = 샘플 8, 청크는 샘플 8~11)
    -> 범위 == (샘플 8의 위치, 샘플 11의 마지막 바이트), last_frame == 11
    """
    built, index = _video_only()
    offsets = built.sample_offsets[b"vide"]

    result = selection_byte_ranges(index, TimeRange(1.0, 1.2))

    assert result.ranges == ((offsets[8], offsets[11] + VIDEO_SIZES[11] - 1),)
    assert result.last_frame == 11


def test_selection_byte_ranges_snaps_times_to_frames():
    """selection_byte_ranges는 프레임 경계가 아닌 시각을 가장 가까운 프레임에 맞춰야 한다.

    영상만, 구간 0.52~0.58초
    -> 0.5~0.6초 구간과 같은 결과
    """
    _built, index = _video_only()

    result = selection_byte_ranges(index, TimeRange(0.52, 0.58))

    assert result == selection_byte_ranges(index, TimeRange(0.5, 0.6))


def test_selection_byte_ranges_covers_audio_from_the_keyframe_decode_time():
    """selection_byte_ranges는 키프레임의 DTS부터 끝 프레임이 끝날 때까지와 겹치는 오디오 샘플의 청크를 범위에 넣어야 한다.

    구간 0.5~0.6초 → 키프레임 4의 DTS 0.3초 ~ 0.7초 → 오디오 샘플 3~6 (0.256~0.768초)
    샘플 3은 키프레임의 PTS(0.4초)보다 앞에서 끝난다 — DTS로 재야 범위에 든다. 청크는 (0~3) · (4~7)
    -> 범위 == 영상 샘플 3~7과 오디오 샘플 0~7의 가장 앞 바이트 ~ 가장 뒤 바이트
    """
    built, index = _with_audio()
    video = built.sample_offsets[b"vide"]
    audio = built.sample_offsets[b"soun"]
    begin = min(video[3], audio[0])
    end = max(video[7] + VIDEO_SIZES[7], audio[7] + AUDIO_SIZE)

    result = selection_byte_ranges(index, TimeRange(0.5, 0.6))

    assert result.ranges == ((begin, end - 1),)
    assert result.total_size == end - begin


def test_selection_byte_ranges_widens_to_audio_stored_before_the_video():
    """selection_byte_ranges는 필요한 오디오 청크가 영상 청크보다 앞에 놓여 있으면 범위를 거기까지 넓혀야 한다.

    구간 1.0~1.2초 → 영상 샘플 8~11(마지막 영상 청크), 키프레임 8의 DTS 0.7초 ~ 1.2초 → 오디오 샘플 6~10
    → 오디오 청크 (4~7) · (8~11). 오디오 샘플 4는 영상 샘플 8보다 두 청크 앞에 놓인다
    -> 범위 시작 == 오디오 샘플 4의 위치 (영상 샘플 8의 위치보다 앞)
    """
    built, index = _with_audio()
    video = built.sample_offsets[b"vide"]
    audio = built.sample_offsets[b"soun"]

    result = selection_byte_ranges(index, TimeRange(1.0, 1.2))

    assert audio[4] < video[8]  # 조립기 배치의 전제
    assert result.ranges == ((audio[4], video[11] + VIDEO_SIZES[11] - 1),)


def test_selection_byte_ranges_ends_on_chunk_boundaries_of_every_track():
    """selection_byte_ranges가 돌려준 범위의 양 끝은 어느 트랙의 청크도 가르지 않아야 한다.

    구간 0.5~0.6초, 영상 청크 (0~2)(3~5)(6~7)(8~11) · 오디오 청크 4개씩
    -> 모든 청크가 범위 안에 통째로 들거나 통째로 밖에 있다
    """
    _built, index = _with_audio()

    result = selection_byte_ranges(index, TimeRange(0.5, 0.6))
    begin, end = result.ranges[0]

    for track in (index.video, index.audio):
        bounds = [*track.chunk_starts, len(track.offsets)]
        for first, after in zip(bounds, bounds[1:]):
            chunk_begin = track.offsets[first]
            chunk_end = track.offsets[after - 1] + track.sizes[after - 1] - 1
            inside = begin <= chunk_begin and chunk_end <= end
            outside = chunk_end < begin or chunk_begin > end
            assert inside or outside


def test_selection_byte_ranges_holds_every_needed_sample_inside_the_range():
    """selection_byte_ranges가 돌려준 범위만 읽어도 필요한 샘플의 바이트가 모두 들어 있어야 한다.

    구간 0.5~0.6초 → 영상 샘플 4~7, 오디오 샘플 3~6
    -> 범위로 자른 bytes에서 각 샘플 위치를 읽으면 조립기가 쓴 바이트
    """
    built, index = _with_audio()

    result = selection_byte_ranges(index, TimeRange(0.5, 0.6))
    begin, end = result.ranges[0]
    piece = built.data[begin : end + 1]

    for handler, samples, track in (
        (b"vide", range(4, 8), index.video),
        (b"soun", range(3, 7), index.audio),
    ):
        for sample in samples:
            start = track.offsets[sample] - begin
            size = track.sizes[sample]
            assert start >= 0
            assert piece[start : start + size] == built.sample_bytes(handler, sample, size)


def test_selection_ending_at_a_duration_off_the_frame_grid_ends_on_the_last_frame():
    """끝이 영상 길이(초)와 같은 mp4 구간은 길이 × fps의 소수부가 .5 이상이어도 검증을 통과하고 마지막 프레임에서 끝나야 한다.

    영상만 · 재정렬 없음, 12샘플(길이 100틱, 마지막만 160틱 · timescale 1000) — 길이 1.26초 = 12.6프레임.
    구간 0.5 ~ 길이
    -> 위반 없음, reaches_end 참, last_frame == 11
    """
    built = build_mp4([video_spec(deltas=[100] * 11 + [160], composition=None, edits=None)])
    index = parse_moov(built.moov)
    selection = TimeRange(0.5, index.duration)

    assert (index.duration, index.fps) == (pytest.approx(1.26), 10)
    assert validate_selections([selection], index.duration, index.fps) == {}
    assert reaches_end(selection.end, index.duration, index.fps)
    assert selection_byte_ranges(index, selection).last_frame == 11


def _file_index(data: bytes):
    """파일의 바이트에서 읽은 색인 — moov의 위치가 들어 있다."""
    return index_mp4(read_mp4_raw(lambda offset, size: data[offset : offset + size])).index


def test_head_plus_the_download_of_sections_covering_the_video_is_the_size_of_the_file():
    """구간이 영상 전체를 덮으면 머리 길이와 받을 바이트의 합이 파일 크기와 같아야 한다.

    영상 1.2초 · 오디오 10샘플(영상 안에서 끝난다 — 1.152초). 파일: ftyp · moov · mdat
    구간 0~길이 하나 / 겹치는 둘(0~0.6, 0.3~길이)
    -> sections_head_size == mdat 본문이 시작하는 자리, 머리 + sections_download_size == 파일 크기
    """
    audio = audio_spec(deltas=[1024] * 10, sizes=[7] * 10, chunks=[4, 4, 2], edits=[(1152, 1024)])
    built = build_mp4([video_spec(), audio])
    index = _file_index(built.data)
    whole = [TimeRange(0.0, index.duration)]
    overlapping = [TimeRange(0.0, 0.6), TimeRange(0.3, index.duration)]

    head = sections_head_size(index)

    assert head == built.mdat_body[0]
    assert head + sections_download_size(index, whole) == len(built.data)
    assert head + sections_download_size(index, overlapping) == len(built.data)


def test_head_is_left_out_of_the_download_of_a_part_of_the_video():
    """영상의 일부만 받는 구간은 머리와 받을 바이트의 합이 파일 크기보다 작아야 한다.

    위와 같은 파일, 구간 0~0.4초 -> 머리 == mdat 본문의 시작, 머리 + 받을 바이트 < 파일 크기
    """
    audio = audio_spec(deltas=[1024] * 10, sizes=[7] * 10, chunks=[4, 4, 2], edits=[(1152, 1024)])
    built = build_mp4([video_spec(), audio])
    index = _file_index(built.data)

    part = sections_download_size(index, [TimeRange(0.0, 0.4)])

    assert sections_head_size(index) == built.mdat_body[0]
    assert 0 < part and sections_head_size(index) + part < len(built.data)


# ================================================================ 구간 파일 크기의 합(추정) (#309)
#
# 합성 mp4의 프레임 → 샘플: [0, 2, 3, 1, 4, 6, 7, 5, 8, 10, 11, 9] (디코드 순서 I P B B).
# 영상 샘플 n의 크기는 40 + 3n, 오디오 샘플은 7바이트이고 샘플 n의 표시 시각은 (n − 1) × 0.128초다.
# 영상의 청크는 샘플 3 · 3 · 2 · 4개씩(첫 샘플 0 · 3 · 6 · 8), 오디오의 청크는 4개씩이다.
# 머리의 어림: 2400 + 프레임 × 12 + 오디오 샘플 × 4 + (영상 청크 + 오디오 청크) × 8


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ((2, 119, 3, 21, 1, 1), 2400 + 24 + 12 + 16 + 119 + 21),
        ((5, 1000, 0, 0, 2, 0), 2400 + 60 + 16 + 1000),  # 오디오가 없다 — 오디오의 표도 없다
        ((3, 300, 2, 14, 3, 2), 2400 + 36 + 8 + 40 + 300 + 14),
        # 4GiB를 넘어도 식이 같다 — 청크 위치는 늘 64비트로 쓴다
        ((10, 1 << 32, 4, 0, 10, 4), 2400 + 120 + 16 + 112 + (1 << 32)),
    ],
    ids=["영상과 오디오", "영상만", "청크가 여럿", "4GiB를 넘는다"],
)
def test_estimated_section_file_size_adds_the_samples_and_a_head_by_their_counts(counts, expected):
    """구간 파일 하나의 추정은 샘플의 바이트에 샘플 · 청크 수로 어림한 머리를 더한 값이어야 한다.

    (프레임 수, 영상 바이트, 오디오 샘플 수, 오디오 바이트, 영상 청크 수, 오디오 청크 수)
    -> 2400 + 프레임 × 12 + 오디오 샘플 × 4 + (영상 청크 + 오디오 청크) × 8 + 영상 바이트 + 오디오 바이트
    """
    assert estimated_section_file_size(*counts) == expected


@pytest.mark.parametrize(
    ("selection", "expected"),
    [
        # 프레임 5~6 = 샘플 6 · 7(58 + 61 = 119바이트, 청크 하나). 오디오는 0.5~0.7초와 겹치는
        # 샘플 4 · 5 · 6(청크 하나)
        (TimeRange(0.5, 0.6), 2400 + 24 + 12 + 16 + 119 + 21),
        # 프레임 3~4 = 샘플 1 · 4. 디코드 순서로 그 사이의 샘플 2 · 3도 센다(43 + 46 + 49 + 52 = 190,
        # 청크 둘). 오디오는 0.3~0.5초와 겹치는 샘플 3 · 4(청크 둘)
        (TimeRange(0.3, 0.4), 2400 + 24 + 8 + 32 + 190 + 14),
    ],
    ids=["샘플이 이어진 구간", "디코드 순서가 엇갈린 구간"],
)
def test_sections_file_size_counts_the_samples_of_the_section_and_its_head(selection, expected):
    """구간 파일 크기의 합은 그 구간의 영상 · 오디오 샘플 바이트에 머리의 어림을 더한 값이어야 한다.

    영상 + 오디오, 구간 하나 -> expected (받을 양 sections_download_size와는 다른 값이다)
    """
    index = _file_index(build_mp4([video_spec(), audio_spec()]).data)

    assert sections_file_size(index, [selection]) == expected
    assert sections_download_size(index, [selection]) != expected


@pytest.mark.parametrize("with_audio", [True, False], ids=["영상과 오디오", "영상만"])
def test_sections_file_size_of_the_whole_video_is_the_size_of_the_source_file(with_audio):
    """영상 전체를 덮는 구간의 크기는 원본 파일의 크기여야 한다 — 받은 것이 곧 원본이다.

    구간 0초 ~ 영상 길이(1.2초, 프레임 0~11) -> 합성 파일의 바이트 수.
    마지막 프레임 하나가 빠진 구간(0~1.0초)은 그 값이 아니다(식으로 센다)
    """
    built = build_mp4([video_spec()] + ([audio_spec()] if with_audio else []))
    index = _file_index(built.data)

    assert sections_file_size(index, [TimeRange(0.0, 1.2)]) == len(built.data)
    assert sections_file_size(index, [TimeRange(0.0, 1.0)]) != len(built.data)


def test_sections_file_size_counts_overlapping_sections_once_each():
    """겹치는 구간은 구간마다 세어야 한다 — 구간마다 파일이 하나씩 생긴다.

    영상 + 오디오. 구간 0.5~0.6초 하나의 크기 2592. 같은 구간 둘, 그리고 0.5~0.6초와 0.3~0.4초(2668)
    -> 5184, 5260. 받을 양(sections_download_size)은 같은 구간 둘이어도 하나일 때와 같다
    """
    index = _file_index(build_mp4([video_spec(), audio_spec()]).data)
    one = TimeRange(0.5, 0.6)

    assert sections_file_size(index, [one]) == 2592
    assert sections_file_size(index, [one, one]) == 5184
    assert sections_file_size(index, [one, TimeRange(0.3, 0.4)]) == 2592 + 2668
    assert sections_download_size(index, [one, one]) == sections_download_size(index, [one])


def test_sections_file_size_of_a_video_without_audio_counts_no_audio():
    """오디오가 없는 영상의 구간 파일 크기에는 오디오의 몫이 없어야 한다.

    영상만, 구간 0.5~0.6초(샘플 6 · 7 — 119바이트, 청크 하나) -> 2400 + 24 + 8 + 119
    """
    index = _file_index(build_mp4([video_spec()]).data)

    assert sections_file_size(index, [TimeRange(0.5, 0.6)]) == 2400 + 24 + 8 + 119
