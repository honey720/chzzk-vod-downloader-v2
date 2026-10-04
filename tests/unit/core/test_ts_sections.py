"""TS 입력의 구간 계획(core/utils/ts_sections.py · core/utils/section_plan.py) 단위 테스트 (#309).

입력은 tests/unit/core/ts_builder.py가 패킷을 직접 조립한 합성 TS다. 암호화 VOD의
세그먼트에서 본 구조를 옮겼다(값은 실제 것이 아니다):

- 영상 60fps · 세그먼트 1초(60프레임) · GOP 0.5초 · B프레임(재정렬 지연 1프레임)
- 오디오 48kHz AAC(ADTS) · PES 하나에 최대 24프레임 · 세그먼트의 오디오는 그 세그먼트의
  영상이 끝나는 시각을 덮을 만큼 들어 있다
- VOD의 0초는 첫 오디오 PTS이고, 영상은 그보다 42.667ms 늦게 시작한다. 플레이리스트의 첫
  #EXTINF가 그만큼 길다

fMP4 입력의 계획은 test_fmp4_sections.py가 본다 — 같은 계획 코드를 다른 공급자로 돈다.
"""

from fractions import Fraction

import pytest

from core.api.hls import parse_media_playlist
from core.api.mpegts import TS_UNSUPPORTED, TsError, parse_ts
from core.models.plan import TimeRange
from core.models.ts_index import TsStreams
from core.utils.section_plan import FPS_DECLARED, FPS_STANDARD
from core.utils.selections import SELECTION_CROSSES_BREAK, SelectionError
from core.utils.ts_sections import (
    TsSectionSource,
    choose_ts_frame_rate,
    plan_ts_sections,
    ts_timeline,
)
from tests.unit.core.test_mpegts import AAC_FRAME, FRAME_60, _gop_frames
from tests.unit.core.ts_builder import Frame, build_ts

FPS = Fraction(60)
CLOCK = 90_000  # PTS의 초당 틱 수
WRAP = 1 << 33  # 33비트 타임스탬프가 0으로 돌아가는 값
ORIGIN = 90_000  # 합성 VOD의 0초 — 첫 오디오 PTS(원시 틱)
LEAD = 3840  # 영상이 오디오보다 늦게 시작하는 틱 수 (42.667ms)
SEGMENT = 60  # 세그먼트 하나의 프레임 수 (1초)


def _pts(frame: int, start: float = 0.0) -> float:
    """합성 녹화의 frame번째 영상 프레임의 시각(초) — 녹화가 VOD의 start초에서 시작할 때."""
    return start + (LEAD + frame * FRAME_60) / CLOCK


def _recording(
    frames: list[int], *, origin: int = ORIGIN, audio: str | None = "adts", extra_audio: int = 0
) -> list[TsStreams]:
    """세그먼트마다 프레임 수가 frames인 합성 녹화 — 세그먼트마다의 parse_ts 결과.

    Args:
        frames: 세그먼트마다의 영상 프레임 수. 0이면 그 세그먼트에는 영상이 없다
        origin: 첫 오디오 PTS(원시 틱)
        audio: "adts" — ADTS 오디오 / "plain" — ADTS가 아닌 오디오(프레임 수를 셀 수 없다)
            / None — 오디오 없음
        extra_audio: 영상이 없는 세그먼트에 넣을 오디오 프레임 수
    """
    segments = []
    shown = 0  # 지금까지 넣은 영상 프레임 수
    heard = 0  # 지금까지 넣은 오디오 프레임 수
    for count in frames:
        video = _gop_frames((origin + LEAD + shown * FRAME_60) % WRAP, count, gop=30)
        video = [Frame(pts=f.pts % WRAP, dts=f.dts % WRAP, idr=f.idr) for f in video]
        shown += count
        # 오디오는 이 세그먼트의 영상이 끝나는 시각을 덮을 때까지 넣는다
        video_end = LEAD + shown * FRAME_60
        wanted = extra_audio if not count else -(-video_end // AAC_FRAME) - heard
        pes = []
        while wanted > 0:
            size = min(24, wanted)
            pes.append(((origin + heard * AAC_FRAME) % WRAP, size))
            heard += size
            wanted -= size
        if audio == "adts":
            data = build_ts(video, adts=pes)
        elif audio == "plain":
            data = build_ts(video, audio=[pts for pts, _size in pes])
        else:
            data = build_ts(video, audio_pid=None)
        segments.append(parse_ts(data))
    return segments


def _playlist(durations: list[float], breaks: tuple[int, ...] = ()):
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for number, duration in enumerate(durations):
        if number in breaks:
            lines.append("#EXT-X-DISCONTINUITY")
        lines += [f"#EXTINF:{duration:.6f},", f"segment-{number:06d}.ts"]
    return parse_media_playlist("\n".join([*lines, "#EXT-X-ENDLIST"]))


def _durations(frames: list[int]) -> list[float]:
    """플레이리스트의 #EXTINF — 첫 세그먼트만 영상이 늦게 시작하는 만큼 길다."""
    lengths = [count / 60 for count in frames]
    lengths[0] += LEAD / CLOCK
    return lengths


# 세그먼트 여섯 개 — 1초짜리 다섯과 0.25초짜리 하나. 영상 315프레임, 길이 5.292667초
LAYOUT = [SEGMENT] * 5 + [15]


@pytest.fixture(scope="module")
def vod() -> list[TsStreams]:
    return _recording(LAYOUT)


def _plan(segments: list[TsStreams], selection: TimeRange, playlist=None):
    playlist = playlist or _playlist(_durations(LAYOUT))
    return plan_ts_sections(playlist, [selection], segments.__getitem__, FPS)[0]


# ================================================================ 시각 축


def test_timeline_starts_at_the_first_audio_pts_and_ends_with_the_last_video_frame(vod):
    """TS의 시각 축은 0초가 첫 오디오 PTS이고 영상 길이가 마지막 영상 프레임이 끝나는 시각이어야 한다.

    LAYOUT(영상 315프레임, 영상이 42.667ms 늦게 시작)
    -> 묶음 하나(0~5), 시작 0, 길이 == (3840 + 315 × 1500) ÷ 90,000
    """
    axis = ts_timeline(_playlist(_durations(LAYOUT)), vod.__getitem__, FPS)

    assert [(g.first, g.last) for g in axis.groups] == [(0, 5)]
    assert float(axis.groups[0].start) == 0.0
    # 근삿값이 아니라 같은 값이어야 한다 — 끝을 영상 길이에 맞춘 구간이 범위 안으로 판정된다
    assert axis.duration == (LEAD + 315 * FRAME_60) / CLOCK


def test_segment_spans_meet_and_the_first_one_starts_after_the_audio(vod):
    """세그먼트의 시각 범위는 첫 세그먼트가 영상이 시작하는 시각에서 시작하고, 이웃끼리 맞닿아야 한다.

    LAYOUT의 세그먼트 0~5
    -> 세그먼트 n의 범위 == [n번째까지의 프레임 수로 센 시각, 다음 세그먼트의 시작)
    """
    source = TsSectionSource(_playlist(_durations(LAYOUT)), vod.__getitem__, FPS)

    spans = [source.span_of(index) for index in range(len(LAYOUT))]

    shown = 0
    for (begin, end), count in zip(spans, LAYOUT):
        assert begin == pytest.approx(_pts(shown), abs=1e-9)
        assert end == pytest.approx(_pts(shown + count), abs=1e-9)
        shown += count
    assert source.origin_of(0) == pytest.approx(0.0, abs=1e-9)


# ================================================================ 구간 계획


def test_section_across_segment_boundaries_picks_the_nearest_frames(vod):
    """세그먼트 경계를 걸치는 구간은 양 끝 시각에서 가장 가까운 프레임과 그 사이의 세그먼트를 골라야 한다.

    구간 1.5 ~ 3.2초 (프레임은 0.042667 + n ÷ 60초에 있다)
    -> 첫 프레임 87번 · 끝 프레임 189번, 세그먼트 0 ~ 3(시작 쪽은 하나 앞부터)
    """
    section = _plan(vod, TimeRange(1.5, 3.2))

    assert section.first_pts == pytest.approx(_pts(87), abs=1e-9)
    assert section.last_pts == pytest.approx(_pts(189), abs=1e-9)
    assert (section.first_segment, section.last_segment) == (0, 3)


def test_section_at_the_start_begins_with_the_first_video_frame(vod):
    """0초에서 시작하는 구간의 첫 프레임은 오디오보다 늦게 시작하는 첫 영상 프레임이어야 한다.

    구간 0.0 ~ 0.5초
    -> 첫 프레임 0번(0.042667초) · 끝 프레임 27번, 세그먼트 0 ~ 0
    """
    section = _plan(vod, TimeRange(0.0, 0.5))

    assert section.first_pts == pytest.approx(_pts(0), abs=1e-9)
    assert section.last_pts == pytest.approx(_pts(27), abs=1e-9)
    assert (section.first_segment, section.last_segment) == (0, 0)


@pytest.mark.parametrize(
    ("end", "last_frame"),
    [
        (5.2, 309),  # 짧은 세그먼트 안의 시각
        (None, 314),  # 영상 길이(시각 축이 말하는 값) — 마지막 프레임
    ],
    ids=["inside", "to-the-end"],
)
def test_section_ending_in_the_short_last_segment(vod, end, last_frame):
    """마지막 짧은 세그먼트에서 끝나는 구간은 그 세그먼트의 프레임을 끝 프레임으로 골라야 한다.

    구간 4.9 ~ 주석의 끝(초). 마지막 세그먼트는 15프레임(300 ~ 314번)
    -> 끝 프레임 == 주석의 번호, 마지막 세그먼트 5
    """
    if end is None:
        end = ts_timeline(_playlist(_durations(LAYOUT)), vod.__getitem__, FPS).duration

    section = _plan(vod, TimeRange(4.9, end))

    assert section.last_pts == pytest.approx(_pts(last_frame), abs=1e-9)
    assert section.last_segment == 5


def test_segment_without_video_is_measured_by_its_audio():
    """영상 프레임이 없는 세그먼트는 오디오로 시각 범위를 재고, 영상 길이와 끝 프레임은 그 앞 세그먼트에서 나와야 한다.

    1초 세그먼트 둘 뒤에 오디오 12프레임만 든 세그먼트 하나. 구간 1.5초 ~ 영상 길이
    -> 영상 길이 == (3840 + 120 × 1500) ÷ 90,000, 세그먼트 2의 범위 == 그 오디오가 놓인 시각,
       끝 프레임 119번, 마지막 세그먼트 2(묶음의 끝까지 받는다)
    """
    frames = [SEGMENT, SEGMENT, 0]
    segments = _recording(frames, extra_audio=12)
    playlist = _playlist([*_durations(frames[:2]), 0.256])
    source = TsSectionSource(playlist, segments.__getitem__, FPS)
    heard = -(-(LEAD + 120 * FRAME_60) // AAC_FRAME)  # 앞의 두 세그먼트에 든 오디오 프레임 수
    duration = (LEAD + 120 * FRAME_60) / CLOCK

    axis = ts_timeline(playlist, segments.__getitem__, FPS)
    whole = TimeRange(1.5, axis.duration)  # 끝 = 시각 축이 말하는 영상 길이
    section = plan_ts_sections(playlist, [whole], segments.__getitem__, FPS)[0]

    assert not source.has_video(2)
    assert source.span_of(2) == (
        pytest.approx(heard * AAC_FRAME / CLOCK, abs=1e-9),
        pytest.approx((heard + 12) * AAC_FRAME / CLOCK, abs=1e-9),
    )
    assert axis.duration == pytest.approx(duration, abs=1e-9)
    assert section.last_pts == pytest.approx(_pts(119), abs=1e-9)
    assert section.last_segment == 2


def test_segment_with_a_single_frame_spans_one_frame_of_the_vod_frame_rate():
    """프레임이 하나뿐인 세그먼트의 시각 범위는 VOD 전체의 프레임 길이만큼이어야 한다.

    1초 세그먼트 둘 뒤에 영상 프레임 하나짜리 세그먼트. 프레임률 60. 구간 1.5초 ~ 영상 길이
    -> 세그먼트 2의 범위 == [120번 프레임의 시각, + 1/60초), 영상 길이 == 그 끝,
       끝 프레임 120번, 마지막 세그먼트 2
    """
    frames = [SEGMENT, SEGMENT, 1]
    segments = _recording(frames)
    playlist = _playlist(_durations(frames))
    source = TsSectionSource(playlist, segments.__getitem__, FPS)
    duration = (LEAD + 121 * FRAME_60) / CLOCK

    axis = ts_timeline(playlist, segments.__getitem__, FPS)
    whole = TimeRange(1.5, axis.duration)  # 끝 = 시각 축이 말하는 영상 길이
    section = plan_ts_sections(playlist, [whole], segments.__getitem__, FPS)[0]

    assert source.span_of(2) == (
        pytest.approx(_pts(120), abs=1e-9),
        pytest.approx(_pts(121), abs=1e-9),
    )
    assert axis.duration == pytest.approx(duration, abs=1e-9)
    assert section.last_pts == pytest.approx(_pts(120), abs=1e-9)
    assert section.last_segment == 2


@pytest.mark.parametrize(
    ("audio", "last_segment"),
    [
        ("adts", 3),  # 오디오가 끝 프레임이 끝날 때까지 있는 것을 안다
        ("plain", 4),  # 오디오가 있는데 끝나는 시각을 모른다 — 다음 세그먼트까지 넣는다
    ],
    ids=["known-end", "unknown-end"],
)
def test_unknown_audio_end_takes_one_more_segment(audio, last_segment):
    """오디오가 있는데 끝나는 시각을 모르면 끝 프레임이 든 세그먼트의 다음 세그먼트까지 받아야 한다.

    LAYOUT, 오디오는 주석의 종류. 구간 1.5 ~ 3.2초 (끝 프레임 189번은 세그먼트 3에 있다)
    -> 마지막 세그먼트 == 주석의 값, 첫·끝 프레임은 어느 쪽이든 87 · 189번
    """
    segments = _recording(LAYOUT, audio=audio)

    section = _plan(segments, TimeRange(1.5, 3.2))

    assert section.last_segment == last_segment
    assert section.first_pts == pytest.approx(_pts(87), abs=1e-9)
    assert section.last_pts == pytest.approx(_pts(189), abs=1e-9)


def test_video_without_audio_takes_no_extra_segment():
    """오디오가 없는 영상은 끝 프레임이 든 세그먼트까지만 받아야 한다 — 끝을 모르는 오디오와 다르게 다룬다.

    LAYOUT인데 오디오가 없다(0초가 첫 영상 프레임이고 프레임은 n ÷ 60초에 있다). 구간 1.5 ~ 3.2초
    -> 첫 프레임 90번 · 끝 프레임 192번(세그먼트 3), 마지막 세그먼트 3
    """
    segments = _recording(LAYOUT, audio=None)
    durations = [count / 60 for count in LAYOUT]  # 영상이 0초에서 시작한다

    section = _plan(segments, TimeRange(1.5, 3.2), _playlist(durations))

    assert section.first_pts == pytest.approx(90 / 60, abs=1e-9)
    assert section.last_pts == pytest.approx(192 / 60, abs=1e-9)
    assert (section.first_segment, section.last_segment) == (0, 3)


def test_unknown_audio_end_stops_at_the_last_segment():
    """오디오가 끝나는 시각을 모르는 영상에서 마지막 세그먼트에서 끝나는 구간은 넣을 세그먼트가 없어도 계획이 서야 한다.

    LAYOUT, ADTS가 아닌 오디오. 구간 4.9 ~ 5.2초 (끝 프레임 309번은 마지막 세그먼트에 있다)
    -> 마지막 세그먼트 5, 끝 프레임 309번
    """
    segments = _recording(LAYOUT, audio="plain")

    section = _plan(segments, TimeRange(4.9, 5.2))

    assert section.last_segment == 5
    assert section.last_pts == pytest.approx(_pts(309), abs=1e-9)


def test_span_of_a_segment_without_video_needs_the_audio_end():
    """영상이 없는 세그먼트의 시각 범위는 오디오가 끝나는 시각을 모르면 구할 수 없어야 한다.

    1초 세그먼트 하나 뒤에 ADTS가 아닌 오디오만 든 세그먼트
    -> span_of(1): TsError(TS_UNSUPPORTED)
    """
    segments = _recording([SEGMENT, 0], audio="plain", extra_audio=12)
    source = TsSectionSource(_playlist([1.042667, 0.256]), segments.__getitem__, FPS)

    with pytest.raises(TsError) as info:
        source.span_of(1)
    assert info.value.message_key == TS_UNSUPPORTED


def test_plan_does_not_look_for_a_time_in_an_audio_only_segment_after_the_video():
    """구간의 끝 시각을 찾을 때 영상이 끝난 뒤의 오디오만 든 세그먼트는 읽지 않아야 한다 — 추정이 그 세그먼트를 가리켜도.

    1초 세그먼트 둘 뒤에 ADTS가 아닌 오디오만 든 세그먼트(시각 범위를 구할 수 없다).
    플레이리스트의 #EXTINF가 0.8 · 0.8초로 실제보다 짧아 1.8초의 추정이 그 세그먼트를 가리킨다.
    구간 1.2 ~ 1.8초 (끝 프레임 105번은 세그먼트 1에 있다)
    -> 계획이 선다. 첫 프레임 69번 · 끝 프레임 105번, 세그먼트 0 ~ 2
       (오디오의 끝을 몰라 끝 프레임이 든 세그먼트의 다음 세그먼트까지 넣는다)
    """
    segments = _recording([SEGMENT, SEGMENT, 0], audio="plain", extra_audio=12)
    playlist = _playlist([0.8, 0.8, 0.256])

    section = plan_ts_sections(playlist, [TimeRange(1.2, 1.8)], segments.__getitem__, FPS)[0]

    assert section.first_pts == pytest.approx(_pts(69), abs=1e-9)
    assert section.last_pts == pytest.approx(_pts(105), abs=1e-9)
    assert (section.first_segment, section.last_segment) == (0, 2)


def test_plan_follows_timestamps_through_the_33bit_wraparound():
    """33비트 타임스탬프가 영상 도중에 0으로 돌아가도 구간의 프레임과 세그먼트가 같아야 한다.

    LAYOUT인데 첫 오디오 PTS가 2³³ − 2초의 틱 (세그먼트 1 ~ 2 사이에서 되감긴다). 구간 1.5 ~ 3.2초
    -> 첫 프레임 87번 · 끝 프레임 189번, 세그먼트 0 ~ 3
    """
    segments = _recording(LAYOUT, origin=WRAP - 2 * CLOCK)

    section = _plan(segments, TimeRange(1.5, 3.2))

    assert max(segments[1].video_pts) > min(segments[2].video_pts)  # 원시 값은 되감겼다
    assert section.first_pts == pytest.approx(_pts(87), abs=1e-9)
    assert section.last_pts == pytest.approx(_pts(189), abs=1e-9)
    assert (section.first_segment, section.last_segment) == (0, 3)


# ================================================================ 끊긴 자리


@pytest.fixture(scope="module")
def broken():
    """세그먼트 셋짜리 녹화 둘을 끊긴 자리로 이은 VOD — 둘째 녹화의 타임스탬프는 첫째와 이어지지 않는다."""
    first = _recording([SEGMENT] * 3)
    second = _recording([SEGMENT] * 3, origin=5_000_000_000)
    playlist = _playlist(_durations([SEGMENT] * 3) * 2, breaks=(3,))
    return playlist, first + second


def test_group_after_a_discontinuity_starts_where_the_previous_one_ended(broken):
    """끊긴 자리 뒤의 묶음은 앞 묶음이 끝난 VOD 시각에서 시작해야 한다.

    60프레임 세그먼트 셋 + 끊긴 자리 + 60프레임 세그먼트 셋
    -> 묶음 (0~2) · (3~5), 둘째의 시작 == 첫째의 끝 == (3840 + 180 × 1500) ÷ 90,000,
       영상 길이 == 그 시각의 두 배
    """
    playlist, segments = broken
    length = (LEAD + 180 * FRAME_60) / CLOCK

    axis = ts_timeline(playlist, segments.__getitem__, FPS)

    assert [(g.first, g.last) for g in axis.groups] == [(0, 2), (3, 5)]
    assert float(axis.groups[1].start) == pytest.approx(length, abs=1e-9)
    assert axis.duration == pytest.approx(2 * length, abs=1e-9)


def test_section_in_the_second_group_is_planned_in_vod_time(broken):
    """끊긴 자리 뒤의 구간은 그 묶음의 세그먼트에서 VOD 시각으로 프레임을 골라야 한다.

    위의 VOD(둘째 묶음은 3.042667초에서 시작한다). 구간 3.5 ~ 4.5초
    -> 둘째 녹화의 25번 · 85번 프레임, 세그먼트 3 ~ 4
    """
    playlist, segments = broken
    start = (LEAD + 180 * FRAME_60) / CLOCK

    section = plan_ts_sections(playlist, [TimeRange(3.5, 4.5)], segments.__getitem__, FPS)[0]

    assert section.first_pts == pytest.approx(_pts(25, start), abs=1e-9)
    assert section.last_pts == pytest.approx(_pts(85, start), abs=1e-9)
    assert (section.first_segment, section.last_segment) == (3, 4)


def test_section_across_a_discontinuity_is_rejected(broken):
    """끊긴 자리를 넘는 구간은 거부돼야 한다.

    위의 VOD. 구간 2.5 ~ 3.5초 (끊긴 자리는 3.042667초)
    -> SelectionError(SELECTION_CROSSES_BREAK)
    """
    playlist, segments = broken

    with pytest.raises(SelectionError) as info:
        plan_ts_sections(playlist, [TimeRange(2.5, 3.5)], segments.__getitem__, FPS)

    assert info.value.violations == {0: (SELECTION_CROSSES_BREAK,)}


# ================================================================ 프레임률


def test_frame_rate_is_the_declared_value_when_there_is_one(vod):
    """선언된 프레임률이 있으면 세그먼트를 재지 않고 그 값이어야 한다.

    60fps 세그먼트, 선언값 30000/1001
    -> (30000/1001, FPS_DECLARED)
    """
    choice = choose_ts_frame_rate([vod[0]], Fraction(30000, 1001))

    assert (choice.rate, choice.source) == (Fraction(30000, 1001), FPS_DECLARED)


def test_frame_rate_is_measured_from_the_pts_of_the_first_segment(vod):
    """선언값이 없으면 세그먼트의 PTS 평균 간격으로 정해야 한다.

    60fps 세그먼트(B프레임 포함), 선언값 없음
    -> (60, FPS_STANDARD)
    """
    choice = choose_ts_frame_rate([vod[0]])

    assert (choice.rate, choice.source) == (Fraction(60), FPS_STANDARD)


def test_frame_rate_cannot_be_measured_from_a_single_frame():
    """선언값이 없고 프레임이 하나뿐이면 프레임률을 정할 수 없어야 한다.

    영상 프레임 하나짜리 세그먼트, 선언값 없음
    -> ValueError
    """
    segment = parse_ts(build_ts([Frame(pts=3000, idr=True)]))

    with pytest.raises(ValueError):
        choose_ts_frame_rate([segment])
