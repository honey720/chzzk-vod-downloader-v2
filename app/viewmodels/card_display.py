"""카드 화면 설정 — 썸네일 모양 · 완료 · 실패 색조 · 애니메이션을 한 곳에서 읽는다 (#554 · #645).

설정 창 화면 탭이 세 키를 고른다(#645). 키 이름은 이 모듈의 상수 셋이 정본이다(이름을 바꿀 때 이 셋과
`app/system/settings.py`의 표만 고친다).

진행 표시는 카드 아래끝 막대 하나다 — 배경 채움 방식과 그 설정 키(`cardProgress`)는 없앴다(설계 §13,
#611). DB에 남은 그 키의 행은 설정 표에 없어 읽을 때 버려진다.

애니메이션은 사용자가 설정 창에서 고르기 전까지(행이 없거나 null) OS 애니메이션 설정을 따른다(설계 §14.4,
`app/system/motion_preference.py`).

카드는 만들 때마다 설정을 읽지 않는다 — 1000장 목록이면 DB를 1000번 읽는다. 처음 읽은 값을 이
모듈이 들고 있고(`current`), 값이 바뀌었으면 `refresh`로 다시 읽는다. 목록 전체가 같은 값을 쓰므로
카드마다 모양이 섞이지 않는다. 설정 창 저장 · 앱이 다시 활성이 될 때 본 창이 `refresh`를 부르고 달라진
값을 목록에 건다(`app/views/mainWindow.py` `applyCardDisplay`).
"""

import logging
from dataclasses import dataclass

from app.system import motion_preference, settings
from core.engine.base import PROGRESS_SECONDS

logger = logging.getLogger(__name__)

# 엔진의 진행 통지 간격(ms) — 카드 막대가 통지 사이를 이어 움직이는 시간의 정본(#557). 같은 값을
# 앱에 따로 박지 않는다
PROGRESS_INTERVAL_MS = round(PROGRESS_SECONDS * 1000)

COMPACT_THUMBNAIL_KEY = "cardCompactThumbnail"  # 썸네일 줄이기 — 참이면 「위 두 줄 옆」 68×38
STATE_TINT_KEY = "cardStateTint"  # 완료 · 실패 색조 — 거짓이면 카드 면에 색을 칠하지 않는다
ANIMATIONS_KEY = "animations"  # 부드러운 애니메이션 — 거짓이면 바뀜이 바로 일어난다. null = OS 설정


@dataclass(frozen=True)
class CardDisplay:
    """카드 화면 설정 한 벌 — 목록 전체가 같은 값을 쓴다."""

    compact_thumbnail: bool = False  # 참이면 썸네일 「위 두 줄 옆」
    animations: bool = True  # 참이면 막대 이어 움직임 · 중지 · 완료 · 실패 모션
    state_tint: bool = True  # 참이면 완료 · 실패 카드 면에 색조를 칠한다


_STATE: dict[str, CardDisplay | None] = {"current": None}  # 읽어 둔 값 — 처음 부를 때 읽는다
# 마지막으로 로그에 남긴 OS 값 — 같은 값을 되풀이하지 않는다
_LOGGED: dict[str, bool | None] = {"os": None}
# 카드에 마지막으로 건 OS 값 — OS를 따라 읽은 적이 없거나 `use`로 값을 바꾼 뒤면 None
_APPLIED_OS: dict[str, bool | None] = {"value": None}


def animations_setting(os_value: bool | None = None) -> bool:
    """지금 쓸 애니메이션 켬 여부를 돌려준다 — 사용자가 고른 값, 고르지 않았으면 OS 설정.

    Args:
        os_value: 이미 읽은 OS 값. 주면 OS를 다시 읽지 않는다 — 비교한 값과 건 값이 어긋나지 않게
            (None이면 여기서 읽는다)
    """
    chosen = settings.load_setting(ANIMATIONS_KEY)
    if chosen is not None:
        return bool(chosen)
    value = motion_preference.system_animations() if os_value is None else os_value
    _APPLIED_OS["value"] = value
    if _LOGGED["os"] != value:
        _LOGGED["os"] = value
        logger.info("애니메이션: OS 설정을 따름(%s)", "켬" if value else "끔")
    return value


def os_animations() -> bool:
    """OS의 애니메이션 설정 — 본 창이 앱이 다시 활성이 될 때 바뀌었는지 비교한다(설계 §14.4)."""
    return motion_preference.system_animations()


def os_value_changed(now: bool) -> bool:
    """OS 값 `now`가 카드에 마지막으로 건 OS 값과 다르면 참.

    건 값이 없으면(OS를 따라 읽은 적이 없음 · `use`로 바꾼 뒤) `now`를 기준으로 적고 거짓이다 —
    본 창이 앱이 다시 활성이 될 때 부른다. 창이 따로 읽어 둔 값과 비교하면, 카드를 만들 때의 읽기와 창의
    읽기가 다른 값을 낼 때 그 뒤 바뀜을 못 알아본다(#645).
    """
    applied = _APPLIED_OS["value"]
    if applied is None:
        _APPLIED_OS["value"] = now
        return False
    return now != applied


def follows_os() -> bool:
    """애니메이션을 사용자가 고르지 않아(행 없음 · null) OS 설정을 따르는 중인지."""
    return settings.load_setting(ANIMATIONS_KEY) is None


def refresh(os_value: bool | None = None) -> CardDisplay:
    """설정에서 다시 읽어 들고 있는 값을 바꾼다. 바꾼 값을 돌려준다.

    `os_value`는 `animations_setting`에 그대로 넘긴다.
    """
    display = CardDisplay(
        compact_thumbnail=bool(settings.load_setting(COMPACT_THUMBNAIL_KEY)),
        animations=animations_setting(os_value),
        state_tint=bool(settings.load_setting(STATE_TINT_KEY)),
    )
    _STATE["current"] = display
    return display


def current() -> CardDisplay:
    """들고 있는 값 — 아직 읽지 않았으면 설정에서 읽는다."""
    display = _STATE["current"]
    return display if display is not None else refresh()


def use(display: CardDisplay | None) -> None:
    """들고 있는 값을 바꾼다(테스트가 쓴다). None이면 다음 `current`가 설정을 다시 읽는다.

    카드에 건 OS 값도 비운다 — 바꾼 값이 OS 값과 달라도 다음 활성 때 덮지 않는다(`os_value_changed`).
    """
    _STATE["current"] = display
    _APPLIED_OS["value"] = None
