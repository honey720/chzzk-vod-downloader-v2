"""세그먼트 다운로드의 크기 어림과 여유 공간 비교 — 순수 계산 (#309).

대상: core/downloaders/segment_space.py. 엔진에 연결된 동작은 test_segment_space_engine.py가 잰다.
"""

import pytest

import core.downloaders.segment_space as space_module
from core.downloaders.base import cut_order
from core.downloaders.segment_space import (
    MARGIN_BYTES,
    SectionUse,
    SegmentSpace,
    SpaceNeed,
    check_space,
    required_free,
)

MIB = 1024 * 1024
MEGA = 1_000_000


def _two_seconds(count: int) -> dict[int, float]:
    """인덱스 0부터 count개, 세그먼트마다 2초."""
    return dict.fromkeys(range(count), 2.0)


def test_margin_is_256_mebibytes():
    """늘 더 보는 여유는 256MiB여야 한다.

    -> MARGIN_BYTES == 268,435,456
    """
    assert MARGIN_BYTES == 268_435_456


def test_required_free_adds_ten_percent_and_the_fixed_margin():
    """필요한 여유는 더 쓸 양 × 1.1 + 256MiB여야 한다.

    더 쓸 양 1,000,000 -> 1,100,000 + 268,435,456
    더 쓸 양 -5 (이미 다 받았다) -> 268,435,456
    """
    assert required_free(1_000_000) == 1_100_000 + 256 * MIB
    assert required_free(-5) == 256 * MIB


def test_start_estimate_is_declared_bitrate_times_total_length():
    """시작 전 어림은 선언된 비트레이트 × 받을 세그먼트 길이의 합이어야 한다.

    8,000,000 bit/s, 2초 세그먼트 10개 (20초)
    -> 세그먼트 20,000,000바이트. 구간이 없으면 산출물 · 같은 볼륨의 최대 · 가장 큰 파일도 같은 값
    """
    space = SegmentSpace(_two_seconds(10), bandwidth=8 * MEGA)

    assert space.need(measured=False) == SpaceNeed(
        temp=20 * MEGA, output=20 * MEGA, together=20 * MEGA, largest=20 * MEGA, on_disk=0
    )


@pytest.mark.parametrize("bandwidth", [None, 0])
def test_start_estimate_is_skipped_without_a_declared_bitrate(bandwidth):
    """비트레이트 선언이 없으면 시작 전 어림은 None이어야 한다.

    bandwidth None · 0, 2초 세그먼트 10개 -> None
    """
    assert SegmentSpace(_two_seconds(10), bandwidth=bandwidth).need(measured=False) is None


def test_start_estimate_is_skipped_without_a_bitrate_even_with_segments_on_disk():
    """비트레이트 선언이 없으면 받아 둔 세그먼트가 있어도 시작 전 어림은 None이어야 한다.

    bandwidth None, 2초 세그먼트 10개 중 0번이 5,000,000바이트로 받아져 있음 -> None
    """
    space = SegmentSpace(_two_seconds(10), on_disk={0: 5 * MEGA})

    assert space.need(measured=False) is None


def test_start_estimate_counts_segments_on_disk_at_their_real_size():
    """시작 전 어림은 받아 둔 세그먼트를 실제 크기로, 나머지를 선언된 비트레이트로 세어야 한다.

    8,000,000 bit/s, 2초 세그먼트 10개, 0번이 5,000,000바이트로 받아져 있음
    -> 5,000,000 + 9 x 2,000,000 = 23,000,000, 이미 놓인 양 5,000,000
    """
    space = SegmentSpace(_two_seconds(10), bandwidth=8 * MEGA, on_disk={0: 5 * MEGA})

    need = space.need(measured=False)

    assert (need.temp, need.on_disk) == (23 * MEGA, 5 * MEGA)


def test_transfer_estimate_is_the_real_average_times_the_remaining_length():
    """전송 중 어림은 받은 것의 실제 크기 + 실제 평균(바이트/초) × 남은 길이여야 한다.

    선언 8,000,000 bit/s(1,000,000바이트/초), 2초 세그먼트 40개,
    0~19번을 3,000,000바이트씩 받음 (실제 평균 1,500,000바이트/초)
    -> 60,000,000 + 1,500,000 x 40초 = 120,000,000, 이미 놓인 양 60,000,000
    """
    space = SegmentSpace(_two_seconds(40), bandwidth=8 * MEGA)
    for index in range(20):
        space.received(index, 3 * MEGA)

    need = space.need(measured=True)

    assert (need.temp, need.on_disk) == (120 * MEGA, 60 * MEGA)


def test_transfer_estimate_is_skipped_when_nothing_was_received():
    """받은 세그먼트가 없으면 전송 중 어림은 None이어야 한다 — 선언된 비트레이트로 대신하지 않는다.

    8,000,000 bit/s, 2초 세그먼트 10개, 받은 것 없음 -> need(measured=True) is None
    """
    assert SegmentSpace(_two_seconds(10), bandwidth=8 * MEGA).need(measured=True) is None


def test_segments_outside_the_download_are_ignored():
    """이 다운로드가 쓰지 않는 세그먼트의 크기는 어림에 들어가지 않아야 한다.

    받을 세그먼트 0~9, 99번을 9,000,000바이트로 알림 -> 전송 중 어림 None(받은 것 없음)
    """
    space = SegmentSpace(_two_seconds(10))
    space.received(99, 9 * MEGA)

    assert space.need(measured=True) is None


def test_reestimate_is_due_once_when_twenty_segments_are_known():
    """다시 어림할 때는 크기를 아는 세그먼트가 20개에 이른 뒤 한 번만이어야 한다.

    2초 세그먼트 40개 — 19개 받음 -> 거짓, 20개째 -> 참, 다시 물으면 거짓, 21개째 -> 거짓
    """
    space = SegmentSpace(_two_seconds(40))
    for index in range(19):
        space.received(index, MEGA)
    before = space.reestimate_due()
    space.received(19, MEGA)
    at_twenty = (space.reestimate_due(), space.reestimate_due())
    space.received(20, MEGA)

    assert (before, at_twenty, space.reestimate_due()) == (False, (True, False), False)


def test_reestimate_is_not_due_when_nothing_is_left_to_receive():
    """받을 세그먼트가 남지 않았으면 다시 어림할 때가 아니어야 한다.

    2초 세그먼트 20개를 모두 받음 -> 거짓
    """
    space = SegmentSpace(_two_seconds(20))
    for index in range(20):
        space.received(index, MEGA)

    assert space.reestimate_due() is False


def test_sections_that_share_nothing_peak_at_the_segment_total():
    """겹치지 않는 구간들의 같은 볼륨 최대는 세그먼트의 합이어야 한다.

    1,000,000바이트/초, 2초 세그먼트 10개. 구간 A = 세그먼트 0~4(10초), B = 5~9(10초)
    -> 세그먼트 20,000,000 · 구간 파일 20,000,000 · 최대 20,000,000
       (A 끝: 남은 세그먼트 10,000,000 + 파일 10,000,000)
    """
    space = SegmentSpace(
        _two_seconds(10),
        bandwidth=8 * MEGA,
        sections=[SectionUse(0, 4, 10.0), SectionUse(5, 9, 10.0)],
    )

    need = space.need(measured=False)

    assert (need.temp, need.output, need.together) == (20 * MEGA, 20 * MEGA, 20 * MEGA)


def test_sections_that_share_everything_peak_at_segments_plus_files():
    """전부 겹치는 구간들의 같은 볼륨 최대는 세그먼트의 합 + 구간 파일의 합이어야 한다.

    1,000,000바이트/초, 2초 세그먼트 10개. 구간 둘 다 세그먼트 0~9(20초)
    -> 세그먼트 20,000,000 · 구간 파일 40,000,000 · 최대 40,000,000
       (첫 구간 끝: 세그먼트 20,000,000이 남아 있고 파일 20,000,000)
    """
    space = SegmentSpace(
        _two_seconds(10),
        bandwidth=8 * MEGA,
        sections=[SectionUse(0, 9, 20.0), SectionUse(0, 9, 20.0)],
    )

    need = space.need(measured=False)

    assert (need.temp, need.output, need.together) == (20 * MEGA, 40 * MEGA, 40 * MEGA)


def test_partly_shared_sections_follow_the_order_they_are_made_in():
    """일부가 겹치는 구간들의 최대는 만드는 순서대로 따라간 값이어야 한다.

    1,000,000바이트/초, 2초 세그먼트 10개. 먼저 A = 0~7(16초), 다음 B = 6~9(8초)
    -> A 끝: 남은 세그먼트 6~9 = 8,000,000 + 파일 16,000,000 = 24,000,000
       B 끝: 0 + 파일 24,000,000 -> 최대 24,000,000
    """
    space = SegmentSpace(
        _two_seconds(10),
        bandwidth=8 * MEGA,
        sections=[SectionUse(0, 7, 16.0), SectionUse(6, 9, 8.0)],
    )

    need = space.need(measured=False)

    assert (need.temp, need.output, need.together) == (20 * MEGA, 24 * MEGA, 24 * MEGA)


def test_section_file_is_the_share_of_its_segments_that_the_section_covers():
    """구간 파일의 어림은 그 구간의 세그먼트 크기 × (구간 길이 ÷ 세그먼트 길이의 합)이어야 한다.

    1,000,000바이트/초, 2초 세그먼트 5개(10초, 10,000,000바이트), 구간 길이 5초
    -> 구간 파일 5,000,000 · 최대 10,000,000(후처리를 시작할 때의 세그먼트)
    """
    space = SegmentSpace(_two_seconds(5), bandwidth=8 * MEGA, sections=[SectionUse(0, 4, 5.0)])

    need = space.need(measured=False)

    assert (need.output, need.together) == (5 * MEGA, 10 * MEGA)


def test_largest_file_of_a_section_download_is_its_biggest_section_file():
    """구간 다운로드의 가장 큰 파일은 구간 파일 어림 가운데 가장 큰 것이어야 한다 — 합이 아니다.

    1,000,000바이트/초, 2초 세그먼트 10개. 구간 A = 세그먼트 0~7(16초), B = 6~9(8초)
    -> 구간 파일 16,000,000 · 8,000,000 -> 가장 큰 파일 16,000,000 (산출물의 합은 24,000,000)
    """
    space = SegmentSpace(
        _two_seconds(10),
        bandwidth=8 * MEGA,
        sections=[SectionUse(0, 7, 16.0), SectionUse(6, 9, 8.0)],
    )

    need = space.need(measured=False)

    assert (need.largest, need.output) == (16 * MEGA, 24 * MEGA)


def test_cut_order_is_longest_first_then_by_number():
    """구간을 만드는 순서는 긴 구간부터, 길이가 같으면 번호순이어야 한다.

    {1: 5.0, 2: 9.0, 3: 5.0} -> [2, 1, 3]
    """
    assert cut_order({1: 5.0, 2: 9.0, 3: 5.0}) == [2, 1, 3]


def _volumes(monkeypatch, same: bool, free: dict[str, int]) -> None:
    """볼륨 판정과 여유 공간을 정해 둔다 — free는 폴더 → 여유 바이트."""
    monkeypatch.setattr(space_module, "same_volume", lambda a, b: same)
    monkeypatch.setattr(space_module, "free_bytes", lambda folder: free[folder])


def test_same_volume_is_checked_once_against_the_combined_peak(monkeypatch):
    """두 폴더가 같은 볼륨이면 (같은 볼륨 최대 − 이미 놓인 양)으로 한 번만 견줘야 한다.

    임시 100 · 산출물 70 · 최대 150 · 이미 놓인 양 30 (단위 1,000,000바이트), 여유 500MiB
    -> 결과 1건: both, 필요 (150 − 30) x 1.1 + 256MiB = 132,000,000 + 268,435,456, 모자라지 않음
    """
    _volumes(monkeypatch, same=True, free={"out": 500 * MIB})
    need = SpaceNeed(
        temp=100 * MEGA, output=70 * MEGA, together=150 * MEGA, largest=70 * MEGA, on_disk=30 * MEGA
    )

    checks = check_space(need, "temp", "out")

    assert [(c.where, c.needed, c.free, c.short) for c in checks] == [
        ("both", 132 * MEGA + 256 * MIB, 500 * MIB, False)
    ]


def test_different_volumes_are_checked_separately(monkeypatch):
    """두 폴더가 다른 볼륨이면 임시 폴더와 산출물 폴더를 각각 견줘야 한다.

    임시 100 · 산출물 70 · 이미 놓인 양 30 (단위 1,000,000바이트)
    -> temp: (100 − 30) x 1.1 + 256MiB, output: 70 x 1.1 + 256MiB
    """
    _volumes(monkeypatch, same=False, free={"temp": 10**12, "out": 10**12})
    need = SpaceNeed(
        temp=100 * MEGA, output=70 * MEGA, together=150 * MEGA, largest=70 * MEGA, on_disk=30 * MEGA
    )

    checks = check_space(need, "temp", "out")

    assert [(c.where, c.needed) for c in checks] == [
        ("temp", 77 * MEGA + 256 * MIB),
        ("output", 77 * MEGA + 256 * MIB),
    ]


def test_check_is_short_only_below_the_needed_bytes(monkeypatch):
    """여유 공간이 필요한 양보다 1바이트라도 적을 때만 모자란 것이어야 한다.

    필요 = 1,000,000 x 1.1 + 256MiB. 여유가 그 값 -> 모자라지 않음, 그 값 − 1 -> 모자람
    """
    need = SpaceNeed(temp=MEGA, output=MEGA, together=MEGA, largest=MEGA, on_disk=0)
    needed = 1_100_000 + 256 * MIB

    _volumes(monkeypatch, same=True, free={"out": needed})
    enough = check_space(need, "temp", "out")[0].short
    _volumes(monkeypatch, same=True, free={"out": needed - 1})
    short = check_space(need, "temp", "out")[0].short

    assert (enough, short) == (False, True)


def test_volume_whose_free_space_is_unknown_is_not_checked(monkeypatch):
    """여유 공간을 알 수 없는 볼륨은 결과에 없어야 한다.

    free_bytes가 OSError -> 결과 0건
    """

    def unknown(folder):
        raise OSError("알 수 없음")

    monkeypatch.setattr(space_module, "same_volume", lambda a, b: True)
    monkeypatch.setattr(space_module, "free_bytes", unknown)
    need = SpaceNeed(temp=MEGA, output=MEGA, together=MEGA, largest=MEGA, on_disk=0)

    assert check_space(need, "temp", "out") == ()


def test_free_bytes_measures_the_nearest_folder_that_exists(tmp_path):
    """아직 없는 폴더의 여유 공간은 실제로 있는 윗폴더에서 재야 한다.

    tmp_path/없음/더없음 -> 예외 없이 tmp_path의 여유 공간과 같은 볼륨의 값(0보다 큼)
    """
    missing = tmp_path / "없음" / "더없음"

    assert space_module.free_bytes(str(missing)) > 0
