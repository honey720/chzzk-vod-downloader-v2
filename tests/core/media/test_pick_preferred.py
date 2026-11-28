"""기본 해상도 고르기 `pick_preferred` (#646) — 카드의 내림차순 해상도 목록에서 고를 번호."""

import pytest

from core.media.representations import pick_preferred

#: 카드가 보이는 차례(내림차순)의 해상도
RESOLUTIONS = [1080, 720, 360, 144]


def test_no_preference_picks_the_first() -> None:
    """원하는 해상도가 None이면 0번이어야 한다.

    RESOLUTIONS, None -> 0
    """
    assert pick_preferred(RESOLUTIONS, None) == 0


@pytest.mark.parametrize(
    ("preferred", "expected"),
    [
        (1080, 0),
        (720, 1),
        (144, 3),
        (480, 2),  # 480 없음 → 그보다 낮은 것 중 가장 높은 360
        (1440, 0),  # 목록보다 높음 → 그보다 낮은 것 중 가장 높은 1080
    ],
)
def test_preference_picks_the_highest_not_above_it(preferred: int, expected: int) -> None:
    """원하는 해상도가 있으면 그 번호, 없으면 그보다 낮은 것 중 가장 높은 번호여야 한다.

    RESOLUTIONS(1080 · 720 · 360 · 144) -> 1080 → 0, 720 → 1, 144 → 3, 480 → 2, 1440 → 0
    """
    assert pick_preferred(RESOLUTIONS, preferred) == expected


def test_preference_below_every_entry_picks_the_last() -> None:
    """원하는 해상도보다 낮은 항목이 없으면 마지막(가장 낮은) 번호여야 한다.

    [1080, 720, 480], 144 -> 2
    """
    assert pick_preferred([1080, 720, 480], 144) == 2


def test_same_resolution_picks_the_first_of_them() -> None:
    """원하는 해상도의 항목이 둘이면 둘 중 앞 번호여야 한다.

    [1080, 720, 720, 360], 720 -> 1
    """
    assert pick_preferred([1080, 720, 720, 360], 720) == 1


def test_empty_list_picks_zero() -> None:
    """목록이 비면 원하는 해상도와 무관하게 0이어야 한다.

    [], None · 720 -> 0
    """
    assert pick_preferred([], None) == 0
    assert pick_preferred([], 720) == 0
