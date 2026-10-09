"""목표 스레드 조정기의 판정 (#347) — 표본 묶음 견주기 · 아래 수준 재확인 · 재탐침 간격.

조정기에 1초 간격의 틱을 넣는다. 실제 시간과 네트워크는 쓰지 않는다. 틱의 속도는 "지금 목표가
몇일 때 얼마가 나오는가"로 준다.
"""

from collections.abc import Callable, Iterable, Iterator

from core.downloaders.thread_control import ThreadController


class Line:
    """조정기에 틱을 넣는 도우미 — 목표별 속도를 정해 두고 돌린다."""

    def __init__(self, controller: ThreadController):
        self.controller = controller
        self.now = 0.0
        self.start = controller.target
        self.targets: list[int] = []  # 틱마다, 그 틱을 넣은 뒤의 목표

    def tick(self, speed: float) -> None:
        self.now += 1.0
        self.controller.step(self.now, speed)
        self.targets.append(self.controller.target)

    def run(self, speed_at: Callable[[int], float], ticks: int) -> None:
        for _ in range(ticks):
            self.tick(speed_at(self.controller.target))

    def raises(self) -> list[int]:
        """목표가 오른 틱 번호(1부터)."""
        previous = [self.start, *self.targets[:-1]]
        return [
            index + 1
            for index, (before, after) in enumerate(zip(previous, self.targets))
            if after > before
        ]


def cycle(values: Iterable[float]) -> Iterator[float]:
    values = list(values)
    while True:
        yield from values


def _line_probing_12_with_a_full_window(low: Callable[[], float]) -> Line:
    """목표 8에서 묶음(10초)을 다 채운 뒤 12로 막 올린 상태를 만든다. 8에서의 속도는 low()다.

    틱 1에 8→12, 표본 둘이 뚜렷하지 않아 틱 5에 8로 돌아간다. 틱 8~17에 8의 표본 10개를 모으고
    틱 17에 다시 12로 올린다. 그 뒤 2틱은 재지 않는다
    """
    line = Line(ThreadController(8, 48))
    for _ in range(17):
        line.tick(low())
    assert line.controller.target == 12, "전제: 묶음을 채운 뒤 12로 올렸다"
    line.tick(0.0)
    line.tick(0.0)
    return line


# ================================================================ 순위 판정


def test_raise_is_accepted_when_three_samples_beat_the_lower_peak():
    """올린 수준의 표본 3개가 아래 수준의 최댓값 + 요구폭을 넘으면 인상을 받아들여야 한다.

    8에서 10.0 고정 → 12에서 10.0, 10.0, 10.8, 10.9, 11.0 (요구치 10.0 × 1.0625 = 10.625)
    -> 다섯째 표본 뒤에도 12이고, 이어서 10틱을 더 돌려도 8로 돌아가지 않는다
    """
    line = _line_probing_12_with_a_full_window(lambda: 10.0)

    for speed in (10.0, 10.0, 10.8, 10.9, 11.0):
        line.tick(speed)
    held = line.controller.target
    line.run(lambda target: 11.0, 6)

    assert held == 12
    assert min(line.targets[-6:]) >= 12


def test_raise_is_reverted_when_fewer_than_three_samples_beat_the_lower_peak():
    """10초 동안 요구치를 넘은 표본이 2개뿐이고 평균도 그대로면 인상을 되물려야 한다.

    8에서 9.0과 10.0이 번갈아 → 12에서 10.8 두 번, 나머지 8개는 9.0과 10.0이 번갈아
    -> 10초 뒤 목표 8
    """
    low = cycle((9.0, 10.0))
    line = _line_probing_12_with_a_full_window(lambda: next(low))

    for speed in (10.8, 10.8, 9.0, 10.0, 9.0, 10.0, 9.0, 10.0, 9.0, 10.0):
        line.tick(speed)

    assert line.controller.target == 8


def test_two_clearly_higher_samples_are_accepted_at_once_and_the_climb_goes_on():
    """올린 뒤 처음 두 표본이 모두 뚜렷이 높으면(선형 기대의 절반 이상) 바로 받아들이고 또 올려야 한다.

    8에서 10.0 → 12에서 13.0, 13.0 (뚜렷함의 문턱 10.0 × 1.25 = 12.5)
    -> 둘째 표본의 틱에 목표 16
    """
    line = _line_probing_12_with_a_full_window(lambda: 10.0)

    line.tick(13.0)
    line.tick(13.0)

    assert line.controller.target == 16


# ================================================================ 평균 차이 판정


def test_raise_is_accepted_when_the_mean_is_clearly_higher_without_beating_the_peak():
    """아래 수준에 튄 값이 하나 있어 최댓값을 못 넘어도, 평균이 뚜렷이 높으면 받아들여야 한다.

    8에서 10.0 아홉 번 + 12.0 한 번(요구치 12.75) → 12에서 11.5 열 번
    -> 10초 뒤에도 12 (평균 10.2 → 11.5, 표준오차의 2.5배를 넘는다)
    """
    low = cycle((10.0, 10.0, 12.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0))
    line = _line_probing_12_with_a_full_window(lambda: next(low))

    line.run(lambda target: 11.5, 10)

    assert line.controller.target == 12


def test_raise_is_reverted_when_the_mean_gain_is_within_the_noise():
    """평균이 요구폭보다 늘었어도 그 차이가 흔들림에 묻히면 되물려야 한다.

    8에서 8.0과 12.0이 번갈아(평균 10.0) → 12에서 9.0과 12.7이 번갈아(평균 10.85, 요구 10.625)
    -> 10초 뒤 목표 8 (차이 0.85는 표준오차 약 0.9의 2.5배에 못 미친다)
    """
    low = cycle((8.0, 12.0))
    line = _line_probing_12_with_a_full_window(lambda: next(low))

    high = cycle((9.0, 12.7))
    for _ in range(10):
        line.tick(next(high))

    assert line.controller.target == 8


# ================================================================ 재탐침 간격


def test_reprobe_gap_doubles_after_each_failed_raise_up_to_two_minutes():
    """올려도 늘지 않으면 되물린 뒤 30초 · 60초 · 120초 · 120초에 다시 올려 봐야 한다.

    속도 10.0 고정(목표와 무관), 시작 목표 4, 400틱
    -> 목표가 오른 틱: 1, 17(묶음을 채운 뒤), 59, 131, 263, 395
       (되물린 틱 29 · 71 · 143 · 275에서 각각 30 · 60 · 120 · 120초 뒤)
    """
    line = Line(ThreadController(4, 48))

    line.run(lambda target: 10.0, 400)

    assert line.raises() == [1, 17, 59, 131, 263, 395]


def test_reprobe_gap_starts_over_after_an_accepted_raise():
    """인상이 받아들여지면 재탐침 간격이 30초로 돌아가야 한다.

    대역 4(연결당 1)에서 4에 머물다(되물림 틱 29 · 71 → 다음 재탐침은 60초 뒤인 틱 131),
    틱 101부터 대역 12
    -> 틱 131의 재탐침이 통과해 8 · 12로 오르고 16을 올려 본다(틱 139, 묶음을 채운 뒤 155)
    -> 16이 틱 167에 되물린 뒤 30초(틱 197) · 그다음은 60초(틱 269)에 다시 올려 본다
    """
    line = Line(ThreadController(4, 48))
    line.run(lambda target: 4.0, 100)
    assert line.controller.target == 4

    line.run(lambda target: float(min(target, 12)), 200)

    assert [tick for tick in line.raises() if tick > 100] == [131, 135, 139, 155, 197, 269]
    assert line.targets[167 - 1] == 12  # 틱 167에 되물렸다


# ================================================================ 아래 수준과 다시 견주기


def _marginal_then_flat(target: int, high: Iterator[float]) -> float:
    return 10.0 if target <= 8 else next(high)


def test_marginally_accepted_level_is_dropped_when_the_lower_level_is_as_fast():
    """뚜렷하지 않게 받아들인 수준은 아래 수준과 다시 견줘, 차이가 없으면 내려앉아야 한다.

    8에서 10.0 → 12에서 처음 다섯 표본만 10.0, 10.0, 10.8, 10.9, 11.0(우연한 통과), 그 뒤 8 이상은
    모두 10.0(4에서는 5.0)
    -> 200틱 뒤 목표 8
    """
    line = _line_probing_12_with_a_full_window(lambda: 10.0)
    for speed in (10.0, 10.0, 10.8, 10.9, 11.0):
        line.tick(speed)
    assert line.controller.target == 12, "전제: 우연히 통과했다"

    line.run(lambda target: 10.0 if target >= 8 else 5.0, 200)

    assert line.controller.target == 8
    assert 16 in line.targets  # 12에서 16도 올려 봤다(되물림)


def test_marginally_accepted_level_is_kept_when_it_really_is_faster():
    """뚜렷하지 않게 받아들였어도 아래 수준보다 정말 빠르면, 다시 견준 뒤 그 수준으로 돌아와야 한다.

    8에서 10.0 고정, 12 이상에서 10.0과 11.0이 2:3으로 섞임(요구치 10.625를 넘는 표본이 절반 넘음)
    -> 300틱 동안 8로 내려갔다가(다시 견주기) 12로 돌아오고, 마지막 목표는 12
    """
    high = cycle((10.0, 11.0, 11.0, 10.0, 11.0))
    line = Line(ThreadController(8, 48))

    line.run(lambda target: _marginal_then_flat(target, high), 300)
    settled = line.targets[60:]

    assert 8 in settled, "아래 수준으로 내려가 다시 견줘야 한다"
    assert line.controller.target == 12
    assert max(settled) <= 16


def test_clearly_accepted_level_is_not_rechecked():
    """뚜렷이 받아들인 수준은 아래 수준으로 내려가 보지 않아야 한다 — 내려가 있는 동안 처리량을 잃는다.

    대역 8(연결당 1): 4→8은 두 배(뚜렷), 8→12는 그대로. 600틱
    -> 처음 30틱 뒤로 목표가 8 아래로 내려가지 않는다
    """
    line = Line(ThreadController(4, 48))

    line.run(lambda target: float(min(target, 8)), 600)

    assert min(line.targets[30:]) == 8
    assert max(line.targets[30:]) == 12


def test_clear_level_is_rechecked_once_the_speed_falls_to_what_the_lower_level_gave():
    """뚜렷이 받아들인 수준이라도, 지금 속도가 아래 수준에서 냈던 값을 못 넘게 되면 다시 견줘 내려와야 한다.

    대역 8(연결당 1)에서 8에 머물다가 틱 100부터 대역 4.2(절반 미만은 아니라 붕괴 규칙은 안 걸린다)
    -> 틱 200에 목표 4
    """
    line = Line(ThreadController(4, 48))
    line.run(lambda target: float(min(target, 8)), 100)
    assert line.controller.target in (8, 12)

    line.run(lambda target: min(float(target), 4.2), 100)

    assert line.controller.target == 4


# ================================================================ 상한 · 시작 · 일시정지


def test_cap_below_the_start_target_never_moves():
    """상한이 시작 목표와 같으면 목표가 바뀌지 않아야 한다.

    시작 2 · 상한 2, 속도 5.0으로 100틱 -> 모든 틱에서 목표 2
    """
    line = Line(ThreadController(2, 2))

    line.run(lambda target: 5.0, 100)

    assert set(line.targets) == {2}


def test_zero_speed_before_the_first_measurement_is_ignored():
    """첫 유효 측정 전의 속도 0은 표본으로 치지 않아야 한다.

    속도 0으로 5틱 → 3.9 한 틱 -> 0인 동안 목표 4, 3.9인 틱에 목표 8
    """
    line = Line(ThreadController(4, 48))

    for _ in range(5):
        line.tick(0.0)
    waiting = line.controller.target
    line.tick(3.9)

    assert waiting == 4
    assert line.controller.target == 8


def test_shift_moves_the_reprobe_time_by_the_paused_seconds():
    """일시정지한 시간만큼 재탐침 시각이 뒤로 밀려야 한다.

    속도 10.0 고정으로 틱 29에 되물림(재탐침은 틱 59) → 틱 40에 shift(100)
    -> 틱 59에는 오르지 않고 틱 159에 오른다
    """
    line = Line(ThreadController(4, 48))
    line.run(lambda target: 10.0, 40)

    line.controller.shift(100.0)
    line.run(lambda target: 10.0, 130)

    assert [tick for tick in line.raises() if tick > 29] == [159]


def test_skip_discards_the_samples_right_after_a_resume():
    """skip 뒤 그 시간 동안의 측정은 판단에 쓰지 않아야 한다.

    정체(기준 10.0) 중에 skip(1초) → 다음 틱의 속도 1.0(일시정지가 섞인 측정) → 그 뒤 10.0
    -> 붕괴 방향으로 센 틱이 0 그대로
    """
    line = Line(ThreadController(4, 48))
    line.run(lambda target: 10.0, 40)

    line.controller.skip(line.now, 1.0)
    line.tick(1.0)

    assert line.controller.collapse_count == 0
