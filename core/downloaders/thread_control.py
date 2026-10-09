"""목표 스레드 수 조정 규칙 (#112 · #347).

판단 신호는 #112와 같다 — "목표를 올렸을 때 총 처리량이 실제로 늘었는가". 달라진 것은 그것을
재는 방법이다. #112는 올리기 전 · 후의 틱 하나씩을 견줬는데, 회선이 찬 뒤의 틱 속도는 그 자체로
흔들려 늘지 않은 인상이 우연히 통과했고, 통과한 것은 한 칸만 되돌려져 목표가 위로만 밀려
올라갔다 (#347). 여기서는 인접한 두 수준의 틱 표본 묶음을 견주고, 뚜렷하지 않게 받아들인 수준은
아래 수준으로 내려가 다시 확인한다.

이 모듈은 엔진을 모른다 — 시각(초)과 총 처리량을 받아 목표를 정할 뿐이다. 시간 상수는 모두
초이고, 관측 틱은 약 1초 간격이라고 가정한다(``TICK_SECONDS``).
"""

import math
import statistics

TICK_SECONDS = 1.0  # 관측 틱 간격(초) — 표본 하나가 대표하는 시간
CLIMB_STEP = 4  # 한 번에 올리는 목표 스레드 수 (#112)
TARGET_CAP = 48  # 목표 스레드 상한 — 서버 부하 상한 (#112)
# 인상 유효 판정의 요구폭: 선형 기대 이득(step/target)의 이 비율. 낮게 잡는 이유는 #112 —
# 1080p 실측에서 8→12가 +7%처럼 선형 미만이어도 실질 이득인 구간이 있다
GROWTH_EFFICIENCY = 0.125
# 선형 기대 이득의 이 비율 이상 늘면 뚜렷한 인상 — 표본 둘만 보고 받아들인다.
# 포화 없는 회선에서 상한까지 빨리 오르게 하는 길이다
CLEAR_EFFICIENCY = 0.5
FAST_SAMPLES = 2  # 뚜렷한 인상으로 받아들이는 데 보는 표본 수
SETTLE_SECONDS = 2.0  # 목표를 바꾼 뒤 재지 않는 시간(초) — 새 연결의 시작 구간이 측정에 섞인다
WINDOW_SECONDS = 10.0  # 한 수준에서 재는 시간(초) — 견줄 표본 묶음 하나
# 올린 수준의 표본 가운데 이 개수 이상이 아래 수준 표본의 최댓값(+요구폭)을 넘으면 늘었다.
# 두 묶음의 분포가 같으면(올려도 안 늘면) 묶음 10개씩에서 통과 확률이 10.5% 이하다
RANK_NEED = 3
# 평균 차이 검정: 평균의 차이가 표준오차의 이 배수를 넘으면 늘었다. 순위 판정이 약한
# 대칭 흔들림에서 인상을 놓치지 않으려고 함께 쓴다
MEAN_Z = 2.5
REPROBE_SECONDS = 30.0  # 되물린 뒤 다시 올려 보기까지(초). 실패할 때마다 두 배
REPROBE_MAX_SECONDS = 120.0  # 그 간격의 상한(초) — 회선이 넓어진 것을 이 안에 알아챈다
RECHECK_SECONDS = 60.0  # 아래 수준과 다시 견주기까지(초). 확인될 때마다 두 배
RECHECK_MAX_SECONDS = 480.0  # 그 간격의 상한(초)
COLLAPSE_RATIO = 0.50  # 붕괴 판정: 정체 기준 총 처리량의 절반 미만 (#112)
COLLAPSE_TICKS = 5  # 감소 확정에 필요한 틱 수 (#112)
RECOVERY_RATIO = 1.30  # 회복 판정: 정체 기준의 이 배수 초과 (#112)
# 회복 판정이 이 시간(초) 이어져야 바로 다시 올려 본다 — 틱 하나로 재개하면 출렁임마다 오른다
RECOVERY_SECONDS = 5.0
HOLD_EMA_ALPHA = 0.2  # 정체 기준의 지수이동평균 가중치 (#112)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


class ThreadController:
    """총 처리량의 표본 묶음을 견줘 목표 스레드 수를 오르내린다.

    관측 틱마다 ``step``을 한 번 부른다. 목표는 ``target``이다.

    판정식(``_gained``): 올린 수준의 표본 가운데 ``RANK_NEED``개 이상이 아래 수준 표본의
    최댓값 × (1 + 요구폭)을 넘거나, 두 묶음의 평균 차이가 요구폭과 표준오차의 ``MEAN_Z``배를
    모두 넘으면 늘었다고 본다.

    - 오를 때: 처음 ``FAST_SAMPLES``개가 모두 뚜렷이 넘으면 바로 받아들이고 계속 오른다.
      아니면 아래 수준의 묶음을 다 채운 뒤 견줘, 못 넘으면 되물리고 머문다.
    - 머물 때: ``REPROBE_SECONDS`` 뒤에 다시 올려 보고, 실패할 때마다 간격을 두 배로 늘린다.
      뚜렷하지 않게 받아들인 수준은 아래 수준으로 내려가 다시 견주고, 차이가 없으면
      내려앉는다 — 우연히 통과한 인상을 되돌리는 힘은 이것뿐이다.
    - 총 처리량이 기준의 절반 미만으로 이어지면 절반으로 줄인다 (#112 그대로).
    """

    def __init__(self, target: int, cap: int):
        """조정기를 만든다.

        Args:
            target: 시작 목표 스레드 수
            cap: 목표 상한 — 호출자가 min(작업 수, ``TARGET_CAP``)으로 정한다
        """
        self.target = target
        self.cap = cap
        self.floor = min(target, CLIMB_STEP)  # 아래 수준과 견주러 내려가는 하한
        self.collapse_count = 0  # 붕괴 방향 틱 수(음수로 센다)
        self._phase = self._climb
        self._settle_until = -math.inf  # 이 시각까지의 측정은 버린다
        self._visit: list[float] = []  # 지금 수준에 온 뒤의 표본
        self._visit_from = 0.0  # 지금 수준에서 재기 시작한 시각
        self._started = False  # 첫 유효 측정을 받았는가
        self._lower = 0  # 견주는 중인 두 수준
        self._upper = 0
        self._other: list[float] = []  # 견줄 상대 수준의 표본 묶음
        self._short = False  # 아래 수준의 묶음이 덜 찬 채 올렸는가
        self._reprobe_gap = REPROBE_SECONDS
        self._reprobe_at = 0.0
        self._recheck_gap = RECHECK_SECONDS
        self._recheck_at = 0.0
        self._peaks: dict[int, float] = {}  # 수준 → 그 수준에서 본 최댓값
        self._clear: dict[int, bool] = {}  # 수준 → 그 수준으로 올린 것이 뚜렷했는가
        self._hold_reference = 0.0  # 정체 구간의 기준 총 처리량
        self._recovering_from: float | None = None  # 회복 판정이 이어지기 시작한 시각

    def step(self, now: float, total_speed: float) -> None:
        """관측 틱 하나 — 지금 시각(단조 시계, 초)과 직전 틱의 총 처리량으로 목표를 조정한다."""
        if now < self._settle_until:
            return
        if not self._started:
            if total_speed <= 0:
                return  # 시작 직후 첫 유효 측정 전 — 판단 근거가 없다
            self._started = True
            self._visit_from = now - TICK_SECONDS
        self._visit.append(total_speed)
        self._phase(now, total_speed)

    def shift(self, seconds: float) -> None:
        """모든 시각 기준을 seconds만큼 뒤로 민다 — 일시정지한 시간을 조정 판단에서 뺀다."""
        self._settle_until += seconds
        self._visit_from += seconds
        self._reprobe_at += seconds
        self._recheck_at += seconds
        if self._recovering_from is not None:
            self._recovering_from += seconds

    def skip(self, now: float, seconds: float) -> None:
        """지금부터 seconds 동안의 측정을 버린다 — 일시정지가 섞인 측정으로 판단하지 않는다."""
        self._settle_until = max(self._settle_until, now + seconds + TICK_SECONDS / 2)

    # ---- 도우미

    def _move(self, level: int, now: float) -> None:
        self.target = level
        self._visit = []
        self._visit_from = now + SETTLE_SECONDS
        self._settle_until = self._visit_from + TICK_SECONDS / 2

    def _full(self, now: float) -> bool:
        """지금 수준에서 묶음 하나만큼 쟀는가."""
        return now - self._visit_from >= WINDOW_SECONDS

    def _recent(self) -> list[float]:
        return self._visit[-int(WINDOW_SECONDS / TICK_SECONDS) :]

    def _bar(self, low: list[float], lower: int, upper: int) -> float:
        """올린 수준의 표본이 넘어야 하는 값 — 아래 수준의 최댓값에 요구폭을 얹는다."""
        return max(low) * (1 + GROWTH_EFFICIENCY * (upper - lower) / lower)

    def _mean_gained(self, low: list[float], high: list[float], lower: int, upper: int) -> bool:
        if len(low) < 2 or len(high) < 2:
            return False
        low_mean, high_mean = _mean(low), _mean(high)
        error = math.sqrt(
            statistics.variance(low) / len(low) + statistics.variance(high) / len(high)
        )
        required = low_mean * (1 + GROWTH_EFFICIENCY * (upper - lower) / lower)
        return high_mean > required and high_mean - low_mean > MEAN_Z * error

    def _hold_at(self, reference: float) -> None:
        self._phase = self._hold
        self._hold_reference = reference
        self.collapse_count = 0
        self._recovering_from = None

    def _raise(self, now: float) -> None:
        """한 칸 올려 본다 — 지금 수준의 최근 표본이 견줄 묶음이 된다."""
        self._lower = self.target
        self._upper = min(self.cap, self.target + CLIMB_STEP)
        self._other = self._recent()
        self._short = not self._full(now)
        self._move(self._upper, now)
        self._phase = self._try

    def _accept(self, now: float, total_speed: float, clear: bool) -> None:
        self._clear[self._upper] = clear
        self._reprobe_gap = REPROBE_SECONDS
        self._phase = self._climb
        self._climb(now, total_speed)

    # ---- 단계 (step이 틱마다 하나를 부른다)

    def _climb(self, now: float, total_speed: float) -> None:
        """등반: 지금 수준의 표본이 모이면 한 칸 올려 본다."""
        if self.target >= self.cap:
            self._hold_at(_mean(self._visit))
            self._reprobe_at = math.inf
            self._recheck_at = now + self._recheck_gap
            return
        if not self._clear.get(self.target, True) and not self._full(now):
            return  # 뚜렷하지 않게 올라온 수준 — 묶음을 다 채운 뒤에 더 올려 본다
        self._raise(now)

    def _try(self, now: float, total_speed: float) -> None:
        """올린 수준에서 재는 중: 늘었으면 받아들이고, 묶음을 다 채워도 아니면 되물린다."""
        lower, upper, visit = self._lower, self._upper, self._visit
        peak = max(self._other)
        self._peaks[lower] = peak
        linear = (upper - lower) / lower
        if len(visit) == FAST_SAMPLES and min(visit) > peak * (1 + CLEAR_EFFICIENCY * linear):
            self._accept(now, total_speed, clear=True)
            return
        if self._short:
            if len(visit) >= FAST_SAMPLES:
                # 뚜렷하지 않았다 — 아래 수준에서 묶음을 다 채운 뒤 제대로 견준다
                self._move(lower, now)
                self._phase = self._refill
            return
        over = sum(value > self._bar(self._other, lower, upper) for value in visit)
        if over >= RANK_NEED:
            self._accept(now, total_speed, clear=over == len(visit))
            return
        if not self._full(now):
            return
        if self._mean_gained(self._other, visit, lower, upper):
            self._clear[upper] = False
            self._reprobe_gap = REPROBE_SECONDS
            self._phase = self._climb
            return
        # 올려도 늘지 않았다 — 되물리고 여기를 천장으로 머문다
        self._clear.pop(upper, None)
        self._peaks[upper] = max(visit)
        self._move(lower, now)
        self._hold_at(_mean(self._other))
        self._reprobe_at = now + self._reprobe_gap
        self._reprobe_gap = min(self._reprobe_gap * 2, REPROBE_MAX_SECONDS)
        self._recheck_at = max(self._recheck_at, now + REPROBE_SECONDS)

    def _refill(self, now: float, total_speed: float) -> None:
        """아래 수준의 묶음을 다 채우는 중: 차면 다시 올려 본다."""
        if self._full(now):
            self._raise(now)

    def _hold(self, now: float, total_speed: float) -> None:
        """정체: 붕괴 감시 · 회복 감지 · 주기 재탐침 · 아래 수준과의 재확인."""
        if total_speed < self._hold_reference * COLLAPSE_RATIO:
            self.collapse_count -= 1
            self._recovering_from = None
            if self.collapse_count <= -COLLAPSE_TICKS:
                # 처리량이 기준의 절반 미만으로 이어졌다(회선 · 서버 악화). 하락한 처리량이
                # 새 기준이 된다 — 추가 감소는 또 절반 미만으로 떨어졌을 때만 일어난다.
                # 회선이 달라졌으니 수준별 기억은 버린다
                halved = max(1, self.target // 2)
                self._peaks.clear()
                self._clear.clear()
                self._hold_reference = total_speed
                self.collapse_count = 0
                self._reprobe_gap = REPROBE_SECONDS
                self._reprobe_at = now + REPROBE_SECONDS
                self._recheck_at = now + RECHECK_SECONDS
                if halved != self.target:
                    self._move(halved, now)
            return
        if total_speed > self._hold_reference * RECOVERY_RATIO:
            if self._recovering_from is None:
                self._recovering_from = now - TICK_SECONDS
            if now - self._recovering_from >= RECOVERY_SECONDS:
                # 기준보다 뚜렷이 빠른 상태가 이어졌다(경합 해소 등) — 바로 다시 올려 본다
                self._recovering_from = None
                self._reprobe_gap = REPROBE_SECONDS
                self._reprobe_at = now
        else:
            # 안정 대역 — 기준을 평활 갱신하고 감소 카운터를 감쇠한다
            self._recovering_from = None
            self._hold_reference += HOLD_EMA_ALPHA * (total_speed - self._hold_reference)
            if self.collapse_count < 0:
                self.collapse_count += 1
        if self.target < self.cap and now >= self._reprobe_at:
            self._raise(now)
        elif (
            self.target > self.floor
            and now >= self._recheck_at
            and self._full(now)
            and self._recheck_worth()
        ):
            self._lower = max(self.floor, self.target - CLIMB_STEP)
            self._upper = self.target
            self._other = self._recent()
            self._move(self._lower, now)
            self._phase = self._recheck

    def _recheck_worth(self) -> bool:
        """아래 수준과 다시 견줄 까닭이 있는가.

        뚜렷하지 않게 받아들인 수준이거나, 지금 속도가 아래 수준에서 냈던 최댓값을 더는
        넘지 못할 때다. 뚜렷했고 지금도 넘는 수준은 내려가 보지 않는다 — 내려가 있는
        동안의 처리량을 잃는다.
        """
        if not self._clear.get(self.target, False):
            return True
        lower = max(self.floor, self.target - CLIMB_STEP)
        peak = self._peaks.get(lower)
        if peak is None:
            return False
        bar = peak * (1 + GROWTH_EFFICIENCY * (self.target - lower) / lower)
        return sum(value > bar for value in self._recent()) < RANK_NEED

    def _recheck(self, now: float, total_speed: float) -> None:
        """아래 수준에서 재는 중: 위 수준이 정말 더 빨랐는지 같은 판정식으로 다시 견준다."""
        lower, upper, visit = self._lower, self._upper, self._visit
        over = sum(value > self._bar(visit, lower, upper) for value in self._other)
        gained = over >= RANK_NEED
        if not gained:
            if not self._full(now):
                return
            gained = self._mean_gained(visit, self._other, lower, upper)
        if not gained:
            # 내려와도 처리량이 같다 — 여기에 내려앉는다
            self._peaks[lower] = max(visit)
            self._clear.pop(upper, None)
            self._hold_at(_mean(visit))
            self._recheck_at = now + RECHECK_SECONDS
            self._reprobe_at = now + self._reprobe_gap
            self._reprobe_gap = min(self._reprobe_gap * 2, REPROBE_MAX_SECONDS)
            return
        if self._full(now):
            self._peaks[lower] = max(visit)
            self._clear[upper] = over == len(self._other)
            self._move(upper, now)
            self._hold_at(_mean(self._other))
            self._recheck_gap = min(self._recheck_gap * 2, RECHECK_MAX_SECONDS)
            self._recheck_at = now + self._recheck_gap
            self._reprobe_at = max(self._reprobe_at, now + REPROBE_SECONDS)
