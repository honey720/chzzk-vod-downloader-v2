"""테마 색 대비 게이트 — 채움 색 규칙 · 상태 글 · 막대 트랙 · 면 색 고정 (#512, 설계 §12.1).

값은 `app.theme`의 두 표에서 읽고, 대비비(WCAG)는 이 파일이 따로 계산한다 — 제품의 계산 함수를 쓰지
않는다(제품이 틀리면 테스트도 같이 틀리지 않게). 기준: 글 4.5 · 그림 3.0(inbox 39).
"""

import math

import pytest

import app.theme as theme

SCHEMES = {"dark": theme.DARK, "light": theme.LIGHT}

# (앞 색, 뒤 바탕, 기준, 무엇)
PAIRS = [
    (
        "onAccent",
        "accent",
        4.5,
        "채움 안 글자 ↔ 채움(다운로드 · 고른 칩 · pill · 글 선택 · 메뉴 고른 줄)",
    ),
    ("onAccent", "accentHover", 4.5, "채움 안 글자 ↔ 채움 호버"),
    ("onAccent", "accentPressed", 4.5, "채움 안 글자 ↔ 채움 눌림"),
    ("stateWaiting", "cardBg", 4.5, "대기 상태 글 ↔ 카드"),
    ("stateRunning", "cardBg", 4.5, "받는 중 상태 글 ↔ 카드"),
    ("statePaused", "cardBg", 4.5, "일시정지 상태 글 ↔ 카드"),
    ("stateFinished", "cardBg", 4.5, "완료 상태 글 ↔ 카드"),
    ("stateFailed", "cardBg", 4.5, "실패 상태 글 ↔ 카드"),
    ("stateFailed", "surface", 4.5, "실패 칩 글 ↔ 헤더바(칩 면 없음 · 고른 칩 바 면 — #536)"),
    ("text", "errorTipBg", 4.5, "등록 오류 말풍선 글 ↔ 면(#536)"),
    ("stateFailed", "errorTipBg", 3.0, "등록 오류 말풍선 테두리 ↔ 면 — 그림(#536)"),
    ("stateFailed", "surfaceAlt", 4.5, "실패 칩 글 ↔ 칩(버튼 면)"),
    ("textMuted", "surfaceAlt", 4.5, "꺼진 칩 글 ↔ 칩(버튼 면)"),
    ("accent", "surface", 3.0, "강조 채움(하단 다운로드) ↔ 하단바 — 그림"),
    ("focusOnAccent", "accent", 3.0, "강조 채움 버튼의 초점 테두리 ↔ 채움(#536)"),
    ("focusOnAccent", "surface", 3.0, "강조 채움 버튼의 초점 테두리 ↔ 바 면(#536)"),
    ("text", "menuHover", 4.5, "메뉴 항목 이름 ↔ 메뉴 호버 면(#536)"),
    ("textMuted", "menuHover", 4.5, "메뉴 흐린 경로 · 머리글 ↔ 메뉴 호버 면(#536)"),
    ("textMuted", "surface", 4.5, "메뉴 흐린 경로 · 머리글 ↔ 메뉴 면(#536)"),
]

# 면 색 — 오너 결정(2026-11-07): 테마 색 이식에서 바꾸지 않는다. 앱의 지금 값 그대로
SURFACES = {
    "dark": {
        "windowBg": "#1b1c1f",
        "surface": "#24262b",
        "surfaceAlt": "#2f3239",
        "surfaceHover": "#383c44",
        "surfacePressed": "#282b31",
        "thumbBg": "#1f2126",
        "border": "#3a3d45",
        "borderStrong": "#4c515b",
        "text": "#e6e8ec",
        "textMuted": "#a0a6b0",
        "cardBg": "#2b2d33",
        "scrollHandle": "#4c515b",
        "scrollHandleHover": "#5e646f",
    },
    "light": {
        "windowBg": "#f2f3f5",
        "surface": "#ffffff",
        "surfaceAlt": "#eceef1",
        "surfaceHover": "#e1e4e9",
        "surfacePressed": "#d5d9df",
        "thumbBg": "#e6e8ec",
        "border": "#d5d8de",
        "borderStrong": "#b9bec7",
        "text": "#1c1e22",
        "textMuted": "#5b6069",
        "cardBg": "#ffffff",
        "scrollHandle": "#c3c7cd",
        "scrollHandleHover": "#aeb3bb",
    },
}


def _luminance(color: str) -> float:
    """sRGB 상대 휘도(WCAG 2.x)."""

    def channel(value: int) -> float:
        v = value / 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(a: str, b: str) -> float:
    light, dark = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def test_contrast_helper_matches_known_values() -> None:
    """대비 계산이 알려진 값과 맞아야 한다 — 흰색 ↔ 검정 21, 같은 색 1."""
    assert round(_contrast("#ffffff", "#000000"), 2) == 21.0
    assert _contrast("#3b8cff", "#3b8cff") == 1.0


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize(("front", "ground", "need", "what"), PAIRS, ids=[p[3] for p in PAIRS])
def test_pair_meets_its_contrast(
    scheme: str, front: str, ground: str, need: float, what: str
) -> None:
    """그 짝의 대비가 두 테마 모두 기준 이상이어야 한다."""
    table = SCHEMES[scheme]
    ratio = _contrast(table[front], table[ground])
    assert ratio >= need, (
        f"{scheme} {what}: {table[front]} ↔ {table[ground]} = {ratio:.2f} < {need}"
    )


@pytest.mark.parametrize("scheme", SCHEMES)
def test_progress_track_is_the_card_border(scheme: str) -> None:
    """진행 막대 트랙은 카드 테두리 색이어야 한다(설계 §12.1)."""
    table = SCHEMES[scheme]
    assert table["barTrack"].lower() == table["border"].lower()


@pytest.mark.parametrize("scheme", SCHEMES)
def test_surfaces_keep_the_app_values(scheme: str) -> None:
    """면 색(바탕 · 바 · 카드 · 버튼 면 · 테두리 · 글)은 앱의 지금 값 그대로여야 한다(오너 결정)."""
    table = SCHEMES[scheme]
    changed = {k: table[k] for k, v in SURFACES[scheme].items() if table[k].lower() != v.lower()}
    assert changed == {}


def test_fill_rule_splits_the_two_themes() -> None:
    """채움 색 규칙 — 다크는 밝은 채움 + 어두운 안 글자, 라이트는 어두운 채움 + 흰 안 글자여야 한다.

    다크: 채움이 안 글자보다 밝다. 라이트: 채움이 안 글자보다 어둡다(설계 §12.1)
    """
    assert _luminance(theme.DARK["accent"]) > _luminance(theme.DARK["onAccent"])
    assert _luminance(theme.LIGHT["accent"]) < _luminance(theme.LIGHT["onAccent"])


# ---------------------------------------------------------------------------
# 상태 버블 · OS 알림 아이콘의 원 색(#523, 설계 §12.1 상태 색)
# ---------------------------------------------------------------------------

#: 버블 상태마다의 원 색 토큰 — 제품 표(app.widgets.icons.BUBBLE_FILL_TOKENS)를 쓰지 않고 따로 적는다.
BUBBLE_FILLS = ("stateRunning", "statePaused", "stateFailed", "stateWarning", "stateFinished")

#: OS 알림 바탕 — 설계 §12.1이 정한 값(라이트 흰색 · 다크 #2c2c2c).
OS_NOTICE_GROUND = {"dark": "#2c2c2c", "light": "#ffffff"}

#: 원이 놓이는 평소 바탕 토큰 — 메뉴바(바 면 — #536부터, 그 전 창 배경) · 고른 메뉴(버튼 면).
#: 창 배경은 메뉴바가 바 면이 된 뒤에도 남겨 둔다(기준을 줄이지 않는다).
BUBBLE_GROUNDS = ("windowBg", "surface", "surfaceAlt")

#: 경고 · 정보 토큰의 원래 색 — 색상각이 같아야 한다(밝기만 옮긴다). 정보 = 진행 색.
WARNING_ORIGINAL = "#f0a33c"


def _oklch_hue(color: str) -> float:
    """OKLCH 색상각(도) — 설계 §12.1: 색상이 같은지는 OKLCH 색상각으로 잰다."""

    def linear(value: int) -> float:
        v = value / 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(int(color[i : i + 2], 16)) for i in (1, 3, 5))
    lms = (
        (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3),
        (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3),
        (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3),
    )
    a = 1.9779984951 * lms[0] - 2.4285922050 * lms[1] + 0.4505937099 * lms[2]
    bb = 0.0259040371 * lms[0] + 0.7827717662 * lms[1] - 0.8086757660 * lms[2]
    return math.degrees(math.atan2(bb, a)) % 360


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("fill", BUBBLE_FILLS)
def test_bubble_glyph_reads_on_its_fill(scheme: str, fill: str) -> None:
    """버블 원 안 기호(채움 안 그림 onAccent)는 원 색과 4.5 이상이어야 한다.

    두 테마 × 버블 원 색 다섯 -> onAccent ↔ 원 색 ≥ 4.5
    """
    table = SCHEMES[scheme]
    ratio = _contrast(table["onAccent"], table[fill])
    assert ratio >= 4.5, f"{scheme} {fill}: {table[fill]} = {ratio:.2f}"


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("fill", BUBBLE_FILLS)
def test_bubble_stands_out_on_its_grounds(scheme: str, fill: str) -> None:
    """버블 원은 메뉴바 · 고른 메뉴 · OS 알림 바탕 모두와 3.0 이상이어야 한다.

    두 테마 × 버블 원 색 다섯 -> 원 ↔ windowBg · surfaceAlt · OS 알림 바탕 ≥ 3.0
    """
    table = SCHEMES[scheme]
    grounds = {name: table[name] for name in BUBBLE_GROUNDS} | {"os": OS_NOTICE_GROUND[scheme]}
    low = {name: round(_contrast(table[fill], g), 2) for name, g in grounds.items()}
    assert min(low.values()) >= 3.0, f"{scheme} {fill}: {low}"


@pytest.mark.parametrize("scheme", SCHEMES)
def test_warning_keeps_the_orange_hue(scheme: str) -> None:
    """경고 토큰은 원래 주황(#f0a33c)과 OKLCH 색상각이 1도 안이어야 한다 — 밝기만 옮긴다.

    두 테마의 stateWarning -> |색상각 − 원래 색상각| ≤ 1
    """
    hue = _oklch_hue(SCHEMES[scheme]["stateWarning"])
    assert abs(hue - _oklch_hue(WARNING_ORIGINAL)) <= 1.0


@pytest.mark.parametrize("scheme", SCHEMES)
def test_info_is_the_progress_hue(scheme: str) -> None:
    """정보 토큰은 그 테마의 진행 색(stateRunning)과 OKLCH 색상각이 1도 안이어야 한다(설계 §11.5).

    두 테마의 stateInfo -> |색상각 − stateRunning 색상각| ≤ 1
    """
    table = SCHEMES[scheme]
    assert abs(_oklch_hue(table["stateInfo"]) - _oklch_hue(table["stateRunning"])) <= 1.0


@pytest.mark.parametrize("scheme", SCHEMES)
def test_info_meets_the_bubble_conditions(scheme: str) -> None:
    """정보 토큰도 버블 조건(기호 4.5 · 바탕 3.0)을 지켜야 한다 — 쓰는 자리가 생겨도 같은 기준.

    두 테마의 stateInfo -> onAccent ↔ stateInfo ≥ 4.5 · 바탕 셋 ≥ 3.0
    """
    table = SCHEMES[scheme]
    grounds = [table[name] for name in BUBBLE_GROUNDS] + [OS_NOTICE_GROUND[scheme]]
    assert _contrast(table["onAccent"], table["stateInfo"]) >= 4.5
    assert min(_contrast(table["stateInfo"], g) for g in grounds) >= 3.0
