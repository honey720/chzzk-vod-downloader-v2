"""구간 → 받을 바이트 범위(core/utils/mp4_ranges.py) 단위 테스트 (#178).

핵심 계약:
- 영상은 구간 첫 프레임 앞의 키프레임부터 끝 프레임을 디코드하는 데 필요한 샘플까지다
- 오디오는 그 키프레임의 시각부터 끝 프레임이 끝날 때까지와 겹치는 샘플이다
- 결과는 연속 범위 하나이고 total_size는 그 바이트 수다

입력은 합성 mp4다 — 영상 10fps 12프레임(키프레임 0·4·8, 디코드 순서 I P B B),
오디오는 0.128초짜리 샘플이며 샘플 n의 표시 시각은 (n − 1) × 0.128초다.
"""

from core.api.mp4 import parse_moov
from core.models.plan import TimeRange
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
    """selection_byte_ranges는 구간 첫 프레임 앞의 키프레임 샘플부터 범위를 잡아야 한다.

    영상만, 구간 0.5~0.6초 (프레임 5~6, 앞 키프레임 = 프레임 4 = 샘플 4)
    -> 범위 시작 == 샘플 4의 위치, keyframe == 4, first_frame == 5
    """
    built, index = _video_only()

    result = selection_byte_ranges(index, TimeRange(0.5, 0.6))

    assert result.ranges[0][0] == built.sample_offsets[b"vide"][4]
    assert (result.keyframe, result.first_frame, result.last_frame) == (4, 5, 6)


def test_selection_byte_ranges_ends_at_last_sample_needed_in_decode_order():
    """selection_byte_ranges는 끝 프레임보다 먼저 표시되지만 나중에 디코드되는 샘플까지 범위에 넣어야 한다.

    영상만, 구간 0.0~0.3초 (프레임 0~3 = 샘플 0, 2, 3, 1 — 끝 프레임 3은 샘플 1)
    -> 범위 끝 == 샘플 3의 마지막 바이트
    """
    built, index = _video_only()
    offsets = built.sample_offsets[b"vide"]

    result = selection_byte_ranges(index, TimeRange(0.0, 0.3))

    assert result.ranges == ((offsets[0], offsets[3] + VIDEO_SIZES[3] - 1),)


def test_selection_byte_ranges_total_size_equals_needed_samples_for_video_only():
    """selection_byte_ranges의 total_size는 영상만 있는 파일에서 필요한 샘플 크기의 합이어야 한다.

    영상만, 구간 0.5~0.6초 → 샘플 4~7
    -> total_size == 샘플 4~7 크기의 합, 범위 하나
    """
    _built, index = _video_only()

    result = selection_byte_ranges(index, TimeRange(0.5, 0.6))

    assert len(result.ranges) == 1
    assert result.total_size == sum(VIDEO_SIZES[4:8])
    assert result.ranges[0][1] - result.ranges[0][0] + 1 == result.total_size


def test_selection_byte_ranges_includes_last_frame_when_end_is_duration():
    """selection_byte_ranges는 구간 끝이 영상 길이이면 마지막 프레임까지 범위에 넣어야 한다.

    영상만, 구간 1.0~1.2초 (프레임 10~11, 앞 키프레임 = 프레임 8 = 샘플 8)
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


def test_selection_byte_ranges_covers_audio_of_the_same_time_span():
    """selection_byte_ranges는 키프레임 시각부터 끝 프레임이 끝날 때까지와 겹치는 오디오 샘플을 범위에 넣어야 한다.

    구간 0.5~0.6초 → 영상 샘플 4~7, 시각 범위 0.4~0.7초 → 오디오 샘플 4~6 (0.384~0.768초)
    -> 범위 == 그 샘플들의 가장 앞 바이트 ~ 가장 뒤 바이트
    """
    built, index = _with_audio()
    video = built.sample_offsets[b"vide"]
    audio = built.sample_offsets[b"soun"]
    begin = min(video[4], audio[4])
    end = max(video[7] + VIDEO_SIZES[7], audio[6] + AUDIO_SIZE)

    result = selection_byte_ranges(index, TimeRange(0.5, 0.6))

    assert result.ranges == ((begin, end - 1),)
    assert result.total_size == end - begin


def test_selection_byte_ranges_holds_every_needed_sample_inside_the_range():
    """selection_byte_ranges가 돌려준 범위만 읽어도 필요한 샘플의 바이트가 모두 들어 있어야 한다.

    구간 0.5~0.6초 → 영상 샘플 4~7, 오디오 샘플 4~6
    -> 범위로 자른 bytes에서 각 샘플 위치를 읽으면 조립기가 쓴 바이트
    """
    built, index = _with_audio()

    result = selection_byte_ranges(index, TimeRange(0.5, 0.6))
    begin, end = result.ranges[0]
    piece = built.data[begin : end + 1]

    for handler, samples, track in (
        (b"vide", range(4, 8), index.video),
        (b"soun", range(4, 7), index.audio),
    ):
        for sample in samples:
            start = track.offsets[sample] - begin
            size = track.sizes[sample]
            assert start >= 0
            assert piece[start : start + size] == built.sample_bytes(handler, sample, size)
