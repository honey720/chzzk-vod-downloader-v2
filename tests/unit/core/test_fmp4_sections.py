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
import core.utils.fmp4_sections as sections_module
from core.utils.fmp4_sections import (
    FPS_DECLARED,
    FPS_MEASURED,
    FPS_STANDARD,
    choose_frame_rate,
    fmp4_timeline,
    plan_fmp4_sections,
)
from core.utils.selections import (
    SELECTION_CROSSES_BREAK,
    SELECTION_NOT_LOCATED,
    SELECTION_OUT_OF_RANGE,
    SELECTION_TOO_SHORT,
    SelectionError,
)
from core.utils.timecode import TimecodeError, parse_timecode
from tests.unit.core.mp4_builder import box
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


def _segment(
    number: int, *, video: bool = True, audio_samples: int = 8, frames: int = 10, last: int = 100
) -> bytes:
    """number번째 1초 세그먼트 — 영상 frames프레임(첫 프레임이 키프레임, 마지막 프레임의 길이 last틱)과 오디오."""
    trafs = []
    if video:
        frames = [
            Sample(
                duration=last if n == frames - 1 else 100,
                size=10,
                flags=KEY if n == 0 else NON_KEY,
            )
            for n in range(frames)
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

    세그먼트 셋 — 0 · 1은 영상 10프레임씩과 오디오, 2는 오디오뿐. 영상 길이 2.0초(프레임 19가 끝나는 시각),
    구간 0.5 ~ 2.0초
    -> 끝 프레임의 PTS == 1.9초(둘째 세그먼트의 마지막 프레임), 받는 세그먼트 0~2
    """
    init = _init()
    segments = [
        parse_media_segment(_segment(0), init),
        parse_media_segment(_segment(1), init),
        parse_media_segment(_segment(2, video=False, audio_samples=4), init),
    ]
    playlist = _playlist([1.0, 1.0, 0.5])

    sections = plan_fmp4_sections(playlist, init, [TimeRange(0.5, 2.0)], segments.__getitem__)

    assert fmp4_timeline(playlist, init, segments.__getitem__).duration == 2.0
    assert len(sections) == 1
    assert (sections[0].first_pts, sections[0].last_pts) == (0.5, 1.9)
    assert (sections[0].first_segment, sections[0].last_segment) == (0, 2)


def test_section_to_the_video_length_is_rejected_when_it_starts_before_a_break():
    """끝이 영상 길이와 같은 구간은 끊긴 자리 앞에서 시작하면 끊긴 자리를 넘는 구간으로 거부해야 한다.

    세그먼트 0 · 1(영상 1초씩) · 끊김 · 세그먼트 2(영상 1초). 영상 길이 3.0초, 구간 0.5 ~ 3.0초
    -> SelectionError(SELECTION_CROSSES_BREAK)
    """
    init = _init()
    segments = [parse_media_segment(_segment(n % 2), init) for n in range(3)]
    lines = ["#EXTM3U", '#EXT-X-MAP:URI="init.mp4"', "#EXTINF:1.000000,", "seg-0.m4s"]
    lines += ["#EXTINF:1.000000,", "seg-1.m4s", "#EXT-X-DISCONTINUITY", "#EXTINF:1.000000,"]
    playlist = parse_media_playlist("\n".join([*lines, "seg-2.m4s", "#EXT-X-ENDLIST"]))

    with pytest.raises(SelectionError) as info:
        plan_fmp4_sections(playlist, init, [TimeRange(0.5, 3.0)], segments.__getitem__)

    assert fmp4_timeline(playlist, init, segments.__getitem__).duration == 3.0
    assert info.value.message_key == SELECTION_CROSSES_BREAK


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

    세그먼트 둘(영상 10프레임씩, 10fps), 마지막 프레임의 길이만 160틱 — 영상 길이 2.06초 = 20.6프레임.
    구간 0.5 ~ 2.06초
    -> 구간 하나, 끝 프레임의 PTS == 1.9초
    """
    init = _init()
    segments = [
        parse_media_segment(_segment(0), init),
        parse_media_segment(_segment(1, last=160), init),
    ]
    playlist = _playlist([1.0, 1.0])
    length = fmp4_timeline(playlist, init, segments.__getitem__).duration

    sections = plan_fmp4_sections(playlist, init, [TimeRange(0.5, length)], segments.__getitem__)

    assert length == pytest.approx(2.06)
    assert length * 10 % 1 >= 0.5  # 전제 — 끝의 프레임 번호(21)가 길이의 프레임 수(20)보다 크다
    assert len(sections) == 1
    assert sections[0].last_pts == 1.9


# ================================================================ 실제 다시보기의 모양


class _Replay:
    """실제 다시보기의 모양을 한 합성 VOD — 세그먼트를 달라는 대로 만들어 준다.

    - 영상 timescale 6000, 세그먼트 하나에 120프레임 · moof 둘(60프레임씩, 사이에 emsg)
    - 프레임 길이는 100틱이고 moof의 마지막 두 프레임만 102틱이다 — moof 하나의 길이의 합이
      6004틱(1.000667초)인데 다음 moof는 6000틱(1초) 뒤에서 시작한다
    - 그래서 #EXTINF는 2.001333이고 세그먼트의 실제 간격은 2.000초다
    - 오디오 timescale 48000, 첫 PTS가 영상보다 앞이다 — VOD 0초는 오디오의 첫 PTS다
    """

    VIDEO_START = 17994  # 첫 세그먼트의 영상 tfdt(틱)
    COMPOSITION = 198  # 영상의 PTS − DTS(틱)
    AUDIO_START = 143599  # 첫 세그먼트의 오디오 tfdt(틱) = 2.991646초 — VOD 0초
    DURATIONS = [100] * 58 + [102, 102]  # moof 하나의 영상 샘플 길이(틱)

    def __init__(self, count: int, *, program_times: bool, extinf: float = 2.001333):
        self.count = count
        self.init = parse_init_segment(
            init_segment(
                [
                    InitTrack(VIDEO, b"vide", 6000, trex=(0, 0, 0)),
                    InitTrack(AUDIO, b"soun", 48000, codec=b"mp4a", trex=(0, 0, 0)),
                ]
            )
        )
        lines = ["#EXTM3U", "#EXT-X-VERSION:7", '#EXT-X-MAP:URI="init.mp4"']
        for number in range(count):
            if program_times:
                seconds = 2 * number
                stamp = f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
                lines.append(f"#EXT-X-PROGRAM-DATE-TIME:2026-01-01T{stamp}.062Z")
            lines += [f"#EXTINF:{extinf},", f"seg-{number}.m4v"]
        self.playlist = parse_media_playlist("\n".join([*lines, "#EXT-X-ENDLIST"]))
        self.asked: list[int] = []  # segment_at에 온 인덱스 — 온 순서대로
        self._parsed: dict[int, Fmp4Segment] = {}

    def segment_at(self, index: int) -> Fmp4Segment:
        self.asked.append(index)
        if index not in self._parsed:
            self._parsed[index] = parse_media_segment(self._segment(index), self.init)
        return self._parsed[index]

    def _segment(self, index: int) -> bytes:
        data = b""
        for half in range(2):
            video = [
                Sample(
                    duration=d,
                    size=10,
                    flags=KEY if n == 0 else NON_KEY,
                    composition=self.COMPOSITION,
                )
                for n, d in enumerate(self.DURATIONS)
            ]
            audio = [Sample(duration=1024, size=7) for _ in range(47)]
            fragment = Fragment(
                [
                    Traf(
                        VIDEO,
                        [Run(video)],
                        decode_time=self.VIDEO_START + 12000 * index + 6000 * half,
                    ),
                    Traf(
                        AUDIO,
                        [Run(audio)],
                        decode_time=self.AUDIO_START + 96000 * index + 48000 * half,
                    ),
                ]
            )
            data += media_segment([fragment], styp=half == 0)
            if half == 0:
                data += box(b"emsg", bytes(40))
        return data

    def frame_time(self, segment: int, frame: int) -> float:
        """세그먼트 segment의 frame번째(0~119) 프레임의 PTS — VOD 시각(초). 검사자가 따로 계산한다."""
        half, n = divmod(frame, 60)
        ticks = (
            self.VIDEO_START
            + 12000 * segment
            + 6000 * half
            + sum(self.DURATIONS[:n])
            + self.COMPOSITION
        )
        return float(Fraction(ticks, 6000) - Fraction(self.AUDIO_START, 48000))

    def nearest(self, seconds: float) -> float:
        """seconds에 가장 가까운 프레임의 PTS — 앞뒤 세그먼트의 모든 프레임에서 고른다."""
        around = int(seconds // 2)
        times = [
            self.frame_time(segment, frame)
            for segment in range(max(around - 1, 0), min(around + 2, self.count))
            for frame in range(120)
        ]
        return min(times, key=lambda time: abs(time - seconds))


def test_timeline_measures_the_video_length_from_real_timestamps_not_extinf():
    """fmp4_timeline은 영상 길이를 #EXTINF의 합이 아니라 마지막 영상 프레임이 끝나는 실제 시각으로 정해야 한다.

    세그먼트 5,000개(#EXTINF 2.001333, 실제 간격 2.000초)
    -> 길이 == 마지막 세그먼트의 마지막 프레임 PTS + 그 프레임의 길이(102틱),
       #EXTINF의 합은 그보다 6초 넘게 길다. 읽은 세그먼트는 첫 · 마지막 둘뿐
    """
    replay = _Replay(5000, program_times=False)

    timeline = fmp4_timeline(replay.playlist, replay.init, replay.segment_at)

    assert timeline.duration == pytest.approx(replay.frame_time(4999, 119) + 102 / 6000)
    assert replay.playlist.duration - timeline.duration > 6
    assert sorted(set(replay.asked)) == [0, 4999]


@pytest.mark.parametrize("program_times", [True, False], ids=["pdt", "extinf-only"])
def test_section_near_the_end_of_a_long_replay_lands_on_the_right_frames(program_times):
    """#EXTINF의 누적이 실제 시각과 세그먼트 여럿만큼 벌어진 위치에서도 구간의 첫·끝 프레임은 요청 시각에 가장 가까운 프레임이어야 한다.

    세그먼트 5,000개(끝에서 #EXTINF 누적이 6.7초 = 세그먼트 셋 넘게 앞선다), 60fps,
    구간 9990.0 ~ 9995.5초. PROGRAM-DATE-TIME 있음 · 없음
    -> 첫·끝 프레임의 PTS == 검사자가 프레임 시각에서 직접 고른 가장 가까운 프레임,
       받는 세그먼트 4993~4997 (시작이 든 4994의 앞 하나부터 끝이 든 4997까지)
    """
    replay = _Replay(5000, program_times=program_times)
    selection = TimeRange(9990.0, 9995.5)

    section = plan_fmp4_sections(
        replay.playlist, replay.init, [selection], replay.segment_at, Fraction(60)
    )[0]

    assert section.first_pts == pytest.approx(replay.nearest(9990.0))
    assert section.last_pts == pytest.approx(replay.nearest(9995.5))
    assert abs(section.first_pts - 9990.0) < 1 / 60
    assert abs(section.last_pts - 9995.5) < 1 / 60
    assert (section.first_segment, section.last_segment) == (4993, 4997)


def test_program_date_time_finds_the_segment_without_extra_reads():
    """PROGRAM-DATE-TIME이 있으면 구간의 세그먼트를 추정 한 번에 찾고, 없으면 다시 추정하느라 더 읽어야 한다.

    세그먼트 5,000개, 구간 9990.0 ~ 9995.5초 (받는 세그먼트 4993~4997)
    -> PDT 있음: 읽은 세그먼트가 길이를 재는 0 · 4999와 구간의 4993~4997뿐.
       PDT 없음: 처음 추정이 어긋나 그 밖의 세그먼트도 읽는다
    """
    selection = [TimeRange(9990.0, 9995.5)]
    with_times = _Replay(5000, program_times=True)
    without = _Replay(5000, program_times=False)

    plan_fmp4_sections(with_times.playlist, with_times.init, selection, with_times.segment_at)
    plan_fmp4_sections(without.playlist, without.init, selection, without.segment_at)

    needed = {0, 4999, *range(4993, 4998)}
    assert set(with_times.asked) <= needed
    assert set(without.asked) - needed  # 어긋난 추정으로 읽은 세그먼트가 있다
    assert len(set(without.asked)) <= len(needed) + 4


def test_section_fails_when_the_segment_cannot_be_located_in_the_allowed_steps(monkeypatch):
    """구간의 시각이 놓인 세그먼트를 정해진 횟수 안에 찾지 못하면 틀린 프레임을 고르지 않고 키 기반 오류로 실패해야 한다.

    세그먼트 5,000개 · PROGRAM-DATE-TIME 없음(처음 추정이 세그먼트 셋 넘게 어긋난다), 찾는 걸음을 1로 제한,
    구간 9990.0 ~ 9995.5초
    -> SelectionError(SELECTION_NOT_LOCATED)
    """
    monkeypatch.setattr(sections_module, "_MAX_LOCATE_STEPS", 1)
    replay = _Replay(5000, program_times=False)

    with pytest.raises(SelectionError) as info:
        plan_fmp4_sections(
            replay.playlist, replay.init, [TimeRange(9990.0, 9995.5)], replay.segment_at
        )

    assert info.value.message_key == SELECTION_NOT_LOCATED
    assert info.value.violations == {0: (SELECTION_NOT_LOCATED,)}


def test_section_past_the_real_length_is_out_of_range_even_inside_the_extinf_total():
    """구간의 끝이 실제 영상 길이를 넘으면 #EXTINF의 합 안이어도 범위 위반이어야 한다.

    세그먼트 5,000개 — 실제 길이 약 10000.04초, #EXTINF의 합 약 10006.7초. 구간 9990 ~ 10003초
    -> SelectionError(SELECTION_OUT_OF_RANGE)
    """
    replay = _Replay(5000, program_times=True)
    assert replay.playlist.duration > 10003

    with pytest.raises(SelectionError) as info:
        plan_fmp4_sections(
            replay.playlist, replay.init, [TimeRange(9990.0, 10003.0)], replay.segment_at
        )

    assert info.value.message_key == SELECTION_OUT_OF_RANGE


def test_section_reads_frames_from_the_second_moof_of_a_segment():
    """구간의 시각이 세그먼트의 둘째 moof에 놓이면 그 moof의 프레임을 골라야 한다.

    세그먼트 40개, 구간 60.0 ~ 61.5초 — 60.0초는 세그먼트 29의 둘째 moof(59.04 ~ 60.04초)에 있다
    -> 첫 프레임의 PTS가 요청과 한 프레임 안, 받는 세그먼트 28~30
    """
    replay = _Replay(40, program_times=True)

    section = plan_fmp4_sections(
        replay.playlist, replay.init, [TimeRange(60.0, 61.5)], replay.segment_at, Fraction(60)
    )[0]

    assert section.first_pts == pytest.approx(replay.nearest(60.0))
    assert abs(section.first_pts - 60.0) < 1 / 60
    assert replay.frame_time(29, 60) <= section.first_pts < replay.frame_time(30, 0)
    assert (section.first_segment, section.last_segment) == (28, 30)


def test_time_between_two_segments_starts_on_the_frame_before_the_gap():
    """구간의 시작이 두 세그먼트 사이의 프레임 없는 자리에 놓이면 오류 없이 빈 자리 앞의 프레임에서 시작해야 한다.

    10fps 세그먼트 셋 — 둘째 세그먼트에는 프레임이 5개뿐이다(1.0 ~ 1.4초, 1.5 ~ 1.9초가 비었다).
    구간 1.7 ~ 2.5초
    -> 첫 프레임의 PTS == 1.4초(빈 자리 앞의 마지막 프레임), 끝 프레임의 PTS == 2.5초
    """
    init = _init()
    segments = [
        parse_media_segment(_segment(0), init),
        parse_media_segment(_segment(1, frames=5), init),
        parse_media_segment(_segment(2), init),
    ]

    section = plan_fmp4_sections(
        _playlist([1.0, 1.0, 1.0]), init, [TimeRange(1.7, 2.5)], segments.__getitem__
    )[0]

    assert (section.first_pts, section.last_pts) == (1.4, 2.5)
