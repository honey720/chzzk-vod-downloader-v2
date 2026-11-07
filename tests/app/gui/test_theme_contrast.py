"""테마 색 대비 게이트 — 채움 색 규칙 · 상태 글 · 막대 트랙 · 면 색 고정 (#512, 설계 §12.1).

값은 `app.theme`의 두 표에서 읽고, 대비비(WCAG)는 이 파일이 따로 계산한다 — 제품의 계산 함수를 쓰지
않는다(제품이 틀리면 테스트도 같이 틀리지 않게). 기준: 글 4.5 · 그림 3.0(inbox 39).
"""

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
    ("stateFailed", "surface", 4.5, "등록 오류 글 ↔ 헤더바"),
    ("stateFailed", "surfaceAlt", 4.5, "실패 칩 글 ↔ 칩(버튼 면)"),
    ("textMuted", "surfaceAlt", 4.5, "꺼진 칩 글 ↔ 칩(버튼 면)"),
    ("accent", "surface", 3.0, "강조 채움(하단 다운로드) ↔ 하단바 — 그림"),
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


def test_contrast_helper_matches_known_values():
    """대비 계산이 알려진 값과 맞아야 한다 — 흰색 ↔ 검정 21, 같은 색 1."""
    assert round(_contrast("#ffffff", "#000000"), 2) == 21.0
    assert _contrast("#3b8cff", "#3b8cff") == 1.0


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize(("front", "ground", "need", "what"), PAIRS, ids=[p[3] for p in PAIRS])
def test_pair_meets_its_contrast(scheme, front, ground, need, what):
    """그 짝의 대비가 두 테마 모두 기준 이상이어야 한다."""
    table = SCHEMES[scheme]
    ratio = _contrast(table[front], table[ground])
    assert ratio >= need, (
        f"{scheme} {what}: {table[front]} ↔ {table[ground]} = {ratio:.2f} < {need}"
    )


@pytest.mark.parametrize("scheme", SCHEMES)
def test_progress_track_is_the_card_border(scheme):
    """진행 막대 트랙은 카드 테두리 색이어야 한다(설계 §12.1)."""
    table = SCHEMES[scheme]
    assert table["barTrack"].lower() == table["border"].lower()


@pytest.mark.parametrize("scheme", SCHEMES)
def test_surfaces_keep_the_app_values(scheme):
    """면 색(바탕 · 바 · 카드 · 버튼 면 · 테두리 · 글)은 앱의 지금 값 그대로여야 한다(오너 결정)."""
    table = SCHEMES[scheme]
    changed = {k: table[k] for k, v in SURFACES[scheme].items() if table[k].lower() != v.lower()}
    assert changed == {}


def test_fill_rule_splits_the_two_themes():
    """채움 색 규칙 — 다크는 밝은 채움 + 어두운 안 글자, 라이트는 어두운 채움 + 흰 안 글자여야 한다.

    다크: 채움이 안 글자보다 밝다. 라이트: 채움이 안 글자보다 어둡다(설계 §12.1)
    """
    assert _luminance(theme.DARK["accent"]) > _luminance(theme.DARK["onAccent"])
    assert _luminance(theme.LIGHT["accent"]) < _luminance(theme.LIGHT["onAccent"])
