"""좁은 폭 단계 판정 — 필요 폭과 쓸 수 있는 폭을 비교해 줄의 모양 단계를 고른다 (#529 — 설계 §2 「좁은 폭」).

단계 0은 가장 넓은 모양이고, 단계가 오를수록 줄이 덜 필요하다(칩 → 드롭다운, 정렬 글 → 아이콘 등).
필요 폭은 지금 보이는 모양과 무관하게 셈한 값이어야 한다 — 지금 보이는 위젯 폭으로 판정하면 경계에서
모양이 바뀌자마자 폭이 바뀌어 다시 판정이 뒤집힌다(프로토 결함 주입: 창 크기 한 번에 151번). 줄어든
모양에서 되돌아갈 때는 `RETURN_SLACK`만큼 더 넓어야 한다 — 경계 근처에서 모양이 오가지 않는다.
"""

from collections.abc import Sequence

RETURN_SLACK = 8  # 줄인 모양에서 되돌아갈 때 더 있어야 하는 폭(px) — 설계 §2 「되돌아갈 때 +8px」


def step_level(needs: Sequence[int], room: int, level: int, slack: int = RETURN_SLACK) -> int:
    """쓸 수 있는 폭 `room`에 맞는 단계를 고른다.

    지금 단계의 필요 폭이 `room`보다 크면 들어갈 때까지 단계를 올린다(마지막 단계에서 멈춘다). 한 단계
    앞(더 넓은 모양)의 필요 폭에 `slack`을 더해도 `room` 안이면 단계를 내린다. 한 번 부르면 최종 단계를
    바로 돌려준다 — 창 크기가 한 번 바뀔 때 모양도 한 번만 바뀐다.

    Args:
        needs: 단계마다 필요한 폭(px). 0번이 가장 넓은 모양이다
        room: 그 줄에서 쓸 수 있는 폭(px)
        level: 지금 단계
        slack: 되돌아갈 때 더 있어야 하는 폭(px)
    """
    last = len(needs) - 1
    level = max(0, min(level, last))
    while level < last and needs[level] > room:
        level += 1
    while level > 0 and needs[level - 1] + slack <= room:
        level -= 1
    return level
