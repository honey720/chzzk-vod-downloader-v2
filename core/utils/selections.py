"""다운로드 구간 검증 (#178).

사용자가 고른 구간 목록이 받을 수 있는 모양인지 규칙별로 판정하는 순수 함수다.
엔진에 넘기기 전의 검사와 입력 화면의 검사가 같은 함수를 부른다 — 규칙이 두
곳에서 따로 구현되어 어긋나는 것을 막는다.

결과는 사용자에게 보일 문구가 아니라 위반 키다. 키는 번역하지 않은 i18n 키
원문이며 번역은 앱 계층이 한다(``MetadataError``와 같은 방식).

시각 비교는 프레임 단위로 양자화해서 한다(``core.utils.timecode.frame_index``).
구간은 프레임 단위로 고르므로, float 오차로 어긋난 두 시각이 같은 프레임을
가리키면 같은 값으로 본다.
"""

import math
from collections.abc import Sequence
from fractions import Fraction

from core.models.plan import TimeRange
from core.utils.timecode import frame_index, frame_rate

# 한 번에 받을 수 있는 구간 수의 상한
MAX_SELECTIONS = 20

# 위반 키 — 번역하지 않은 i18n 키 원문
SELECTION_ORDER = "Start must be before end"  # 시작이 끝과 같거나 뒤다
SELECTION_OUT_OF_RANGE = "Selection is outside the video"  # 시작 < 0 또는 끝 > 영상 길이
SELECTION_TOO_SHORT = "Selection is shorter than one frame"  # 시작과 끝이 같은 프레임이다
SELECTION_DUPLICATE = "Duplicate selection"  # 시작·끝이 둘 다 같은 구간이 또 있다
SELECTION_TOO_MANY = "Too many selections"  # MAX_SELECTIONS 번째를 넘은 구간이다

# 영상 길이를 프레임 수로 내릴 때의 여유(프레임) — 길이가 프레임 경계에 정확히
# 놓였는데 float 오차로 경계 바로 아래 값이 되어 마지막 프레임이 잘리는 것을 막는다
_DURATION_SLACK_FRAMES = Fraction(1, 1000)


def validate_selections(
    ranges: Sequence[TimeRange | tuple[float, float]], duration: float, fps: float
) -> dict[int, tuple[str, ...]]:
    """구간 목록을 규칙별로 검사해 구간마다 위반 키를 돌려준다.

    구간은 ``TimeRange`` 또는 (시작, 끝) 초 쌍으로 준다. ``TimeRange``는 생성할 때
    시작 < 끝을 검사하므로, 그 규칙을 어긴 입력을 판정하려면 쌍으로 준다.

    규칙:

    - 순서 — 시작 < 끝 (``SELECTION_ORDER``)
    - 범위 — 0 ≤ 시작, 끝 ≤ 영상 길이 (``SELECTION_OUT_OF_RANGE``)
    - 최소 길이 — 1프레임 (``SELECTION_TOO_SHORT``)
    - 중복 — 시작·끝 프레임이 둘 다 같은 구간이 있으면 그 구간 전부 (``SELECTION_DUPLICATE``)
    - 개수 — ``MAX_SELECTIONS``를 넘은 뒤쪽 구간 (``SELECTION_TOO_MANY``)

    겹치는 구간은 허용한다. 유한한 수가 아닌 시각은 범위 위반으로 보고 나머지
    규칙은 그 구간에 적용하지 않는다.

    Args:
        ranges: 검사할 구간 목록. 순서가 구간 번호다
        duration: 영상 길이(초)
        fps: 선언 fps. 프레임 단위 비교의 기준이다

    Returns:
        위반이 있는 구간만 담은 ``{구간 인덱스(0부터): 위반 키들}``. 비어 있으면 통과다.
        키의 순서는 위 규칙 순서다.
    """
    duration_frames = math.floor(Fraction(duration) * frame_rate(fps) + _DURATION_SLACK_FRAMES)
    violations: dict[int, list[str]] = {}
    seen: dict[tuple[int, int], list[int]] = {}

    for index, item in enumerate(ranges):
        start, end = (item.start, item.end) if isinstance(item, TimeRange) else item
        found: list[str] = []
        if not (math.isfinite(start) and math.isfinite(end)):
            found.append(SELECTION_OUT_OF_RANGE)
        else:
            start_frame, end_frame = frame_index(start, fps), frame_index(end, fps)
            if start >= end:
                found.append(SELECTION_ORDER)
            if start_frame < 0 or end_frame > duration_frames:
                found.append(SELECTION_OUT_OF_RANGE)
            if start < end and end_frame - start_frame < 1:
                found.append(SELECTION_TOO_SHORT)
            seen.setdefault((start_frame, end_frame), []).append(index)
        if found:
            violations[index] = found

    for indexes in seen.values():
        if len(indexes) > 1:
            for index in indexes:
                violations.setdefault(index, []).append(SELECTION_DUPLICATE)
    for index in range(MAX_SELECTIONS, len(ranges)):
        violations.setdefault(index, []).append(SELECTION_TOO_MANY)

    return {index: tuple(keys) for index, keys in sorted(violations.items())}
