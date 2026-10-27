"""구간들의 컷 진행을 하나의 값으로 합치는 규칙 (#309) — ``SectionCutProgress``."""

import pytest

from core.downloaders.base import SectionCutProgress


def _tracker(lengths: dict[int, float]) -> tuple[SectionCutProgress, list[float]]:
    published: list[float] = []
    return SectionCutProgress(lengths, published.append), published


def test_sections_are_weighed_by_length():
    """구간 하나가 끝나면 진행은 그 구간의 길이 ÷ 전체 길이만큼 올라야 한다.

    구간 길이 {0: 1, 1: 3}. 0번이 끝남 → 1번이 절반 진행 → 1번이 끝남
    -> 알린 값 == [0.25, 0.625, 1.0]
    """
    tracker, published = _tracker({0: 1.0, 1: 3.0})

    tracker.finish(0)
    tracker.section(1)(0.5)
    tracker.finish(1)

    assert published == pytest.approx([0.25, 0.625, 1.0])


def test_progress_inside_a_section_is_published_as_it_rises():
    """구간 하나를 자르는 동안 오른 진행은 그때마다 알려야 한다.

    구간 하나(길이 10). 진행 0.1 · 0.2 · 0.3
    -> 알린 값 == [0.1, 0.2, 0.3]
    """
    tracker, published = _tracker({7: 10.0})
    report = tracker.section(7)

    for fraction in (0.1, 0.2, 0.3):
        report(fraction)

    assert published == pytest.approx([0.1, 0.2, 0.3])


def test_published_progress_never_goes_back():
    """앞서 알린 값보다 낮은 진행이 와도 알리지 않아야 한다.

    구간 하나. 진행 0.5 → 0.2 → 0.6
    -> 알린 값 == [0.5, 0.6]
    """
    tracker, published = _tracker({0: 4.0})
    report = tracker.section(0)

    for fraction in (0.5, 0.2, 0.6):
        report(fraction)

    assert published == pytest.approx([0.5, 0.6])


def test_one_is_published_only_when_every_section_is_finished():
    """구간의 진행이 1에 닿아도 그 구간이 끝났다고 알리기 전에는 1을 알리지 않아야 한다.

    구간 하나. 진행 1.0 → 끝남
    -> 알린 값 == [0.999, 1.0]
    """
    tracker, published = _tracker({0: 4.0})

    tracker.section(0)(1.0)
    tracker.finish(0)

    assert published == [0.999, 1.0]


def test_steps_smaller_than_the_threshold_are_not_published():
    """아주 조금 오른 진행은 알리지 않아야 한다 — 통지가 ffmpeg가 알리는 횟수만큼 늘지 않는다.

    구간 하나. 진행 0.1000 → 0.1005 → 0.1010 → 0.1030 (알리는 간격 0.002)
    -> 알린 값 == [0.1, 0.103]
    """
    tracker, published = _tracker({0: 1.0})
    report = tracker.section(0)

    for fraction in (0.1, 0.1005, 0.101, 0.103):
        report(fraction)

    assert published == pytest.approx([0.1, 0.103])
