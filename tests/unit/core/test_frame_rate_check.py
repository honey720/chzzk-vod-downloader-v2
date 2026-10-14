"""선언 프레임률과 실제 PTS 대조(core/utils/timecode.py::check_frame_rate) 단위 테스트 (#309).

핵심 계약:
- 첫 프레임에 맞춘 선언 프레임률의 격자에 프레임을 놓고, 비는 자리와 가장 큰 어긋남을 잰다
- 판정은 하지 않는다 — 잰 값만 돌려준다
"""

from fractions import Fraction

import pytest

from core.utils.timecode import check_frame_rate


def test_check_frame_rate_reports_clean_match_for_uniform_frames():
    """check_frame_rate는 간격이 일정한 프레임에서 빈 자리 0, 어긋남 0에 가까운 값을 돌려줘야 한다.

    PTS = n/60 (600개), fps = 60
    -> missing_frames == 0, max_grid_offset < 0.001, average_fps == 60, frames == 600
    """
    result = check_frame_rate(60, [n / 60 for n in range(600)])

    assert result.missing_frames == 0
    assert result.max_grid_offset < 0.001
    assert result.average_fps == pytest.approx(60)
    assert (result.declared, result.frames) == (Fraction(60), 600)


def test_check_frame_rate_measures_jitter_as_grid_offset():
    """check_frame_rate는 간격이 17·17·16ms로 반복되는 프레임에서 빈 자리 없이 작은 어긋남을 돌려줘야 한다.

    PTS = 0, 17, 34, 50, 67, 84 … ms (300개), fps = 60
    -> missing_frames == 0, 0.01 < max_grid_offset < 0.05 (17ms는 1/60초보다 0.33ms 길다 = 0.02프레임)
    """
    pts = [((n // 3) * 50 + (0, 17, 34)[n % 3]) / 1000 for n in range(300)]

    result = check_frame_rate(60, pts)

    assert result.missing_frames == 0
    assert 0.01 < result.max_grid_offset < 0.05


def test_check_frame_rate_counts_missing_frames():
    """check_frame_rate는 프레임이 빠진 자리 수를 돌려줘야 한다.

    60fps PTS 10개에서 4·5번 프레임을 뺀 목록
    -> missing_frames == 2, frames == 8
    """
    result = check_frame_rate(60, [n / 60 for n in (0, 1, 2, 3, 6, 7, 8, 9)])

    assert (result.missing_frames, result.frames) == (2, 8)


@pytest.mark.parametrize(
    ("fps", "low", "high"),
    [
        (Fraction(2997, 100), 0.0, 0.001),  # 원본과 같은 프레임률 — 어긋남 없음
        (
            Fraction(30000, 1001),
            0.35,
            0.5,
        ),  # 100만 분의 1 다른 프레임률 — 40만 프레임에서 0.4프레임
    ],
)
def test_check_frame_rate_shows_drift_of_slightly_different_rate(fps, low, high):
    """check_frame_rate는 선언 프레임률이 실제와 100만 분의 1 다르면 그만큼 커진 어긋남을 돌려줘야 한다.

    PTS = n × 400/11988 (n = 0 … 400,000, 1,000프레임마다 하나), fps = 2997/100 · 30000/1001
    -> max_grid_offset < 0.001 · 0.35 ~ 0.5
    """
    pts = [float(Fraction(n * 400, 11988)) for n in range(0, 400_001, 1000)]

    result = check_frame_rate(fps, pts)

    assert low <= result.max_grid_offset < high


@pytest.mark.parametrize("pts", [[], [1.0], [2.0, 2.0]])
def test_check_frame_rate_rejects_too_few_frames(pts):
    """check_frame_rate는 프레임이 2개 미만이거나 첫 PTS와 마지막 PTS가 같으면 ValueError를 내야 한다.

    [], [1.0], [2.0, 2.0]
    -> ValueError
    """
    with pytest.raises(ValueError):
        check_frame_rate(60, pts)
