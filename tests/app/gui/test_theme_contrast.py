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
    ("text", "cardHoverBg", 4.5, "본문 글 ↔ 호버 · 고른 카드(#611 176m)"),
    ("textMuted", "cardHoverBg", 4.5, "흐린 글(카드 정보) ↔ 호버 · 고른 카드(#611 176m)"),
    ("stateWaiting", "cardHoverBg", 4.5, "대기 상태 글 ↔ 호버 · 고른 카드(#611 176m)"),
    ("stateRunning", "cardHoverBg", 4.5, "받는 중 상태 글 ↔ 호버 · 고른 카드(#611 176m)"),
    ("statePaused", "cardHoverBg", 4.5, "일시정지 상태 글 ↔ 호버 · 고른 카드(#611 176m)"),
    ("textMuted", "cardTintFinishedHover", 4.5, "흐린 글 ↔ 호버한 완료 카드(#611 176m)"),
    ("textMuted", "cardTintFailedHover", 4.5, "흐린 글 ↔ 호버한 실패 카드(#611 176m)"),
    ("stateFinishedOnTint", "cardTintFinished", 4.5, "완료 상태 글 ↔ 완료 카드(#554)"),
    ("stateFinishedOnTint", "cardTintFinishedHover", 4.5, "완료 상태 글 ↔ 호버한 완료 카드(#554)"),
    ("stateFailedOnTint", "cardTintFailed", 4.5, "실패 상태 글 ↔ 실패 카드(#554)"),
    ("stateFailedOnTint", "cardTintFailedHover", 4.5, "실패 상태 글 ↔ 호버한 실패 카드(#554)"),
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
        "windowBg": "#f0f0f0",  # 바 면과 같은 창 배경 — 오너 결정(2026-11-17, #611 176m)
        "surface": "#ffffff",
        "surfaceAlt": "#eceef1",
        "barBg": "#f0f0f0",  # 바 면 (A)안 — 오너 결정(2026-11-16, #611 176i)
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
