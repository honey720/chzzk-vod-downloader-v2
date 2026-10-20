"""단계 여럿을 막대 하나로 보이는 계산기 — 남은 몫 재배분 (#390)."""

import pytest

from app.viewmodels.staged_progress import StagedProgress

STAGES = ("transfer", "cut")


def test_bar_rises_by_the_work_done_measured_in_expected_seconds():
    """막대는 (100 − 지금 값) × 이번에 한 일 ÷ 남은 일만큼 올라야 한다.

    받기 예상 80초 · 컷 예상 20초. 받기 0.5 → 받기 1 → 컷 0.5 → 컷 1
    -> [40, 80, 90, 100]
    """
    bar = StagedProgress(STAGES)
    seconds = {"transfer": 80.0, "cut": 20.0}

    seen = [
        bar.update({"transfer": 0.5}, seconds),
        bar.update({"transfer": 1.0}, seconds),
        bar.update({"transfer": 1.0, "cut": 0.5}, seconds),
        bar.update({"transfer": 1.0, "cut": 1.0}, seconds),
    ]

    assert seen == [40, 80, 90, 100]


def test_bar_stands_still_when_no_work_was_done():
    """한 일이 그대로인 통지는 예상 시간이 바뀌어도 막대를 움직이지 않아야 한다.

    받기 예상 80초 · 컷 20초에서 받기 0.5(40) → 같은 일로 받기 예상 40 · 160 · 80초인 통지 셋
    -> 막대 == 40, 40, 40, 40
    """
    bar = StagedProgress(STAGES)
    seen = [bar.update({"transfer": 0.5}, {"transfer": 80.0, "cut": 20.0})]

    for transfer_seconds in (40.0, 160.0, 80.0):
        seen.append(bar.update({"transfer": 0.5}, {"transfer": transfer_seconds, "cut": 20.0}))

    assert seen == [40, 40, 40, 40]


def test_work_that_goes_back_is_taken_as_the_work_seen_before():
    """앞서 받은 것보다 작은 한 일이 와도 막대는 내려가지 않고, 그 뒤의 일은 앞서 받은 값부터 센다.

    받기 예상 80초 · 컷 20초. 받기 0.5 → 0.2 → 0.75
    -> [40, 40, 60]
    """
    bar = StagedProgress(STAGES)
    seconds = {"transfer": 80.0, "cut": 20.0}

    seen = [bar.update({"transfer": value}, seconds) for value in (0.5, 0.2, 0.75)]

    assert seen == [40, 40, 60]


@pytest.mark.parametrize("cut_seconds", [20.0, 60.0, 180.0, 1.0])
def test_last_stage_alone_fills_what_is_left_by_its_work_whatever_its_estimate(cut_seconds):
    """남은 단계가 하나면 그 단계의 예상 시간이 얼마든 그 단계의 한 일만큼 남은 몫을 채워야 한다.

    받기 예상 80초 · 컷 예상 20초로 받기를 끝냄(80). 그 뒤 컷 예상을 20 · 60 · 180 · 1초로 바꾸고
    컷 0.25 · 0.5 · 0.75 · 1
    -> [85, 90, 95, 100]
    """
    bar = StagedProgress(STAGES)
    bar.update({"transfer": 1.0}, {"transfer": 80.0, "cut": 20.0})

    seen = [
        bar.update({"transfer": 1.0, "cut": value}, {"transfer": 80.0, "cut": cut_seconds})
        for value in (0.25, 0.5, 0.75, 1.0)
    ]

    assert seen == [85, 90, 95, 100]


def test_bar_shows_100_only_when_every_stage_is_done():
    """막대는 모든 단계의 일이 끝났을 때만 100이고, 그 전에는 거의 다 해도 99여야 한다.

    받기 예상 1초 · 컷 예상 1000초. 컷만 1 · 받기 0.9999 → 받기 1
    -> [99, 100]
    """
    bar = StagedProgress(STAGES)
    seconds = {"transfer": 1.0, "cut": 1000.0}

    almost = bar.update({"transfer": 0.9999, "cut": 1.0}, seconds)
    done = bar.update({"transfer": 1.0, "cut": 1.0}, seconds)

    assert (almost, done) == (99, 100)


def test_stage_without_an_estimate_takes_no_share_of_the_bar():
    """예상 시간이 0인 단계(받을 것이 없는 받기)는 몫이 없어, 다른 단계의 일만으로 0에서 100까지 차야 한다.

    받기 예상 0초(받기는 처음부터 1) · 컷 예상 20초. 받기 1 → 컷 0.5 → 컷 1
    -> [0, 50, 100]
    """
    bar = StagedProgress(STAGES)
    seconds = {"transfer": 0.0, "cut": 20.0}

    seen = [
        bar.update({"transfer": 1.0}, seconds),
        bar.update({"transfer": 1.0, "cut": 0.5}, seconds),
        bar.update({"transfer": 1.0, "cut": 1.0}, seconds),
    ]

    assert seen == [0, 50, 100]


def test_changed_estimate_changes_only_the_slope_from_then_on():
    """두 단계가 남은 동안 예상이 바뀌어도 막대는 뛰거나 줄지 않고, 그때부터의 기울기만 바뀌어야 한다.

    컷 예상 20초. 받기 예상 80초로 받기 0.5(40) → 받기 예상이 120초로 늘고 받기 0.75
    -> 둘째 값 == int(40 + 60 × (0.25 × 120) ÷ (0.5 × 120 + 20)) == 62 (줄지 않고, 뛰지도 않는다)
    """
    bar = StagedProgress(STAGES)
    first = bar.update({"transfer": 0.5}, {"transfer": 80.0, "cut": 20.0})

    second = bar.update({"transfer": 0.75}, {"transfer": 120.0, "cut": 20.0})

    assert (first, second) == (40, 62)


def test_stage_names_must_not_repeat():
    """같은 단계 이름을 두 번 주면 계산기를 만들지 않아야 한다."""
    with pytest.raises(ValueError):
        StagedProgress(("transfer", "transfer"))
