"""같은 요청을 오류로 다시 보내기 전에 기다릴 시간 (#329).

실패한 요청을 곧바로 다시 보내면 서버가 같은 오류를 내는 동안(끊긴 회선 · 오류를 내는 서버)
간격 없이 요청이 쌓인다. 다시 보낼 때마다 대기를 두 배씩 늘리고 상한에서 멈춘다. 동시에
실패한 요청들이 같은 순간에 다시 몰리지 않게 대기를 조금씩 흔든다.

대기 시간 계산은 이 모듈 한 곳에 둔다 — 다운로드 엔진의 세그먼트 · 파트 재요청이 쓰고,
다른 요청 경로(영상 정보 조회 등)가 같은 규칙을 쓸 때도 여기서 가져간다. 기다리는 방법
(스레드를 재우는지, 대기열에 시각을 적는지)은 쓰는 쪽이 정한다.
"""

import random
from collections.abc import Callable

RETRY_FIRST_DELAY = 0.5  # 첫 재요청 전의 대기(초)
RETRY_MAX_DELAY = 4.0  # 대기의 상한(초) — 두 배씩 늘다가 여기서 멈춘다
# 대기를 이 비율까지 줄여 흔든다 — 줄이기만 해서 상한을 넘지 않는다
RETRY_JITTER = 0.1


def retry_delay(attempt: int, rand: Callable[[], float] = random.random) -> float:
    """attempt번째 재요청 전에 기다릴 시간(초)을 돌려준다.

    흔들기 전 값은 0.5 · 1 · 2 · 4 · 4 …초이고, 그 값에서 최대 ``RETRY_JITTER``만큼 줄인다.

    Args:
        attempt: 몇 번째 재요청인가(1부터). 1보다 작으면 1로 본다
        rand: [0, 1)의 난수를 내는 함수 — 테스트가 흔들림을 고정할 때 바꿔 넣는다
    """
    doublings = min(max(attempt, 1) - 1, 16)  # 상한에 닿은 뒤로는 더 셀 필요가 없다
    nominal = min(RETRY_FIRST_DELAY * 2**doublings, RETRY_MAX_DELAY)
    return nominal * (1.0 - RETRY_JITTER * rand())
