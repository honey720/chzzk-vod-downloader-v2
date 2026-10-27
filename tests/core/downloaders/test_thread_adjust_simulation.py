"""회선 모양별 스레드 조정 시뮬레이션 (#347).

회선을 "스레드 수 → 처리량" 곡선과 틱마다의 배율(출렁임)로 흉내 내어 조정 규칙에 틱 단위로
넣는다. 실제 시간 · 네트워크를 쓰지 않고, 배율은 시드를 고정한 난수로 만든다.

    처리량(틱) = min(스레드 수 × 연결당 상한, 대역 × 배율(틱))

출렁이는 것은 회선의 대역이다 — 연결당 상한에 묶여 있는 동안(회선이 차기 전)에는 대역이 조금
떨어져도 처리량이 그대로다.

연결당 상한은 1 MB/s다 — 대역 20 MB/s면 스레드 20개에서 회선이 찬다(그 위로는 올려도 늘지 않는다).

출렁임의 모양(보정한 출렁임): 틱마다 배율을 넓게 뽑는다 — 4%는 0.02~0.10, 19%는 0.25~0.50,
31%는 0.50~0.75, 11%는 0.75~0.90, 35%는 0.90~1.00
"""

import random
from collections.abc import Callable

import pytest

from core.downloaders.file_downloader import FileDownloader
from core.models.download_data import DownloadData

CAP = 1.0  # 연결당 상한(MB/s)
SEEDS = (1, 2, 3)
WARMUP = 80  # 이 틱까지는 올라가는 구간으로 보고 머문 범위의 판정에서 뺀다
# 보정한 출렁임의 배율 분포 — (누적 확률, 하한, 상한)
CALIBRATED = (
    (0.04, 0.02, 0.10),
    (0.23, 0.25, 0.50),
    (0.54, 0.50, 0.75),
    (0.65, 0.75, 0.90),
    (1.0, 0.90, 1.0),
)


class QuietLogger:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def steady(_rng: random.Random) -> float:
    return 1.0


def calibrated(rng: random.Random) -> float:
    pick = rng.random()
    low, high = next((lo, hi) for edge, lo, hi in CALIBRATED if pick <= edge)
    return rng.uniform(low, high)


def symmetric(rng: random.Random) -> float:
    """평균 1 · 표준편차 15%의 대칭 흔들림 — 대역을 고정한 회선에서 틱 측정값이 흔들리는 모양."""
    return min(1.6, max(0.4, rng.gauss(1.0, 0.15)))


def simulate(
    band: Callable[[int], float],
    jitter: Callable[[random.Random], float],
    ticks: int,
    seed: int = 1,
    cap: float = CAP,
) -> tuple[list[int], list[float]]:
    """ticks틱 동안 조정 규칙을 돌려 (틱마다의 목표 스레드 수, 틱마다의 처리량)을 돌려준다.

    Args:
        band: 틱 번호 → 그 틱의 대역(MB/s)
        jitter: 난수 → 그 틱의 배율
        cap: 연결당 상한(MB/s)
    """
    data = DownloadData(
        base_url="https://example.invalid/video.mp4",
        vod_url="https://chzzk.naver.com/video/1",
        output_path="unused.part",
        resolution=1080,
        content_type="video",
    )
    data.max_threads = 6996
    scaler = FileDownloader(data, QuietLogger())
    clock = [0.0]
    scaler._now = lambda: clock[0]
    rng = random.Random(seed)
    targets, speeds = [], []
    for tick in range(ticks):
        clock[0] = float(tick + 1)  # 틱은 1초 간격이다
        speed = min(data.adjust_threads * cap, band(tick) * jitter(rng))
        data.speed_mb = speed
        scaler._adjust_threads()
        targets.append(data.adjust_threads)
        speeds.append(speed)
    return targets, speeds


def mean(values) -> float:
    values = list(values)
    return sum(values) / len(values)


# ================================================================ 1 · 2. 출렁임 없는 회선


def test_target_reaches_the_cap_when_the_line_never_fills():
    """회선이 차지 않으면(스레드를 올릴수록 처리량이 늘면) 목표가 상한 48에 닿아야 한다.

    대역 무한, 출렁임 없음, 120틱 -> 마지막 목표 == 48
    """
    targets, _speeds = simulate(lambda tick: float("inf"), steady, 120)

    assert targets[-1] == 48


def test_target_stays_just_above_the_fill_point_on_a_steady_line():
    """출렁임 없는 회선에서는 목표가 회선이 찬 지점(20)과 그 위 두 칸(28) 사이에 머물러야 한다.

    대역 20 MB/s(스레드 20개에서 참), 출렁임 없음, 400틱 -> 80틱 뒤의 목표가 모두 20 ~ 28
    """
    targets, _speeds = simulate(lambda tick: 20.0, steady, 400)

    assert min(targets[WARMUP:]) >= 20
    assert max(targets[WARMUP:]) <= 28


# ================================================================ 3. 출렁이는 회선


@pytest.mark.parametrize("seed", SEEDS)
def test_target_is_not_stuck_far_below_the_fill_point_on_a_noisy_line(seed):
    """출렁이는 회선에서 목표가 회선이 찬 지점보다 한참 아래에 갇혀서는 안 된다.

    대역 20 MB/s(스레드 20개에서 참), 보정한 출렁임, 600틱 -> 80틱 뒤의 평균 목표 >= 12
    """
    targets, _speeds = simulate(lambda tick: 20.0, calibrated, 600, seed)

    assert mean(targets[WARMUP:]) >= 12


@pytest.mark.parametrize("seed", SEEDS)
def test_target_stays_near_the_fill_point_on_a_noisy_line(seed):
    """출렁이는 회선에서도 목표가 회선이 찬 지점 근처에 머물고, 뒤로 갈수록 오르지 않아야 한다.

    대역 20 MB/s(스레드 20개에서 참), 보정한 출렁임, 1800틱
    -> 80틱 뒤의 틱 가운데 90% 이상에서 목표 <= 28
    -> 마지막 1/3의 평균 목표 <= 가운데 1/3의 평균 목표 + 4
    """
    targets, _speeds = simulate(lambda tick: 20.0, calibrated, 1800, seed)
    settled = targets[WARMUP:]
    third = len(settled) // 3

    assert sum(target <= 28 for target in settled) / len(settled) >= 0.9
    assert mean(settled[2 * third :]) <= mean(settled[third : 2 * third]) + 4


@pytest.mark.parametrize("seed", SEEDS)
def test_target_stays_low_when_a_few_connections_fill_a_fixed_line(seed):
    """연결 몇 개로 차는 고정 대역에서는, 틱 속도가 흔들려도 목표가 찬 지점 근처에 머물러야 한다.

    대역 22 MB/s · 연결당 2.75 MB/s(스레드 8개에서 참), 틱 ±15% 흔들림, 1800틱
    -> 80틱 뒤의 틱 가운데 90% 이상에서 목표 <= 16
    -> 80틱 뒤의 평균 목표 <= 14
    """
    targets, _speeds = simulate(lambda tick: 22.0, symmetric, 1800, seed, cap=2.75)
    settled = targets[WARMUP:]

    assert sum(target <= 16 for target in settled) / len(settled) >= 0.9
    assert mean(settled) <= 14


def test_target_stays_at_the_fill_point_when_a_few_connections_fill_a_steady_line():
    """연결 몇 개로 차는 일정한 대역에서는 목표가 찬 지점(8)과 그 위 한 칸(12) 사이에 머물러야 한다.

    대역 22 MB/s · 연결당 2.75 MB/s(스레드 8개에서 참), 출렁임 없음, 600틱
    -> 80틱 뒤의 목표가 모두 8 ~ 12
    """
    targets, _speeds = simulate(lambda tick: 22.0, steady, 600, cap=2.75)

    assert min(targets[WARMUP:]) >= 8
    assert max(targets[WARMUP:]) <= 12


# ================================================================ 4 · 5. 대역이 바뀌는 회선


def test_target_survives_a_drop_and_the_throughput_recovers():
    """대역이 잠깐 크게 떨어졌다 돌아오면 목표가 무너지지 않고 처리량이 곧 회복돼야 한다.

    대역 20 MB/s, 틱 200 ~ 219에서 4 MB/s(20%), 그 뒤 다시 20 MB/s. 출렁임 없음, 300틱
    -> 떨어진 동안의 목표 >= 4
    -> 돌아온 뒤 20틱 안에 처리량이 떨어지기 전(틱 199)의 90% 이상
    """
    targets, speeds = simulate(lambda tick: 4.0 if 200 <= tick < 220 else 20.0, steady, 300)

    assert min(targets[200:220]) >= 4
    assert max(speeds[220:240]) >= speeds[199] * 0.9


def test_target_climbs_again_when_the_line_gets_wider():
    """대역이 넓어지면 목표가 새로 찬 지점 근처까지 다시 올라가야 한다.

    대역 20 MB/s → 틱 200부터 40 MB/s(스레드 40개에서 참). 출렁임 없음, 320틱
    -> 넓어진 뒤 100틱 안에 목표 >= 36
    """
    targets, _speeds = simulate(lambda tick: 20.0 if tick < 200 else 40.0, steady, 320)

    assert max(targets[200:300]) >= 36
