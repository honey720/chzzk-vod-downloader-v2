"""단계 여럿을 진행 막대 하나(0~100)로 보이는 계산기 — 남은 몫 재배분 (#390).

다운로드는 단계를 차례로 지난다(구간 다운로드: 받기 → 자르기). 단계마다 "한 일"(0~1)과 그 단계
전체에 걸릴 예상 시간(초)을 받아, 막대를 **한 일**만큼만 올린다. 지난 시간으로 올리지 않는다 —
받기가 멈춰 있는 동안(회선이 끊김 · 일시정지) 막대가 오르고, 재개하면 쉰 시간만큼 뛰던 것(#390)이
이 계산에서는 생기지 않는다.

통지마다 ``(100 − 지금 값) × 이번에 한 일 ÷ 남은 일``만큼 올린다. 일은 예상 시간 단위로 잰다 —
단계 s에서 이번에 한 일은 (지금 한 일 − 앞서 한 일) × 그 단계의 예상 시간이다. 예상이 틀려도
막대는 줄지도 뛰지도 않는다:

- 한 단계만 남으면 예상 시간이 약분되어 그 단계의 한 일만으로 남은 몫을 채운다 — 후처리가
  예상보다 길면 천천히, 짧으면 빨리 찰 뿐이다.
- 예상은 단계 여럿이 함께 남아 있는 동안 남은 몫을 나누는 데만 쓴다. 예상이 바뀌면 그때부터의
  기울기가 바뀐다.

Qt를 모른다 — 헤드리스 출력도 같은 계산을 쓸 수 있다. 여러 스레드에서 불러도 된다.
"""

import threading
from collections.abc import Mapping, Sequence


class StagedProgress:
    """단계마다의 한 일과 예상 시간으로 진행 막대 값(0~100)을 낸다.

    내는 값은 줄지 않는다. 100은 모든 단계의 일이 다 끝났을 때만 낸다 — 그 전에는 99에서 멈춘다.
    단계를 더하려면(구간 없는 다운로드의 받기 → 합치기 등) 이름만 늘리면 된다.
    """

    def __init__(self, stages: Sequence[str]):
        """계산기를 만든다.

        Args:
            stages: 단계 이름 — 순서는 뜻이 없다. 같은 이름을 두 번 쓰지 않는다
        """
        if len(set(stages)) != len(stages):
            raise ValueError(f"단계 이름이 겹친다: {list(stages)}")
        self._stages = tuple(stages)
        self._done = dict.fromkeys(self._stages, 0.0)  # 앞서 받은 한 일(0~1)
        self._value = 0.0  # 막대의 값(0~100) — 소수까지 들고 있는다
        self._lock = threading.Lock()

    def update(self, done: Mapping[str, float], seconds: Mapping[str, float]) -> int:
        """이번 통지의 한 일과 예상 시간으로 막대를 올리고 보일 값을 돌려준다.

        Args:
            done: 단계 이름 → 그 단계의 한 일(0~1). 빠진 단계는 앞서 받은 값 그대로다.
                앞서 받은 값보다 작으면 앞서 받은 값으로 본다(한 일은 줄지 않는다)
            seconds: 단계 이름 → 그 단계 전체에 걸릴 예상 시간(초, 0 이상). 빠진 단계는 0 —
                남은 몫을 나눌 때 그 단계의 몫이 없다

        Returns:
            막대에 보일 값(0~100). 모든 단계의 한 일이 1일 때만 100
        """
        with self._lock:
            now = {
                stage: max(self._done[stage], min(max(done.get(stage, 0.0), 0.0), 1.0))
                for stage in self._stages
            }
            cost = {stage: max(seconds.get(stage, 0.0), 0.0) for stage in self._stages}
            worked = sum((now[s] - self._done[s]) * cost[s] for s in self._stages)
            remaining = sum((1.0 - self._done[s]) * cost[s] for s in self._stages)
            if remaining > 0:
                # worked ≤ remaining이라 100을 넘지 않는다
                self._value += (100.0 - self._value) * worked / remaining
            self._done = now
            return self._shown()

    def _shown(self) -> int:
        if all(value >= 1.0 for value in self._done.values()):
            return 100
        return min(int(self._value), 99)
