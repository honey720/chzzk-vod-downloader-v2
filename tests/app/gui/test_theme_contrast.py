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
    ("stateFailed", "barBg", 4.5, "실패 칩 글 ↔ 헤더바(칩 면 없음 — #536 · #611)"),
    ("text", "cardHoverBg", 4.5, "본문 글 ↔ 반 단계 호버 카드(#611 176m · 176q)"),
    ("textMuted", "cardHoverBg", 4.5, "흐린 글(카드 정보) ↔ 반 단계 호버 카드(#611 176m · 176q)"),
    ("stateWaiting", "cardHoverBg", 4.5, "대기 상태 글 ↔ 반 단계 호버 카드(#611 176m · 176q)"),
    ("stateRunning", "cardHoverBg", 4.5, "받는 중 상태 글 ↔ 반 단계 호버 카드(#611 176m · 176q)"),
    ("statePaused", "cardHoverBg", 4.5, "일시정지 상태 글 ↔ 반 단계 호버 카드(#611 176m · 176q)"),
    ("text", "cardSelectedBg", 4.5, "본문 글 ↔ 한 단계 고른 카드(#611 176q)"),
    ("textMuted", "cardSelectedBg", 4.5, "흐린 글(카드 정보) ↔ 한 단계 고른 카드(#611 176q)"),
    ("stateWaiting", "cardSelectedBg", 4.5, "대기 상태 글 ↔ 한 단계 고른 카드(#611 176q)"),
    ("stateRunning", "cardSelectedBg", 4.5, "받는 중 상태 글 ↔ 한 단계 고른 카드(#611 176q)"),
    ("statePaused", "cardSelectedBg", 4.5, "일시정지 상태 글 ↔ 한 단계 고른 카드(#611 176q)"),
    ("text", "cardTintFinished", 4.5, "본문 글 ↔ 완료 카드(#554 · #611 176q)"),
    ("textMuted", "cardTintFinished", 4.5, "흐린 글 ↔ 완료 카드(#554 · #611 176q)"),
    ("stateFinishedOnTint", "cardTintFinished", 4.5, "완료 상태 글 ↔ 완료 카드(#554 · #611 176q)"),
    ("text", "cardTintFinishedHover", 4.5, "본문 글 ↔ 호버한 완료 카드(#554 · #611 176q)"),
    ("textMuted", "cardTintFinishedHover", 4.5, "흐린 글 ↔ 호버한 완료 카드(#554 · #611 176q)"),
    (
        "stateFinishedOnTint",
        "cardTintFinishedHover",
        4.5,
        "완료 상태 글 ↔ 호버한 완료 카드(#554 · #611 176q)",
    ),
    ("text", "cardTintFinishedSelected", 4.5, "본문 글 ↔ 고른 완료 카드(#554 · #611 176q)"),
    ("textMuted", "cardTintFinishedSelected", 4.5, "흐린 글 ↔ 고른 완료 카드(#554 · #611 176q)"),
    (
        "stateFinishedOnTint",
        "cardTintFinishedSelected",
        4.5,
        "완료 상태 글 ↔ 고른 완료 카드(#554 · #611 176q)",
    ),
    ("text", "cardTintFailed", 4.5, "본문 글 ↔ 실패 카드(#554 · #611 176q)"),
    ("textMuted", "cardTintFailed", 4.5, "흐린 글 ↔ 실패 카드(#554 · #611 176q)"),
    ("stateFailedOnTint", "cardTintFailed", 4.5, "실패 상태 글 ↔ 실패 카드(#554 · #611 176q)"),
    ("text", "cardTintFailedHover", 4.5, "본문 글 ↔ 호버한 실패 카드(#554 · #611 176q)"),
    ("textMuted", "cardTintFailedHover", 4.5, "흐린 글 ↔ 호버한 실패 카드(#554 · #611 176q)"),
    (
        "stateFailedOnTint",
        "cardTintFailedHover",
        4.5,
        "실패 상태 글 ↔ 호버한 실패 카드(#554 · #611 176q)",
    ),
    ("text", "cardTintFailedSelected", 4.5, "본문 글 ↔ 고른 실패 카드(#554 · #611 176q)"),
    ("textMuted", "cardTintFailedSelected", 4.5, "흐린 글 ↔ 고른 실패 카드(#554 · #611 176q)"),
    (
        "stateFailedOnTint",
        "cardTintFailedSelected",
        4.5,
        "실패 상태 글 ↔ 고른 실패 카드(#554 · #611 176q)",
    ),
    ("text", "barBg", 4.5, "바 글 ↔ 바 면(#611 176i)"),
    ("textMuted", "barBg", 4.5, "바 흐린 글(안 고른 탭 · 하단 글) ↔ 바 면(#611 176i)"),
    ("text", "fieldBg", 4.5, "입력 칸 글 ↔ 칸 면(#611 176i)"),
    ("text", "errorTipBg", 4.5, "등록 오류 말풍선 글 ↔ 면(#536)"),
    ("text", "tipBg", 4.5, "말풍선 글 ↔ 떠 있는 면(#536)"),
    ("textMuted", "tipBg", 4.5, "말풍선 단축키(흐린 글) ↔ 떠 있는 면(#536)"),
    ("stateFailed", "errorTipBg", 3.0, "등록 오류 말풍선 테두리 ↔ 면 — 그림(#536)"),
    ("stateFailed", "surfaceAlt", 4.5, "실패 칩 글 ↔ 칩(버튼 면)"),
    ("textMuted", "surfaceAlt", 4.5, "꺼진 칩 글 ↔ 칩(버튼 면)"),
    ("accent", "barBg", 3.0, "강조 채움(하단 다운로드) ↔ 하단바 — 그림"),
    ("focusOnAccent", "accent", 3.0, "강조 채움 버튼의 초점 테두리 ↔ 채움(#536)"),
    ("focusOnAccent", "barBg", 3.0, "강조 채움 버튼의 초점 테두리 ↔ 바 면(#536)"),
    ("text", "barHover", 4.5, "바 버튼 · 칩 글 ↔ 바 버튼 호버 면(#611 176k)"),
    ("text", "barPressed", 4.5, "바 버튼 · 칩 글 ↔ 바 버튼 눌림 면(#611 176k)"),
    ("textMuted", "barHover", 3.0, "바 아이콘(흐린 색) ↔ 바 버튼 호버 면 — 그림(#611 176k)"),
    ("textMuted", "barPressed", 3.0, "바 아이콘(흐린 색) ↔ 바 버튼 눌림 면 — 그림(#611 176k)"),
    ("text", "barSelected", 4.5, "고른 칩 · 켠 선택 버튼 글 ↔ 고른 면(#611 176t)"),
    ("text", "barSelected", 4.5, "고른 실패 칩 글(text) ↔ 고른 면(#611 176u)"),
    ("accent", "barBg", 3.0, "고른 칩 · 켠 선택 버튼 focus ring ↔ 바 면(176u) — 그림"),
    ("text", "menuHover", 4.5, "메뉴 항목 이름 ↔ 메뉴 호버 면(#536)"),
    ("textMuted", "menuHover", 4.5, "메뉴 흐린 경로 · 머리글 ↔ 메뉴 호버 면(#536)"),
    ("textMuted", "surface", 4.5, "메뉴 흐린 경로 · 머리글 ↔ 메뉴 면(#536)"),
    ("focusOnAccent", "windowBg", 3.0, "카드 focus ring ↔ 창 바탕(카드 사이) — 그림(#561)"),
    ("pickMarkRing", "accent", 3.0, "고른 체크 원의 테두리 ↔ 강조색 채움 — 그림(#561)"),
]

# 면 색 — 오너 결정(2026-11-07): 테마 색 이식에서 바꾸지 않는다. 앱의 지금 값 그대로
SURFACES = {
    "dark": {
        "windowBg": "#1b1c1f",
        "surface": "#24262b",
        "surfaceAlt": "#2f3239",
        "barBg": "#222329",  # 바 면 (A)안 — 오너 결정(2026-11-16, #611 176i)
        "fieldBg": "#2f3239",
        "surfaceHover": "#383c44",
        "surfacePressed": "#282b31",
        "thumbBg": "#1a1c21",  # 카드 면과 같이 반 단계 내림 — 오너 결정(2026-11-17, #611 176s)
        "border": "#3a3d45",
        "borderStrong": "#4c515b",
        "text": "#e6e8ec",
        "textMuted": "#a0a6b0",
        "cardBg": "#26282e",  # 반 단계 어둡게 — 오너 결정(2026-11-17, #611 176r)
        "scrollHandle": "#4c515b",
        "scrollHandleHover": "#5e646f",
    },
    "light": {
        "windowBg": "#f2f3f6",  # 바 면과 같은 창 배경, 푸른 회색 · #f3f3f3 밝기 — 오너 결정(2026-11-17, #611 176p)
        "surface": "#ffffff",
        "surfaceAlt": "#eceef1",
        "barBg": "#f2f3f6",  # 바 면 (A)안, 푸른 회색 — 오너 결정(2026-11-16 · 11-17, #611 176i · 176p)
        "fieldBg": "#ffffff",  # 회색 바 위 흰 입력 칸 — 오너 결정(2026-11-16, #611 176i)
        "surfaceHover": "#e1e4e9",
        "surfacePressed": "#d5d9df",
        "thumbBg": "#e6e8ec",
        "border": "#d5d8de",
        "borderStrong": "#b9bec7",
        "text": "#1c1e22",
        "textMuted": "#555a63",  # 호버한 색조 카드 위 4.5 — 오너 결정(2026-11-17, #611 176m)
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


# 입력 칸 테두리 (나) ↔ 칸 면 대비 — 176h에서 칸 면 surfaceAlt 위 2.4로 정한 값. 176i에서 라이트 칸 면만 흰색이 돼
# 라이트는 2.79다(테두리 값은 그대로)
FIELD_EDGE_FACE = {"dark": 2.4, "light": 2.79}
FIELD_EDGE_SLACK = 0.05  # 목표 대비의 허용 폭 — 16진 색 반올림


@pytest.mark.parametrize("scheme", SCHEMES)
def test_field_edge_keeps_the_chosen_contrast(scheme: str) -> None:
    """입력 칸 테두리(fieldEdge)는 칸 면과 정한 대비이고 borderStrong보다 진해야 한다(#611).

    fieldEdge ↔ fieldBg = 다크 2.4 · 라이트 2.79 ± 0.05, borderStrong ↔ fieldBg < 그 값
    """
    table = SCHEMES[scheme]
    face = table["fieldBg"]
    ratio = _contrast(table["fieldEdge"], face)
    assert abs(ratio - FIELD_EDGE_FACE[scheme]) <= FIELD_EDGE_SLACK, f"{scheme}: {ratio:.2f}"
    assert _contrast(table["borderStrong"], face) < ratio


@pytest.mark.parametrize("scheme", SCHEMES)
def test_field_edge_hover_is_one_step_further_from_the_face(scheme: str) -> None:
    """입력 칸 호버 테두리는 평소 테두리보다 칸 면 · 바 면과 대비가 커야 한다(한 단계 진하게, #611).

    fieldEdgeHover ↔ fieldBg · barBg > fieldEdge ↔ 같은 면
    """
    table = SCHEMES[scheme]
    for ground in ("fieldBg", "barBg"):
        assert _contrast(table["fieldEdgeHover"], table[ground]) > _contrast(
            table["fieldEdge"], table[ground]
        ), ground


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize(
    ("bar", "card"), [("barHover", "surfaceHover"), ("barPressed", "surfacePressed")]
)
def test_bar_button_face_is_further_from_the_bar_than_the_card_face(
    scheme: str, bar: str, card: str
) -> None:
    """바 버튼 호버 · 눌림 면은 카드 쪽 같은 면보다 바 면과 대비가 커야 한다(한 단계 더, #611 176k).

    barHover ↔ barBg > surfaceHover ↔ barBg, barPressed ↔ barBg > surfacePressed ↔ barBg
    """
    table = SCHEMES[scheme]
    assert _contrast(table[bar], table["barBg"]) > _contrast(table[card], table["barBg"])


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
# 선택 모드의 체크 원(#561)
# ---------------------------------------------------------------------------

#: 썸네일 밝기의 두 끝 — 안 고른 체크 원이 놓일 수 있는 가장 밝은 · 어두운 바탕.
THUMBNAIL_ENDS = {"white": "#ffffff", "black": "#000000"}


def _over(front: str, alpha: float, ground: str) -> str:
    """`front`를 불투명도 `alpha`로 `ground` 위에 얹은 색 — 채널마다 sRGB 값을 섞는다."""
    mixed = (
        round(int(front[i : i + 2], 16) * alpha + int(ground[i : i + 2], 16) * (1 - alpha))
        for i in (1, 3, 5)
    )
    return "#" + "".join(f"{value:02x}" for value in mixed)


def test_over_helper_matches_known_values() -> None:
    """얹기 계산이 알려진 값과 맞아야 한다 — 검정 50%를 흰색 위에 = #808080, 100% = 앞 색."""
    assert _over("#000000", 0.5, "#ffffff") == "#808080"
    assert _over("#3b8cff", 1.0, "#ffffff") == "#3b8cff"


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("thumbnail", THUMBNAIL_ENDS)
def test_empty_pick_mark_ring_reads_on_its_ground(scheme: str, thumbnail: str) -> None:
    """안 고른 체크 원의 테두리는 썸네일 위에 얹힌 원 바탕과 3.0 이상이어야 한다.

    두 테마 × 썸네일 흰색 · 검정, 원 바탕(pickMarkScrim · PICK_MARK_SCRIM_ALPHA)을 썸네일에 얹은 색
    -> pickMarkRing ↔ 얹은 색 ≥ 3.0
    """
    table = SCHEMES[scheme]
    ground = _over(table["pickMarkScrim"], theme.PICK_MARK_SCRIM_ALPHA, THUMBNAIL_ENDS[thumbnail])
    ratio = _contrast(table["pickMarkRing"], ground)
    assert ratio >= 3.0, f"{scheme} {thumbnail}: {table['pickMarkRing']} ↔ {ground} = {ratio:.2f}"


@pytest.mark.parametrize("scheme", SCHEMES)
def test_empty_pick_mark_ground_stands_out_on_a_white_thumbnail(scheme: str) -> None:
    """안 고른 체크 원의 바탕은 흰 썸네일과 3.0 이상이어야 한다 — 테두리가 썸네일에 묻혀도 원이 보인다.

    두 테마, 원 바탕을 #ffffff에 얹은 색 ↔ #ffffff ≥ 3.0
    """
    table = SCHEMES[scheme]
    white = THUMBNAIL_ENDS["white"]
    ground = _over(table["pickMarkScrim"], theme.PICK_MARK_SCRIM_ALPHA, white)
    ratio = _contrast(ground, white)
    assert ratio >= 3.0, f"{scheme}: {ground} ↔ {white} = {ratio:.2f}"


# ---------------------------------------------------------------------------
# 상태 버블 · OS 알림 아이콘의 원 색(#523, 설계 §12.1 상태 색)
# ---------------------------------------------------------------------------

#: 버블 상태마다의 원 색 토큰 — 제품 표(app.widgets.icons.BUBBLE_FILL_TOKENS)를 쓰지 않고 따로 적는다.
BUBBLE_FILLS = ("stateRunning", "statePaused", "stateFailed", "stateWarning", "stateFinished")

#: OS 알림 바탕 — 설계 §12.1이 정한 값(라이트 흰색 · 다크 #2c2c2c).
OS_NOTICE_GROUND = {"dark": "#2c2c2c", "light": "#ffffff"}

#: 원이 놓이는 평소 바탕 토큰 — 메뉴바(바 면 barBg — #611 176i부터, 그 전 surface · 창 배경) · 고른 메뉴(버튼 면).
#: 창 배경 · surface는 메뉴바 면이 바뀐 뒤에도 남겨 둔다(기준을 줄이지 않는다).
BUBBLE_GROUNDS = ("windowBg", "surface", "barBg", "surfaceAlt")

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
def test_info_is_the_progress_color(scheme: str) -> None:
    """정보 토큰은 그 테마의 진행 색(stateRunning)과 값이 같아야 한다(설계 §11.5, #611 176n).

    두 테마의 stateInfo · stateRunning -> 소문자 16진 값이 같음
    """
    table = SCHEMES[scheme]
    assert table["stateInfo"].lower() == table["stateRunning"].lower()


@pytest.mark.parametrize("scheme", SCHEMES)
def test_info_meets_the_bubble_conditions(scheme: str) -> None:
    """정보 토큰도 버블 조건(기호 4.5 · 바탕 3.0)을 지켜야 한다 — 쓰는 자리가 생겨도 같은 기준.

    두 테마의 stateInfo -> onAccent ↔ stateInfo ≥ 4.5 · 바탕 셋 ≥ 3.0
    """
    table = SCHEMES[scheme]
    grounds = [table[name] for name in BUBBLE_GROUNDS] + [OS_NOTICE_GROUND[scheme]]
    assert _contrast(table["onAccent"], table["stateInfo"]) >= 4.5
    assert min(_contrast(table["stateInfo"], g) for g in grounds) >= 3.0


# 라이트 바탕 넷 — border(#d5d8de)의 색상각 · 절반 채도를 준 푸른 회색, #f3f3f3 밝기 안(오너 결정 2026-11-17, #611 176p)
LIGHT_TINTED = {
    "windowBg": "#f2f3f6",
    "barBg": "#f2f3f6",
    "cardSelectedBg": "#e4e4e7",  # 176p까지의 호버 면 값이 고른 카드 면이 됐다(#611 176q)
    "rowHoverBg": "#e4e4e7",
}
# 바 버튼 면 셋 — 고른 면은 176k ~ 176u의 호버 면 값, 호버 · 눌림은 그 값에서 파생한 값(오너 결정 2026-11-17, #611 176v)
BAR_FACES = {
    "dark": {"barSelected": "#4c5159", "barHover": "#43474f", "barPressed": "#474c54"},
    "light": {"barSelected": "#c2c5c9", "barHover": "#d0d3d6", "barPressed": "#c9cccf"},
}


@pytest.mark.parametrize("token", LIGHT_TINTED)
def test_light_ground_is_the_blue_grey(token: str) -> None:
    """라이트 바탕 넷은 지시 값이고 파랑 채널이 빨강보다 커야 한다(무채색이 아님).

    windowBg · barBg = #f2f3f6, cardSelectedBg · rowHoverBg = #e4e4e7, blue − red > 0
    """
    value = theme.LIGHT[token].lower()
    assert value == LIGHT_TINTED[token]
    assert int(value[5:7], 16) > int(value[1:3], 16), value


@pytest.mark.parametrize("scheme", BAR_FACES)
def test_bar_button_faces_are_the_chosen_values(scheme: str) -> None:
    """바 버튼 고른 면 · 호버 · 눌림 면은 정한 값이어야 한다.

    다크 barSelected #4c5159 · barHover #43474f · barPressed #474c54,
    라이트 #c2c5c9 · #d0d3d6 · #c9cccf (#611 176v)
    """
    table = SCHEMES[scheme]
    got = {token: table[token].lower() for token in BAR_FACES[scheme]}
    assert got == BAR_FACES[scheme]


def _lab(color: str) -> tuple[float, float, float]:
    """sRGB 16진 색의 CIE L*a*b*(D65) — 제품의 변환을 쓰지 않는다."""

    def linear(channel: int) -> float:
        c = channel / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(int(color[i : i + 2], 16)) for i in (1, 3, 5))
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > (6 / 29) ** 3 else t / (3 * (6 / 29) ** 2) + 4 / 29

    return 116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))


def _de00_lab(one: tuple[float, float, float], two: tuple[float, float, float]) -> float:
    """두 L*a*b* 사이 CIEDE2000 색 차(Sharma 2005 식)."""
    l1, a1, b1 = one
    l2, a2, b2 = two
    c_bar = (math.hypot(a1, b1) + math.hypot(a2, b2)) / 2
    g = 0.5 * (1 - math.sqrt(c_bar**7 / (c_bar**7 + 25**7)))
    a1p, a2p = a1 * (1 + g), a2 * (1 + g)
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if c1p else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if c2p else 0.0
    dlp, dcp = l2 - l1, c2p - c1p
    if c1p * c2p == 0:
        dhp = 0.0
    elif abs(h2p - h1p) <= 180:
        dhp = h2p - h1p
    else:
        dhp = h2p - h1p - 360 if h2p > h1p else h2p - h1p + 360
    dbig_h = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dhp / 2))
    lbp, cbp = (l1 + l2) / 2, (c1p + c2p) / 2
    if c1p * c2p == 0:
        hbp = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hbp = (h1p + h2p) / 2
    else:
        hbp = (h1p + h2p + 360) / 2 if h1p + h2p < 360 else (h1p + h2p - 360) / 2
    t = (
        1
        - 0.17 * math.cos(math.radians(hbp - 30))
        + 0.24 * math.cos(math.radians(2 * hbp))
        + 0.32 * math.cos(math.radians(3 * hbp + 6))
        - 0.20 * math.cos(math.radians(4 * hbp - 63))
    )
    sl = 1 + 0.015 * (lbp - 50) ** 2 / math.sqrt(20 + (lbp - 50) ** 2)
    sc = 1 + 0.045 * cbp
    sh = 1 + 0.015 * cbp * t
    rt = (
        -2
        * math.sqrt(cbp**7 / (cbp**7 + 25**7))
        * math.sin(math.radians(60 * math.exp(-(((hbp - 275) / 25) ** 2))))
    )
    return math.sqrt(
        (dlp / sl) ** 2 + (dcp / sc) ** 2 + (dbig_h / sh) ** 2 + rt * (dcp / sc) * (dbig_h / sh)
    )


def _de00(one: str, two: str) -> float:
    return _de00_lab(_lab(one), _lab(two))


def test_de00_helper_matches_known_values() -> None:
    """ΔE00 도우미가 Sharma 시험 짝 · 같은 색에서 알려진 값이어야 한다.

    (50, 2.6772, −79.7751) ↔ (50, 0, −82.7485) = 2.0425, (50, 2.5, 0) ↔ (73, 25, −18) = 27.1492,
    같은 색 = 0
    """
    assert _de00_lab((50, 2.6772, -79.7751), (50, 0, -82.7485)) == pytest.approx(2.0425, abs=1e-4)
    assert _de00_lab((50, 2.5, 0), (73, 25, -18)) == pytest.approx(27.1492, abs=1e-4)
    assert _de00("#2b2d33", "#2b2d33") == 0


CARD_FACES = [
    ("cardBg", "cardHoverBg", "cardSelectedBg"),
    ("cardTintFinished", "cardTintFinishedHover", "cardTintFinishedSelected"),
    ("cardTintFailed", "cardTintFailedHover", "cardTintFailedSelected"),
]  # (평소 면, 호버 면, 선택 면) — 중립 · 완료 · 실패 카드
HALF_STEP = (1.2, 2.0)  # 호버 면 ↔ 평소 면 ΔE00 허용 범위 — 반 단계 약 1.5, 16진 반올림 폭 포함
ONE_STEP = 2.8  # 선택 면 ↔ 평소 면 ΔE00 하한 — 한 단계 약 3.0(완료 · 실패 색조는 2.9 안팎)
DARK_SELECTED_MIN = 3.0  # 다크 중립 선택 면 ↔ 평소 면 ΔE00 하한 — 지시문 176q
CARD_HOVER_HALF = 1.5  # 다크 호버 면의 두 반쪽 ΔE00 하한 — 반 단계(#611 176r)
MIDPOINT_SLACK = (
    0.5  # 다크 호버 면의 두 반쪽(평소 ↔ 호버, 호버 ↔ 선택) ΔE00 차 상한 — 16진 반올림 폭
)
DARK_REST_176Q = {
    "cardBg": "#2b2d33",
    "cardTintFinished": "#243129",
    "cardTintFailed": "#3b2826",
}  # 176q의 다크 평소 면(#611 176q 보고) — 176r 평소 면은 이보다 선택 면에서 멀어야 한다
DARK_SELECTED_FACES = {
    "cardSelectedBg": "#35363c",
    "cardTintFinishedSelected": "#2d3a32",
    "cardTintFailedSelected": "#45312f",
}  # 다크 선택 면 — 그 위 글 4.5를 맞춘 176q 값 그대로(#611 176r)
BAR_TO_CARD_MIN = 1.5  # 다크 바 면 ↔ 카드 평소 면 ΔE00 하한(#611 176r)


@pytest.mark.parametrize(("rest", "hover", "selected"), CARD_FACES)
def test_light_card_hover_is_half_a_step_and_selected_one_step(
    rest: str, hover: str, selected: str
) -> None:
    """라이트 카드 호버 면은 평소 면에서 반 단계, 선택 면은 한 단계여야 하고 호버 ≠ 선택이어야 한다(#611 176q).

    ΔE00(평소, 호버) 1.2 ~ 2.0, ΔE00(평소, 선택) >= 2.8, ΔE00(평소, 호버) < ΔE00(평소, 선택),
    호버 ≠ 선택(16진 값)
    """
    table = theme.LIGHT
    half = _de00(table[rest], table[hover])
    one = _de00(table[rest], table[selected])
    assert HALF_STEP[0] <= half <= HALF_STEP[1], f"light {hover}: {half:.2f}"
    assert one >= ONE_STEP, f"light {selected}: {one:.2f}"
    assert half < one
    assert table[hover].lower() != table[selected].lower()


@pytest.mark.parametrize(("rest", "hover", "selected"), CARD_FACES)
def test_dark_card_hover_is_midway_between_rest_and_selected(
    rest: str, hover: str, selected: str
) -> None:
    """다크 카드 호버 면은 평소 면 · 선택 면 가운데여야 한다(#611 176r).

    ΔE00(평소, 호버) >= 1.5, ΔE00(호버, 선택) >= 1.5, 두 값 차 <= 0.5, 호버 ≠ 선택(16진 값)
    """
    table = theme.DARK
    near = _de00(table[rest], table[hover])
    far = _de00(table[hover], table[selected])
    assert near >= CARD_HOVER_HALF and far >= CARD_HOVER_HALF, f"{hover}: {near:.2f} {far:.2f}"
    assert abs(near - far) <= MIDPOINT_SLACK, f"{hover}: {near:.2f} {far:.2f}"
    assert table[hover].lower() != table[selected].lower()


@pytest.mark.parametrize(("rest", "_hover", "selected"), CARD_FACES)
def test_dark_selected_face_is_farther_than_in_176q(rest: str, _hover: str, selected: str) -> None:
    """다크 평소 면 ↔ 선택 면 ΔE00은 176q 값보다 커야 한다(#611 176r).

    ΔE00(평소, 선택) > ΔE00(176q 평소 #2b2d33 · #243129 · #3b2826, 선택)
    """
    step = _de00(theme.DARK[rest], theme.DARK[selected])
    before = _de00(DARK_REST_176Q[rest], theme.DARK[selected])
    assert step > before, f"{rest}: {step:.2f} <= {before:.2f}"


def test_dark_selected_faces_keep_the_176q_values() -> None:
    """다크 선택 면(중립 · 완료 · 실패)은 176q 값 그대로여야 한다(#611 176r).

    cardSelectedBg #35363c · cardTintFinishedSelected #2d3a32 · cardTintFailedSelected #45312f
    """
    changed = {
        k: theme.DARK[k] for k, v in DARK_SELECTED_FACES.items() if theme.DARK[k].lower() != v
    }
    assert changed == {}


def test_dark_card_face_is_lighter_than_the_bar_face() -> None:
    """다크 카드 평소 면은 바 면보다 밝고 ΔE00 1.5 이상 떨어져야 한다(#611 176r).

    L*(cardBg) > L*(barBg), ΔE00(barBg, cardBg) >= 1.5
    """
    card, bar = theme.DARK["cardBg"], theme.DARK["barBg"]
    assert _lab(card)[0] > _lab(bar)[0], f"{card} {bar}"
    assert _de00(bar, card) >= BAR_TO_CARD_MIN, f"{_de00(bar, card):.2f}"


def test_dark_layers_get_lighter_from_the_list_to_the_selected_card() -> None:
    """다크 면 밝기는 빈 목록 < 바 < 카드 평소 < 호버 < 선택 차례여야 한다(#611 176r).

    L*: windowBg < barBg < cardBg < cardHoverBg < cardSelectedBg
    """
    layers = ("windowBg", "barBg", "cardBg", "cardHoverBg", "cardSelectedBg")
    lightness = [_lab(theme.DARK[name])[0] for name in layers]
    assert lightness == sorted(lightness), dict(zip(layers, lightness, strict=True))
    assert len(set(lightness)) == len(layers)


def test_dark_selected_card_is_at_least_three_from_the_rest_face() -> None:
    """다크 중립 선택 면은 평소 면과 ΔE00 3.0 이상 떨어져야 한다(#611 176q).

    ΔE00(cardBg, cardSelectedBg) >= 3.0
    """
    step = _de00(theme.DARK["cardBg"], theme.DARK["cardSelectedBg"])
    assert step >= DARK_SELECTED_MIN, f"{step:.2f}"


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize(("rest", "hover", "selected"), CARD_FACES)
def test_card_faces_take_no_accent(scheme: str, rest: str, hover: str, selected: str) -> None:
    """호버 · 선택 면은 평소 면보다 강조색(파랑) 쪽으로 더 기울지 않아야 한다 — 중립 면(#611 176q).

    (파랑 − 빨강)(호버 · 선택) − (파랑 − 빨강)(평소) <= 3(푸른 회색 기운 · 16진 반올림 폭)
    """
    table = SCHEMES[scheme]

    def lean(value: str) -> int:
        return int(value[5:7], 16) - int(value[1:3], 16)

    for face in (hover, selected):
        assert lean(table[face]) - lean(table[rest]) <= 3, f"{scheme} {face}: {table[face]}"


FIELD_HOVER_MIN = 12.0  # 입력 칸 호버 ↔ 평소 테두리 ΔE00 하한 — 두 단계(약 13.4), 지시문 176s
FOCUS_GAP_MIN = 12.0  # 호버 테두리 ↔ 초점 테두리(accent) ΔE00 하한 — 지시문 176s


@pytest.mark.parametrize("scheme", SCHEMES)
def test_field_edge_hover_is_two_steps_from_the_rest_edge(scheme: str) -> None:
    """입력 칸 호버 테두리는 평소 테두리와 ΔE00 12 이상 떨어져야 한다(#611 176s).

    ΔE00(fieldEdge, fieldEdgeHover) >= 12
    """
    table = SCHEMES[scheme]
    step = _de00(table["fieldEdge"], table["fieldEdgeHover"])
    assert step >= FIELD_HOVER_MIN, f"{scheme}: {step:.2f}"


@pytest.mark.parametrize(("scheme", "lighter"), [("dark", True), ("light", False)])
def test_field_edge_hover_moves_toward_the_text(scheme: str, lighter: bool) -> None:
    """입력 칸 호버 테두리는 다크에서 평소 테두리보다 밝고 라이트에서 어두워야 한다(#611 176s).

    다크: L*(fieldEdgeHover) > L*(fieldEdge), 라이트: L*(fieldEdgeHover) < L*(fieldEdge)
    """
    table = SCHEMES[scheme]
    rest, hover = _lab(table["fieldEdge"])[0], _lab(table["fieldEdgeHover"])[0]
    assert (hover > rest) is lighter, f"{scheme}: {rest:.1f} -> {hover:.1f}"


@pytest.mark.parametrize("scheme", SCHEMES)
def test_field_edge_hover_differs_from_the_focus_edge(scheme: str) -> None:
    """입력 칸 호버 테두리는 초점 테두리(accent)와 ΔE00 12 이상 떨어져야 한다(#611 176s).

    ΔE00(fieldEdgeHover, accent) >= 12
    """
    table = SCHEMES[scheme]
    gap = _de00(table["fieldEdgeHover"], table["accent"])
    assert gap >= FOCUS_GAP_MIN, f"{scheme}: {gap:.2f}"


BAR_HOVER_GAP = (3.3, 3.6)  # 바 호버 면 ↔ 고른 칩 면 ΔE00 범위 — 반 단계(약 3.35, 지시문 176v)
DARK_OLD_BAR_HOVER = (
    "#383c44"  # 176k 전 다크 바 버튼 호버 면(surfaceHover) — 약하다고 한 값(지시문 176v)
)
DARK_THUMB_MIN = 3.0  # 다크 썸네일 자리표시 ↔ 카드 평소 면 ΔE00 하한 — 지시문 176s


def test_dark_thumbnail_placeholder_stands_apart_from_the_card_face() -> None:
    """다크 썸네일 자리표시는 카드 평소 면과 ΔE00 3.0 이상 떨어지고 카드 면보다 어두워야 한다(#611 176s).

    ΔE00(thumbBg, cardBg) >= 3.0, L*(thumbBg) < L*(cardBg)
    """
    table = theme.DARK
    gap = _de00(table["thumbBg"], table["cardBg"])
    assert gap >= DARK_THUMB_MIN, f"{gap:.2f}"
    assert _lab(table["thumbBg"])[0] < _lab(table["cardBg"])[0]


@pytest.mark.parametrize("scheme", SCHEMES)
def test_bar_hover_is_half_a_step_short_of_the_chosen_face(scheme: str) -> None:
    """바 호버 면은 고른 칩 면과 ΔE00 3.3 ~ 3.6 떨어져야 한다(#611 176v).

    3.3 <= ΔE00(barHover, barSelected) <= 3.6
    """
    table = SCHEMES[scheme]
    gap = _de00(table["barHover"], table["barSelected"])
    low, high = BAR_HOVER_GAP
    assert low <= gap <= high, f"{scheme}: {gap:.2f}"


@pytest.mark.parametrize("scheme", SCHEMES)
def test_bar_faces_go_rest_hover_pressed_chosen(scheme: str) -> None:
    """바 면에서의 거리는 평소 < 호버 < 눌림 < 고른 면 차례여야 한다(#611 176v).

    ΔE00(barBg, ·): barHover < barPressed < barSelected, barHover > 0
    """
    table = SCHEMES[scheme]
    gaps = [
        _de00(table["barBg"], table[name]) for name in ("barHover", "barPressed", "barSelected")
    ]
    assert 0 < gaps[0] < gaps[1] < gaps[2], f"{scheme}: {[round(g, 2) for g in gaps]}"


def test_dark_bar_hover_is_stronger_than_the_old_hover() -> None:
    """다크 바 호버 면은 바 면에서 옛 호버 값(#383c44)보다 멀어야 한다(#611 176v).

    ΔE00(barBg, barHover) > ΔE00(barBg, #383c44)
    """
    table = theme.DARK
    assert _de00(table["barBg"], table["barHover"]) > _de00(table["barBg"], DARK_OLD_BAR_HOVER)


@pytest.mark.parametrize(("scheme", "lighter"), [("dark", True), ("light", False)])
def test_bar_selected_moves_away_from_the_bar(scheme: str, lighter: bool) -> None:
    """고른 칩 면은 다크에서 바 호버 면보다 밝고 라이트에서 어두워야 한다(#611 176t).

    다크: L*(barSelected) > L*(barHover), 라이트: L*(barSelected) < L*(barHover)
    """
    table = SCHEMES[scheme]
    hover, chosen = _lab(table["barHover"])[0], _lab(table["barSelected"])[0]
    assert (chosen > hover) is lighter, f"{scheme}: {hover:.1f} -> {chosen:.1f}"
