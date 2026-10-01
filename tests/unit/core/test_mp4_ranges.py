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

from core.api.mp4 import parse_moov
from core.models.plan import TimeRange
from core.utils.hybrid_cut import SOURCE_LEAD_SECONDS
from core.utils.mp4_ranges import selection_byte_ranges
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
