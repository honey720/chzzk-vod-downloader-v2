"""구간 → 세그먼트 범위와 프레임(core/utils/fmp4_sections.py) 단위 테스트 (#309).

ffmpeg로는 만들기 어려운 모양의 입력을 다룬다. 입력은 tests/unit/core/fmp4_builder.py가
상자를 직접 조립한 합성 fMP4다 — 영상 10fps(timescale 1000, 샘플 길이 100틱), 오디오
timescale 8000 · 샘플 길이 1024틱. 받기부터 구간 파일까지의 경로는 test_m3u8_sections.py가 본다.
"""

from fractions import Fraction

import pytest

from core.api.fmp4 import parse_init_segment, parse_media_segment
from core.api.hls import parse_media_playlist
from core.models.plan import TimeRange
from core.models.fmp4_index import Fmp4Init, Fmp4Segment
from core.utils.fmp4_sections import (
    FPS_DECLARED,
    FPS_MEASURED,
    FPS_STANDARD,
    choose_frame_rate,
    plan_fmp4_sections,
)
from core.utils.selections import (
    SELECTION_CROSSES_BREAK,
    SELECTION_TOO_SHORT,
    SelectionError,
)
from core.utils.timecode import TimecodeError, parse_timecode
from tests.unit.core.fmp4_builder import (
    KEY,
    NON_KEY,
    Fragment,
    InitTrack,
    Run,
    Sample,
    Traf,
    init_segment,
    media_segment,
)

VIDEO = 1  # 영상 트랙 번호
AUDIO = 2  # 오디오 트랙 번호


def _segment(number: int, *, video: bool = True, audio_samples: int = 8) -> bytes:
    """number번째 1초 세그먼트 — 영상 10프레임(첫 프레임이 키프레임)과 오디오."""
    trafs = []
    if video:
        frames = [
            Sample(duration=100, size=10, flags=KEY if n == 0 else NON_KEY) for n in range(10)
        ]
        trafs.append(Traf(VIDEO, [Run(frames)], decode_time=number * 1000))
    sound = [Sample(duration=1024, size=7) for _ in range(audio_samples)]
    trafs.append(Traf(AUDIO, [Run(sound)], decode_time=number * 8192))
    return media_segment([Fragment(trafs)])


def _playlist(durations: list[float]):
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", '#EXT-X-MAP:URI="init.mp4"']
    for number, duration in enumerate(durations):
        lines += [f"#EXTINF:{duration:.6f},", f"seg-{number}.m4s"]
    return parse_media_playlist("\n".join([*lines, "#EXT-X-ENDLIST"]))


def test_section_ending_at_the_video_length_skips_a_last_segment_without_video():
    """끝이 영상 길이와 같은 구간은 마지막 세그먼트에 영상이 없으면 그 앞 세그먼트의 마지막 프레임에서 끝나야 한다.

    세그먼트 셋 — 0 · 1은 영상 10프레임씩과 오디오, 2는 오디오뿐. #EXTINF 1.0 · 1.0 · 0.5 (길이 2.5초),
    구간 0.5 ~ 2.5초
    -> 끝 프레임의 PTS == 1.9초(둘째 세그먼트의 마지막 프레임), 받는 세그먼트 0~2
    """
    init = parse_init_segment(
        init_segment(
            [
                InitTrack(VIDEO, b"vide", 1000, trex=(100, 10, NON_KEY)),
                InitTrack(AUDIO, b"soun", 8000, codec=b"mp4a", trex=(1024, 7, KEY)),
            ]
        )
    )
    segments = [
        parse_media_segment(_segment(0), init),
        parse_media_segment(_segment(1), init),
        parse_media_segment(_segment(2, video=False, audio_samples=4), init),
    ]

    sections = plan_fmp4_sections(
        _playlist([1.0, 1.0, 0.5]), init, [TimeRange(0.5, 2.5)], segments.__getitem__
    )

    assert len(sections) == 1
    assert (sections[0].first_pts, sections[0].last_pts) == (0.5, 1.9)
    assert (sections[0].first_segment, sections[0].last_segment) == (0, 2)


def test_section_to_the_video_length_is_rejected_when_a_break_follows_its_last_segment():
    """끝이 영상 길이와 같은 구간은 그 뒤에 끊긴 자리와 세그먼트가 더 있으면 moof를 더 읽지 않고 거부해야 한다.

    세그먼트 0 · 1(1초씩) · 끊김 · 세그먼트 2(#EXTINF 0.02 — 한 프레임보다 짧다, 오디오뿐).
    길이 2.02초 = 10fps에서 20프레임, 구간 0.5 ~ 1.96초(끝이 프레임 20 — 둘째 세그먼트 안에서 끝난다)
    -> SelectionError(SELECTION_CROSSES_BREAK), 프레임 정보를 읽은 세그먼트는 0뿐
    """
    init = parse_init_segment(
        init_segment(
            [
                InitTrack(VIDEO, b"vide", 1000, trex=(100, 10, NON_KEY)),
                InitTrack(AUDIO, b"soun", 8000, codec=b"mp4a", trex=(1024, 7, KEY)),
            ]
        )
    )
    segments = [
        parse_media_segment(_segment(0), init),
        parse_media_segment(_segment(1), init),
        parse_media_segment(_segment(0, video=False, audio_samples=4), init),
    ]
    lines = ["#EXTM3U", '#EXT-X-MAP:URI="init.mp4"', "#EXTINF:1.000000,", "seg-0.m4s"]
    lines += ["#EXTINF:1.000000,", "seg-1.m4s", "#EXT-X-DISCONTINUITY", "#EXTINF:0.020000,"]
    playlist = parse_media_playlist("\n".join([*lines, "seg-2.m4s", "#EXT-X-ENDLIST"]))
    asked = []

    def segment_at(index: int):
        asked.append(index)
        return segments[index]

    with pytest.raises(SelectionError) as info:
        plan_fmp4_sections(playlist, init, [TimeRange(0.5, 1.96)], segment_at)

    assert info.value.message_key == SELECTION_CROSSES_BREAK
    assert asked == [0]


# ================================================================ 프레임률


def _init() -> Fmp4Init:
    return parse_init_segment(
        init_segment(
            [
                InitTrack(VIDEO, b"vide", 1000, trex=(100, 10, NON_KEY)),
                InitTrack(AUDIO, b"soun", 8000, codec=b"mp4a", trex=(1024, 7, KEY)),
            ]
        )
    )


def _video_segment(durations: list[int], timescale: int = 1000) -> tuple[Fmp4Init, Fmp4Segment]:
    """영상 샘플 길이가 durations(틱)인 세그먼트 하나와 그 초기화 세그먼트."""
    init = parse_init_segment(
        init_segment([InitTrack(VIDEO, b"vide", timescale, trex=(0, 10, NON_KEY))])
    )
    frames = [Sample(duration=d, size=10, flags=NON_KEY) for d in durations]
    return init, parse_media_segment(media_segment([Fragment([Traf(VIDEO, [Run(frames)])])]), init)


def test_choose_frame_rate_reads_sixty_from_millisecond_timestamps():
    """choose_frame_rate는 프레임 간격이 17 · 17 · 16ms로 도는 입력의 프레임률을 60으로 정해야 한다.

    timescale 1000, 영상 240프레임(4초), 샘플 길이 17 · 17 · 16틱의 반복. 선언값 없음
    -> (60, FPS_STANDARD) — 가장 많은 샘플 길이(17)로 정한 1000/17이 아니다
    """
    init, segment = _video_segment([17, 17, 16] * 80)

    choice = choose_frame_rate(init, [segment])

    assert (choice.rate, choice.source) == (Fraction(60), FPS_STANDARD)


def test_last_frame_number_of_a_second_stays_inside_that_second():
    """정한 프레임률로 읽은 타임코드 FF 59는 그 초 안의 시각이어야 한다.

    17 · 17 · 16ms 입력에서 정한 프레임률, 타임코드 00:00:01:59
    -> 1초 이상 2초 미만. 가장 많은 샘플 길이로 정한 1000/17(초당 59칸)로는 FF 59를 읽지 못한다
    """
    init, segment = _video_segment([17, 17, 16] * 80)

    rate = choose_frame_rate(init, [segment]).rate

    assert 1.0 <= parse_timecode("00:00:01:59", rate) < 2.0
    with pytest.raises(TimecodeError):  # 고치기 전의 값 — FF는 58까지였다
        parse_timecode("00:00:01:59", Fraction(1000, 17))


def test_choose_frame_rate_prefers_the_declared_value():
    """choose_frame_rate는 선언된 프레임률이 있으면 프레임을 재지 않고 그 값을 그대로 써야 한다.

    17 · 17 · 16ms 입력(재면 60), 선언값 2997/50
    -> (2997/50, FPS_DECLARED)
    """
    init, segment = _video_segment([17, 17, 16] * 80)

    choice = choose_frame_rate(init, [segment], Fraction(2997, 50))

    assert (choice.rate, choice.source) == (Fraction(2997, 50), FPS_DECLARED)


@pytest.mark.parametrize(
    ("timescale", "durations", "expected"),
    [
        (30000, [1001] * 120, Fraction(30000, 1001)),
        (60000, [1001] * 240, Fraction(60000, 1001)),
        (1000, [33, 33, 34] * 40, Fraction(30)),  # 30fps를 ms로 적은 것
        (1000, [40] * 100, Fraction(25)),
        (90000, [1500] * 240, Fraction(60)),
        (24000, [1001] * 96, Fraction(24000, 1001)),
    ],
    ids=["29.97", "59.94", "30-in-ms", "25", "60", "23.976"],
)
def test_choose_frame_rate_snaps_to_the_nearest_standard_rate(timescale, durations, expected):
    """choose_frame_rate는 잰 평균 프레임률이 표준 비율과 0.1% 안이면 가장 가까운 표준 비율을 돌려줘야 한다.

    주석의 경우마다 (timescale, 샘플 길이들). 선언값 없음
    -> (기대한 표준 비율, FPS_STANDARD)
    """
    init, segment = _video_segment(durations, timescale)

    choice = choose_frame_rate(init, [segment])

    assert (choice.rate, choice.source) == (expected, FPS_STANDARD)


@pytest.mark.parametrize(
    ("durations", "expected"),
    [
        ([100] * 40, Fraction(10)),  # 10fps
        ([50] * 80, Fraction(20)),  # 20fps
        ([17] * 60, Fraction(1000, 17)),  # 정말로 58.8fps인 입력 — 60과 2% 다르다
        # 프레임이 빠진 입력 — 120프레임, 간격 119개의 합 = 84 × 30 − 마지막 34 = 2486틱
        ([16, 17, 17, 34] * 30, Fraction(119000, 2486)),
    ],
    ids=["10", "20", "58.8", "dropped-frames"],
)
def test_choose_frame_rate_keeps_the_measured_average_outside_standard_rates(durations, expected):
    """choose_frame_rate는 잰 평균 프레임률이 어느 표준 비율과도 0.1% 넘게 다르면 잰 값을 분수 그대로 돌려줘야 한다.

    timescale 1000, 주석의 샘플 길이들. 선언값 없음
    -> ((프레임 수 − 1) × 1000 ÷ (마지막 PTS − 첫 PTS), FPS_MEASURED)
    """
    init, segment = _video_segment(durations)

    choice = choose_frame_rate(init, [segment])

    assert (choice.rate, choice.source) == (expected, FPS_MEASURED)


def test_choose_frame_rate_uses_the_sample_duration_of_a_single_frame():
    """choose_frame_rate는 프레임이 하나뿐이면 그 프레임의 샘플 길이로 정해야 한다.

    timescale 1000, 영상 1프레임(길이 40틱)
    -> (25, FPS_MEASURED)
    """
    init, segment = _video_segment([40])

    choice = choose_frame_rate(init, [segment])

    assert (choice.rate, choice.source) == (Fraction(25), FPS_MEASURED)


def test_choose_frame_rate_fails_without_any_frame_or_declared_value():
    """choose_frame_rate는 선언값도 영상 프레임도 없으면 ValueError를 내야 한다.

    오디오뿐인 세그먼트, 선언값 없음
    -> ValueError
    """
    init = _init()
    segment = parse_media_segment(_segment(0, video=False), init)

    with pytest.raises(ValueError):
        choose_frame_rate(init, [segment])


def test_plan_validates_selections_with_the_frame_rate_it_is_given():
    """plan_fmp4_sections는 프레임률을 넘겨받으면 첫 세그먼트로 다시 정하지 않고 그 값으로 구간을 검증해야 한다.

    10fps 입력, 구간 0.1 ~ 0.3초. 넘긴 프레임률 1(프레임 번호 0 ~ 0) · 넘기지 않음(프레임 번호 1 ~ 3)
    -> 1을 넘기면 SelectionError(SELECTION_TOO_SHORT), 넘기지 않으면 구간 하나
    """
    init = _init()
    segments = [parse_media_segment(_segment(n), init) for n in range(2)]
    playlist = _playlist([1.0, 1.0])
    selection = [TimeRange(0.1, 0.3)]

    with pytest.raises(SelectionError) as info:
        plan_fmp4_sections(playlist, init, selection, segments.__getitem__, Fraction(1))

    assert info.value.message_key == SELECTION_TOO_SHORT
    assert len(plan_fmp4_sections(playlist, init, selection, segments.__getitem__)) == 1


# ================================================================ 끝 = 길이


def test_section_ending_at_a_video_length_off_the_frame_grid_is_accepted():
    """끝이 영상 길이(초)와 같은 구간은 길이 × fps의 소수부가 .5 이상이어도 통과하고 마지막 프레임에서 끝나야 한다.

    세그먼트 둘(영상 10프레임씩, 10fps), #EXTINF 1.0 · 1.06 — 길이 2.06초 = 20.6프레임. 구간 0.5 ~ 2.06초
    -> 구간 하나, 끝 프레임의 PTS == 1.9초
    """
    init = _init()
    segments = [parse_media_segment(_segment(n), init) for n in range(2)]
    playlist = _playlist([1.0, 1.06])

    sections = plan_fmp4_sections(
        playlist, init, [TimeRange(0.5, playlist.duration)], segments.__getitem__
    )

    assert (
        playlist.duration * 10 % 1 >= 0.5
    )  # 전제 — 끝의 프레임 번호(21)가 길이의 프레임 수(20)보다 크다
    assert len(sections) == 1
    assert sections[0].last_pts == 1.9
