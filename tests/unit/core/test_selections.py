"""구간 모델과 구간 검증(TimeRange · core/utils/selections.py) 단위 테스트 (#178).

핵심 계약:
- TimeRange는 start < end가 아니면 만들어지지 않는다
- validate_selections는 구간 번호별로 위반 키를 돌려준다 — 순서·범위·최소 길이·중복·개수
- 겹치는 구간은 허용한다
- 시각 비교는 프레임 단위다
"""

from fractions import Fraction

import pytest

from core.models.plan import TimeRange
from core.utils.selections import (
    MAX_SELECTIONS,
    SELECTION_DUPLICATE,
    SELECTION_ORDER,
    SELECTION_OUT_OF_RANGE,
    SELECTION_TOO_MANY,
    SELECTION_TOO_SHORT,
    SelectionError,
    reaches_end,
    validate_selections,
)

FPS = 60
FRAME = 1 / 60  # 60fps 한 프레임의 길이(초)
DURATION = 10.0  # 60fps에서 600프레임


# ================================================================ TimeRange


@pytest.mark.parametrize(("start", "end"), [(5.0, 5.0), (5.0, 4.0), (float("nan"), 1.0)])
def test_time_range_rejects_start_not_before_end(start, end):
    """TimeRange는 start < end가 아니면 ValueError를 내야 한다.

    (5.0, 5.0), (5.0, 4.0), (nan, 1.0)
    -> ValueError
    """
    with pytest.raises(ValueError):
        TimeRange(start, end)


def test_time_range_accepts_start_before_end():
    """TimeRange는 start < end이면 두 값을 그대로 담아야 한다.

    (1.5, 2.0)
    -> start == 1.5, end == 2.0
    """
    selection = TimeRange(1.5, 2.0)

    assert (selection.start, selection.end) == (1.5, 2.0)


# ================================================================ 통과


def test_validate_selections_returns_empty_when_all_rules_hold():
    """validate_selections는 위반이 없으면 빈 dict를 돌려줘야 한다.

    [(0.0, 1.0), (2.0, 3.5)], duration=10.0, fps=60
    -> {}
    """
    assert validate_selections([(0.0, 1.0), (2.0, 3.5)], DURATION, FPS) == {}


def test_validate_selections_accepts_time_range_objects():
    """validate_selections는 TimeRange 객체도 쌍과 같게 판정해야 한다.

    [TimeRange(1.0, 2.0), TimeRange(1.0, 2.0)]
    -> {0: (중복,), 1: (중복,)}
    """
    result = validate_selections([TimeRange(1.0, 2.0), TimeRange(1.0, 2.0)], DURATION, FPS)

    assert result == {0: (SELECTION_DUPLICATE,), 1: (SELECTION_DUPLICATE,)}


# ================================================================ 순서


@pytest.mark.parametrize(("start", "end"), [(5.0, 5.0), (5.0, 4.0)])
def test_validate_selections_flags_start_not_before_end(start, end):
    """validate_selections는 시작이 끝과 같거나 뒤이면 순서 위반만 돌려줘야 한다.

    (5.0, 5.0), (5.0, 4.0), duration=10.0
    -> {0: (순서,)}
    """
    assert validate_selections([(start, end)], DURATION, FPS) == {0: (SELECTION_ORDER,)}


# ================================================================ 최소 길이


def test_validate_selections_accepts_exactly_one_frame():
    """validate_selections는 길이가 정확히 1프레임인 구간을 통과시켜야 한다.

    (5.0, 5.0 + 1/60), fps=60
    -> {}
    """
    assert validate_selections([(5.0, 5.0 + FRAME)], DURATION, FPS) == {}


def test_validate_selections_flags_shorter_than_one_frame():
    """validate_selections는 시작과 끝이 같은 프레임이면 최소 길이 위반을 돌려줘야 한다.

    (5.0, 5.0 + 0.4/60), fps=60
    -> {0: (최소 길이,)}
    """
    result = validate_selections([(5.0, 5.0 + FRAME * 0.4)], DURATION, FPS)

    assert result == {0: (SELECTION_TOO_SHORT,)}


def test_validate_selections_counts_one_frame_despite_float_error():
    """validate_selections는 float 오차가 있어도 1프레임 떨어진 두 시각을 1프레임 길이로 봐야 한다.

    start = 0.1 + 0.2 (0.30000000000000004), end = 0.3 + 1/60
    -> {}
    """
    start = 0.1 + 0.2  # 0.3보다 미세하게 크다 — 끝과의 차이가 1/60보다 작아진다

    assert validate_selections([(start, 0.3 + FRAME)], DURATION, FPS) == {}


# ================================================================ 범위


def test_validate_selections_accepts_whole_video():
    """validate_selections는 0부터 영상 길이까지인 구간을 통과시켜야 한다.

    (0.0, 10.0), duration=10.0
    -> {}
    """
    assert validate_selections([(0.0, DURATION)], DURATION, FPS) == {}


def test_validate_selections_flags_end_one_frame_past_duration():
    """validate_selections는 끝이 영상 길이보다 1프레임 뒤이면 범위 위반을 돌려줘야 한다.

    (0.0, 10.0 + 1/60), duration=10.0
    -> {0: (범위,)}
    """
    result = validate_selections([(0.0, DURATION + FRAME)], DURATION, FPS)

    assert result == {0: (SELECTION_OUT_OF_RANGE,)}


def test_validate_selections_flags_start_one_frame_before_zero():
    """validate_selections는 시작이 0보다 1프레임 앞이면 범위 위반을 돌려줘야 한다.

    (-1/60, 1.0), duration=10.0
    -> {0: (범위,)}
    """
    assert validate_selections([(-FRAME, 1.0)], DURATION, FPS) == {0: (SELECTION_OUT_OF_RANGE,)}


@pytest.mark.parametrize("rate", [Fraction(30000, 1001), Fraction(2997, 100), 29.97])
@pytest.mark.parametrize(
    ("end_frame", "expected"),
    [
        (299, {}),  # 299번째 프레임 경계는 9.98초 — 10초 안이다
        (300, {0: (SELECTION_OUT_OF_RANGE,)}),  # 300번째 프레임 경계는 10.01초 — 10초 밖이다
    ],
)
def test_validate_selections_bounds_end_by_last_whole_frame_at_2997(end_frame, expected, rate):
    """validate_selections는 29.97 계열 프레임률에서 영상 길이 안의 마지막 프레임 경계까지만 끝으로 받아야 한다.

    duration=10.0, fps = 30000/1001 · 2997/100 · 29.97, 끝 = 299프레임 · 300프레임 (끝 시각 = 프레임 ÷ fps)
    -> 299: {}, 300: {0: (범위,)}
    """
    end = float(end_frame / Fraction(str(rate)))

    assert validate_selections([(0.0, end)], 10.0, rate) == expected


@pytest.mark.parametrize("fps", [Fraction(2997, 100), 29.97])
def test_validate_selections_accepts_end_at_duration_after_five_hours_of_decimal_2997(fps):
    """validate_selections는 간격이 400/11988초인 5시간 반짜리 영상에서 끝이 영상 길이인 구간을 통과시켜야 한다.

    영상 600,000프레임, duration = 600,000 × 400/11988 (약 20,020초), 구간 (0.0, duration), fps = 2997/100 · 29.97
    -> {}
    """
    duration = float(Fraction(600_000 * 400, 11988))

    assert validate_selections([(0.0, duration)], duration, fps) == {}


def test_validate_selections_flags_non_finite_time_as_out_of_range():
    """validate_selections는 유한하지 않은 시각을 범위 위반 하나로 돌려줘야 한다.

    (nan, 1.0)
    -> {0: (범위,)}
    """
    result = validate_selections([(float("nan"), 1.0)], DURATION, FPS)

    assert result == {0: (SELECTION_OUT_OF_RANGE,)}


def test_validate_selections_reports_every_broken_rule_in_rule_order():
    """validate_selections는 한 구간이 여러 규칙을 어기면 규칙 순서대로 모두 돌려줘야 한다.

    (12.0, 11.0), duration=10.0
    -> {0: (순서, 범위)}
    """
    result = validate_selections([(12.0, 11.0)], DURATION, FPS)

    assert result == {0: (SELECTION_ORDER, SELECTION_OUT_OF_RANGE)}


# ================================================================ 중복 · 겹침


def test_validate_selections_flags_every_member_of_a_duplicate_group():
    """validate_selections는 시작·끝이 둘 다 같은 구간들을 모두 중복으로 돌려줘야 한다.

    [(1.0, 2.0), (3.0, 4.0), (1.0, 2.0)]
    -> {0: (중복,), 2: (중복,)}
    """
    result = validate_selections([(1.0, 2.0), (3.0, 4.0), (1.0, 2.0)], DURATION, FPS)

    assert result == {0: (SELECTION_DUPLICATE,), 2: (SELECTION_DUPLICATE,)}


def test_validate_selections_compares_duplicates_by_frame():
    """validate_selections는 float 값이 달라도 같은 프레임을 가리키면 중복으로 봐야 한다.

    [(0.1 + 0.2, 1.0), (0.3, 1.0)]
    -> {0: (중복,), 1: (중복,)}
    """
    result = validate_selections([(0.1 + 0.2, 1.0), (0.3, 1.0)], DURATION, FPS)

    assert result == {0: (SELECTION_DUPLICATE,), 1: (SELECTION_DUPLICATE,)}


def test_validate_selections_treats_one_frame_difference_as_distinct():
    """validate_selections는 끝이 1프레임 다른 두 구간을 중복으로 보지 않아야 한다.

    [(1.0, 2.0), (1.0, 2.0 + 1/60)]
    -> {}
    """
    assert validate_selections([(1.0, 2.0), (1.0, 2.0 + FRAME)], DURATION, FPS) == {}


def test_validate_selections_allows_overlapping_ranges():
    """validate_selections는 서로 겹치거나 포함하는 구간을 통과시켜야 한다.

    [(1.0, 5.0), (3.0, 8.0), (1.0, 8.0)]
    -> {}
    """
    assert validate_selections([(1.0, 5.0), (3.0, 8.0), (1.0, 8.0)], DURATION, FPS) == {}


# ================================================================ 개수


def _distinct_ranges(count: int) -> list[tuple[float, float]]:
    """서로 다른 1초짜리 구간 count개 — n초부터 n+1초까지."""
    return [(float(n), float(n + 1)) for n in range(count)]


def test_validate_selections_accepts_max_selections():
    """validate_selections는 구간이 상한 개수이면 통과시켜야 한다.

    서로 다른 구간 MAX_SELECTIONS개
    -> {}
    """
    ranges = _distinct_ranges(MAX_SELECTIONS)

    assert validate_selections(ranges, float(MAX_SELECTIONS + 1), FPS) == {}


def test_validate_selections_flags_ranges_beyond_max_selections():
    """validate_selections는 구간이 상한보다 하나 많으면 마지막 구간을 개수 위반으로 돌려줘야 한다.

    서로 다른 구간 MAX_SELECTIONS + 1개
    -> {MAX_SELECTIONS: (개수,)}
    """
    ranges = _distinct_ranges(MAX_SELECTIONS + 1)

    result = validate_selections(ranges, float(MAX_SELECTIONS + 1), FPS)

    assert result == {MAX_SELECTIONS: (SELECTION_TOO_MANY,)}


# ================================================================ SelectionError (#309)


def test_selection_error_carries_first_violation_key_and_all_violations():
    """SelectionError의 message_key는 번호가 가장 앞선 구간의 첫 위반 키이고 violations는 전부를 담아야 한다.

    violations = {2: (DUPLICATE,), 1: (ORDER, OUT_OF_RANGE)}
    -> message_key == ORDER, violations 그대로, 문자열에 구간 번호 2(1부터 셈)
    """
    violations = {
        2: (SELECTION_DUPLICATE,),
        1: (SELECTION_ORDER, SELECTION_OUT_OF_RANGE),
    }

    error = SelectionError(violations)

    assert error.message_key == SELECTION_ORDER
    assert error.violations == violations
    assert str(error) == f"{SELECTION_ORDER}: 구간 2"


@pytest.mark.parametrize(
    ("end", "duration", "fps", "expected"),
    [
        (10.0, 10.0, 30, True),
        (10.0, 10.02, 30, True),  # 길이 300.6프레임 → 300프레임. 끝도 프레임 300이다
        (9.99, 10.0, 30, True),  # 프레임 299.7 → 300. 프레임 단위로 같다
        (9.9, 10.0, 30, False),  # 프레임 297
        (5.0, 10.0, 30, False),
        (4250.549333, 4250.549333, Fraction(1000, 17), True),
    ],
    ids=["equal", "duration-off-grid", "same-frame", "three-frames-short", "middle", "sample"],
)
def test_reaches_end_compares_end_and_duration_by_frame(end, duration, fps, expected):
    """reaches_end는 구간의 끝과 영상 길이를 프레임 번호로 바꿔 같으면 참을 돌려줘야 한다.

    주석의 경우마다 (끝, 길이, fps)
    -> 끝의 프레임 번호 == 길이의 프레임 수일 때만 True
    """
    assert reaches_end(end, duration, fps) is expected


def test_reaches_end_is_true_only_for_ends_that_validation_accepts_as_the_last():
    """reaches_end가 참인 끝은 검증을 통과하고, 그보다 한 프레임 뒤의 끝은 범위 위반이어야 한다.

    길이 10.02초, 30fps — 끝 10.0초(프레임 300) · 끝 10.0 + 1/30초(프레임 301)
    -> 10.0: reaches_end True · 위반 없음 / 한 프레임 뒤: SELECTION_OUT_OF_RANGE
    """
    assert reaches_end(10.0, 10.02, 30)
    assert validate_selections([(1.0, 10.0)], 10.02, 30) == {}
    assert validate_selections([(1.0, 10.0 + 1 / 30)], 10.02, 30) == {0: (SELECTION_OUT_OF_RANGE,)}
