"""타임코드 해석·표기와 실제 프레임 맞추기(core/utils/timecode.py) 단위 테스트 (#178).

핵심 계약:
- 타임코드의 시각은 명목 시각이다 — HH×3600 + MM×60 + SS + FF ÷ fps
- 29.97처럼 소수로 선언된 fps는 N×1000/1001 유리수로 계산한다
- 해석과 표기는 서로 역이다(60 · 30 · 29.97fps)
- 프레임 맞추기는 PTS가 가장 가까운 프레임을 고르고, 빠진 자리에서는 시작은 앞·끝은 뒤로 간다
"""

from fractions import Fraction

import pytest

from core.utils.timecode import (
    TIMECODE_FIELD_OUT_OF_RANGE,
    TIMECODE_FRAME_OUT_OF_RANGE,
    TIMECODE_INVALID_FORMAT,
    TimecodeError,
    format_milliseconds,
    format_timecode,
    frame_index,
    frame_rate,
    frames_per_second,
    parse_timecode,
    snap_to_frame,
)

NTSC_30 = Fraction(30000, 1001)  # 29.97fps의 실제 프레임률


# ================================================================ frame_rate


@pytest.mark.parametrize(
    ("fps", "expected"),
    [
        (60, Fraction(60)),
        (60.0, Fraction(60)),
        (30, Fraction(30)),
        (29.97, Fraction(30000, 1001)),
        (59.94, Fraction(60000, 1001)),
        (23.976, Fraction(24000, 1001)),
    ],
)
def test_frame_rate_maps_declared_fps_to_exact_rational(fps, expected):
    """frame_rate는 선언 fps가 정수나 N×1000/1001에 가까우면 그 유리수를 돌려줘야 한다.

    60 -> 60/1, 29.97 -> 30000/1001, 59.94 -> 60000/1001, 23.976 -> 24000/1001
    """
    assert frame_rate(fps) == expected


@pytest.mark.parametrize("fps", [0, -30, float("nan"), float("inf")])
def test_frame_rate_rejects_non_positive_or_non_finite(fps):
    """frame_rate는 fps가 0 이하이거나 유한하지 않으면 ValueError를 내야 한다.

    fps=0, -30, nan, inf
    -> ValueError
    """
    with pytest.raises(ValueError):
        frame_rate(fps)


@pytest.mark.parametrize(("fps", "expected"), [(60, 60), (30, 30), (29.97, 30), (59.94, 60)])
def test_frames_per_second_is_ceiling_of_frame_rate(fps, expected):
    """frames_per_second는 프레임률을 올림한 정수를 돌려줘야 한다.

    60 -> 60, 30 -> 30, 29.97 -> 30, 59.94 -> 60
    """
    assert frames_per_second(fps) == expected


def test_frame_index_keeps_ntsc_grid_after_six_hours():
    """frame_index는 29.97fps에서 6시간 지점의 프레임 시각을 그 프레임 번호로 바꿔야 한다.

    n=647352, seconds=n×1001/30000 (약 21600.6초), fps=29.97
    -> 647352
    """
    n = 647352
    seconds = float(Fraction(n * 1001, 30000))

    assert frame_index(seconds, 29.97) == n


# ================================================================ parse_timecode


def test_parse_timecode_reads_full_form():
    """parse_timecode는 HH:MM:SS:FF를 명목 시각으로 바꿔야 한다.

    "01:02:03:30", fps=60
    -> 3723.5
    """
    assert parse_timecode("01:02:03:30", 60) == pytest.approx(3723.5)


@pytest.mark.parametrize(("text", "expected"), [("5:03", 303.0), ("1:05:03", 3903.0)])
def test_parse_timecode_reads_short_forms(text, expected):
    """parse_timecode는 칸이 둘이면 MM:SS, 셋이면 HH:MM:SS로 읽어야 한다.

    "5:03" -> 303.0, "1:05:03" -> 3903.0 (fps=60)
    """
    assert parse_timecode(text, 60) == pytest.approx(expected)


def test_parse_timecode_divides_frames_by_ntsc_rate():
    """parse_timecode는 29.97fps에서 FF를 30000/1001로 나눠야 한다.

    "00:00:01:15", fps=29.97
    -> 1 + 15×1001/30000 = 1.5005
    """
    assert parse_timecode("00:00:01:15", 29.97) == pytest.approx(1.5005, abs=1e-9)


def test_parse_timecode_ignores_surrounding_whitespace():
    """parse_timecode는 앞뒤 공백이 있어도 같은 값을 돌려줘야 한다.

    " 5:03 ", fps=60
    -> 303.0
    """
    assert parse_timecode(" 5:03 ", 60) == pytest.approx(303.0)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "5",  # 칸 하나
        "1:2:3:4:5",  # 칸 다섯
        "1:aa",
        "1::3",  # 빈 칸
        "00:00:01.500",  # 밀리초 입력
        "-1:00",
        "1: 2",
        "５:03",  # 전각 숫자
    ],
)
def test_parse_timecode_rejects_malformed_text(text):
    """parse_timecode는 형태가 맞지 않으면 형식 오류 키로 TimecodeError를 내야 한다.

    칸 수가 2~4가 아님, 숫자가 아닌 글자, 빈 칸, 밀리초 표기
    -> message_key == TIMECODE_INVALID_FORMAT
    """
    with pytest.raises(TimecodeError) as info:
        parse_timecode(text, 60)

    assert info.value.message_key == TIMECODE_INVALID_FORMAT


@pytest.mark.parametrize("text", ["60:00", "0:60", "1:60:00", "1:00:60", "00:59:60:00"])
def test_parse_timecode_rejects_minutes_or_seconds_of_sixty(text):
    """parse_timecode는 MM이나 SS가 60 이상이면 칸 범위 키로 TimecodeError를 내야 한다.

    "60:00", "0:60", "1:60:00", "1:00:60", "00:59:60:00" (fps=60)
    -> message_key == TIMECODE_FIELD_OUT_OF_RANGE
    """
    with pytest.raises(TimecodeError) as info:
        parse_timecode(text, 60)

    assert info.value.message_key == TIMECODE_FIELD_OUT_OF_RANGE


@pytest.mark.parametrize(("fps", "last_frame"), [(60, 59), (30, 29), (29.97, 29)])
def test_parse_timecode_frame_field_is_bounded_by_fps(fps, last_frame):
    """parse_timecode는 FF가 fps 미만의 마지막 값이면 받고 그 다음 값이면 프레임 범위 키로 거부해야 한다.

    60fps: 59 통과 / 60 거부, 30fps: 29 / 30, 29.97fps: 29 / 30
    -> 거부의 message_key == TIMECODE_FRAME_OUT_OF_RANGE
    """
    parse_timecode(f"00:00:00:{last_frame}", fps)

    with pytest.raises(TimecodeError) as info:
        parse_timecode(f"00:00:00:{last_frame + 1}", fps)

    assert info.value.message_key == TIMECODE_FRAME_OUT_OF_RANGE


# ================================================================ format_timecode


@pytest.mark.parametrize(("fps", "frame_count"), [(60, 60), (30, 30), (29.97, 30)])
def test_timecode_round_trips_through_parse_and_format(fps, frame_count):
    """해석한 타임코드를 다시 표기하면 원래 문자열이어야 한다.

    fps=60 · 30 · 29.97, 시각 6종 × FF 전부
    -> format_timecode(parse_timecode(text)) == text
    """
    for prefix in ("00:00:00", "00:00:59", "00:59:59", "01:05:03", "12:34:56", "123:00:01"):
        for frame in range(frame_count):
            text = f"{prefix}:{frame:02d}"

            assert format_timecode(parse_timecode(text, fps), fps) == text


@pytest.mark.parametrize(
    ("seconds", "fps", "expected"),
    [
        (2.9999999996, 60, "00:00:03:00"),
        (0.9999, 30, "00:00:01:00"),
        (59.999, 29.97, "00:01:00:00"),
    ],
)
def test_format_timecode_carries_into_next_second(seconds, fps, expected):
    """format_timecode는 반올림한 FF가 칸의 범위를 넘으면 다음 초로 올려야 한다.

    2.9999999996초 @60 -> "00:00:03:00", 0.9999초 @30 -> "00:00:01:00", 59.999초 @29.97 -> "00:01:00:00"
    """
    assert format_timecode(seconds, fps) == expected


@pytest.mark.parametrize("seconds", [-0.001, float("nan"), float("inf")])
def test_format_timecode_rejects_negative_or_non_finite(seconds):
    """format_timecode는 시각이 음수이거나 유한하지 않으면 ValueError를 내야 한다.

    seconds=-0.001, nan, inf
    -> ValueError
    """
    with pytest.raises(ValueError):
        format_timecode(seconds, 60)


# ================================================================ format_milliseconds


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.0, "00:00:00.000"),
        (3723.5, "01:02:03.500"),
        (1.0004, "00:00:01.000"),
        (0.9996, "00:00:01.000"),  # 반올림이 1000이 되어 다음 초로 올라간다
        (360000.25, "100:00:00.250"),
    ],
)
def test_format_milliseconds_rounds_to_nearest_millisecond(seconds, expected):
    """format_milliseconds는 가장 가까운 밀리초로 반올림해 HH:MM:SS.mmm으로 표기해야 한다.

    3723.5 -> "01:02:03.500", 0.9996 -> "00:00:01.000", 360000.25 -> "100:00:00.250"
    """
    assert format_milliseconds(seconds) == expected


# ================================================================ snap_to_frame


def _uniform_pts(count: int, rate: Fraction) -> list[float]:
    """간격이 일정한 PTS 목록 — n ÷ rate."""
    return [float(Fraction(n) / rate) for n in range(count)]


@pytest.mark.parametrize("edge", ["start", "end"])
@pytest.mark.parametrize("offset_frames", [-0.4, 0.0, 0.4])
def test_snap_to_frame_picks_nearest_on_uniform_60fps(edge, offset_frames):
    """snap_to_frame은 간격이 일정한 60fps에서 반 프레임 안의 가장 가까운 프레임을 골라야 한다.

    PTS = n/60 (600개), 명목 시각 = (123 + offset)/60, offset = -0.4 · 0 · 0.4
    -> 123
    """
    pts = _uniform_pts(600, Fraction(60))

    assert snap_to_frame((123 + offset_frames) / 60, pts, 60, edge) == 123


@pytest.mark.parametrize("edge", ["start", "end"])
def test_snap_to_frame_follows_frame_number_on_jittered_intervals(edge):
    """snap_to_frame은 간격이 17·17·16ms로 반복되는 목록에서 명목 시각 n/60을 n번째 프레임에 맞춰야 한다.

    PTS = 0, 17, 34, 50, 67, 84, 100 … ms (300개), 명목 시각 = n/60
    -> n
    """
    pts = [((n // 3) * 50 + (0, 17, 34)[n % 3]) / 1000 for n in range(300)]

    assert [snap_to_frame(n / 60, pts, 60, edge) for n in range(300)] == list(range(300))


@pytest.mark.parametrize("missing_frame", [4, 5])
def test_snap_to_frame_start_falls_back_to_previous_frame_when_frame_is_missing(missing_frame):
    """snap_to_frame은 명목 시각의 프레임이 빠져 있으면 시작을 앞 프레임에 맞춰야 한다.

    60fps PTS에서 4·5번 프레임을 뺀 목록 [0,1,2,3,6,7,8,9]/60, 명목 시각 = 4/60 · 5/60
    -> 인덱스 3 (3번 프레임)
    """
    pts = [n / 60 for n in (0, 1, 2, 3, 6, 7, 8, 9)]

    assert snap_to_frame(missing_frame / 60, pts, 60, "start") == 3


@pytest.mark.parametrize("missing_frame", [4, 5])
def test_snap_to_frame_end_falls_back_to_next_frame_when_frame_is_missing(missing_frame):
    """snap_to_frame은 명목 시각의 프레임이 빠져 있으면 끝을 뒤 프레임에 맞춰야 한다.

    60fps PTS에서 4·5번 프레임을 뺀 목록 [0,1,2,3,6,7,8,9]/60, 명목 시각 = 4/60 · 5/60
    -> 인덱스 4 (6번 프레임)
    """
    pts = [n / 60 for n in (0, 1, 2, 3, 6, 7, 8, 9)]

    assert snap_to_frame(missing_frame / 60, pts, 60, "end") == 4


@pytest.mark.parametrize("edge", ["start", "end"])
def test_snap_to_frame_maps_nominal_time_to_nearest_ntsc_frame(edge):
    """snap_to_frame은 29.97fps에서 명목 시각 SS + FF×1001/30000에 가장 가까운 프레임을 골라야 한다.

    PTS = n×1001/30000 (300개), SS = 0~8, FF = 0~29
    -> round(명목 시각 × 30000/1001)
    """
    pts = _uniform_pts(300, NTSC_30)

    for second in range(9):
        for frame in range(30):
            nominal = Fraction(second) + Fraction(frame * 1001, 30000)
            expected = round(nominal * NTSC_30)

            assert snap_to_frame(float(nominal), pts, 29.97, edge) == expected


def test_snap_to_frame_breaks_tie_by_edge():
    """snap_to_frame은 두 프레임과의 거리가 같으면 시작은 앞, 끝은 뒤 프레임을 골라야 한다.

    PTS = [0.0, 0.5], fps=2, 명목 시각 = 0.25
    -> start: 0, end: 1
    """
    pts = [0.0, 0.5]

    assert snap_to_frame(0.25, pts, 2, "start") == 0
    assert snap_to_frame(0.25, pts, 2, "end") == 1


@pytest.mark.parametrize("edge", ["start", "end"])
def test_snap_to_frame_clamps_outside_the_list(edge):
    """snap_to_frame은 명목 시각이 목록 밖이면 가장 가까운 끝 프레임을 골라야 한다.

    PTS = [1.0, 2.0, 3.0], fps=1, 명목 시각 = 0.0 · 9.0
    -> 0 · 2
    """
    pts = [1.0, 2.0, 3.0]

    assert snap_to_frame(0.0, pts, 1, edge) == 0
    assert snap_to_frame(9.0, pts, 1, edge) == 2


def test_snap_to_frame_rejects_empty_list():
    """snap_to_frame은 PTS 목록이 비어 있으면 ValueError를 내야 한다.

    frame_pts=[]
    -> ValueError
    """
    with pytest.raises(ValueError):
        snap_to_frame(0.0, [], 60, "start")
