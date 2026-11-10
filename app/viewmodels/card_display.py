"""카드 화면 설정 — 진행 표시 · 썸네일 모양 · 애니메이션을 한 곳에서 읽는다 (#554, 설계 §12.1).

설정 화면 항목은 설정 단계에서 만든다. 그때까지는 설정 키로만 고른다 — 키 이름은 이 모듈의 상수
셋이 정본이다(설정 단계에서 이름을 바꿀 때 이 셋과 `app/system/settings.py`의 표만 고친다).

카드는 만들 때마다 설정을 읽지 않는다 — 1000장 목록이면 DB를 1000번 읽는다. 처음 읽은 값을 이
모듈이 들고 있고(`current`), 값이 바뀌었으면 `refresh`로 다시 읽는다. 목록 전체가 같은 값을 쓰므로
카드마다 모양이 섞이지 않는다.
"""

from dataclasses import dataclass

from app.system import settings

PROGRESS_KEY = "cardProgress"  # 진행 표시 — "bar"(막대, 기본) · "fill"(배경 채움)
COMPACT_THUMBNAIL_KEY = "cardCompactThumbnail"  # 썸네일 줄이기 — 참이면 「위 두 줄 옆」 68×38
ANIMATIONS_KEY = "animations"  # 부드러운 애니메이션 — 거짓이면 바뀜이 바로 일어난다

PROGRESS_BAR = "bar"  # 진행 표시 — 카드 아래끝 막대
PROGRESS_FILL = "fill"  # 진행 표시 — 카드 배경 채움


@dataclass(frozen=True)
class CardDisplay:
    """카드 화면 설정 한 벌 — 목록 전체가 같은 값을 쓴다."""

    progress: str = PROGRESS_BAR  # PROGRESS_BAR · PROGRESS_FILL
    compact_thumbnail: bool = False  # 참이면 썸네일 「위 두 줄 옆」
    animations: bool = True  # 참이면 채움 이어 움직임 · 중지 모션

    @property
    def fill(self) -> bool:
        """배경 채움 방식인지."""
        return self.progress == PROGRESS_FILL


_STATE: dict[str, CardDisplay | None] = {"current": None}  # 읽어 둔 값 — 처음 부를 때 읽는다


def refresh() -> CardDisplay:
    """설정에서 다시 읽어 들고 있는 값을 바꾼다. 바꾼 값을 돌려준다."""
    display = CardDisplay(
        progress=str(settings.load_setting(PROGRESS_KEY)),
        compact_thumbnail=bool(settings.load_setting(COMPACT_THUMBNAIL_KEY)),
        animations=bool(settings.load_setting(ANIMATIONS_KEY)),
    )
    _STATE["current"] = display
    return display


def current() -> CardDisplay:
    """들고 있는 값 — 아직 읽지 않았으면 설정에서 읽는다."""
    display = _STATE["current"]
    return display if display is not None else refresh()


def use(display: CardDisplay | None) -> None:
    """들고 있는 값을 바꾼다(테스트 · 설정 화면이 쓴다). None이면 다음 `current`가 설정을 다시 읽는다."""
    _STATE["current"] = display
