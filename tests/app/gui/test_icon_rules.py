"""아이콘 규칙 게이트 — 동작 아이콘의 선 · 그림 칸 · 잉크 범위, 상태 아이콘 안쪽 기호의 선, 글자 기호 아이콘 없음 (inbox 177).

그리는 함수를 기록용 painter로 돌려 실제로 건 펜을 읽는다 — 함수 안에 숫자를 따로 쓰면 규칙 상수를 거치지 않아도
여기서 드러난다.
"""

import ast
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

import app.theme as theme
import app.widgets.contentItemWidget as card_face
from app.widgets import icons

REPO = Path(__file__).resolve().parents[3]
GRIDS = (12, 16)  # 동작 아이콘이 쓰이는 그림 칸 — 카드 버튼 · 바 버튼(px)


class _Recorder:
    """QPainter를 감싸 그리기 호출마다 그때의 펜 · 붓을 적는다 — 나머지 호출은 그대로 넘긴다."""

    def __init__(self, painter: QPainter) -> None:
        self._painter = painter
        self.draws: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._painter, name)
        if not name.startswith("draw"):
            return attr

        def draw(*args: Any) -> Any:
            pen, brush = self._painter.pen(), self._painter.brush()
            self.draws.append(
                {
                    "op": name,
                    "args": args,
                    "stroked": pen.style() != Qt.PenStyle.NoPen,
                    "width": pen.widthF(),
                    "cap": pen.capStyle(),
                    "join": pen.joinStyle(),
                    "filled": brush.style() != Qt.BrushStyle.NoBrush,
                }
            )
            return attr(*args)

        return draw


def _recorded(paint: Callable[[_Recorder], None], side: int) -> list[dict[str, Any]]:
    """paint(기록 painter)를 side px 그림에 돌리고 그리기 기록을 돌려준다."""
    image = QImage(side * 4, side * 4, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    recorder = _Recorder(painter)
    paint(recorder)
    painter.end()
    return recorder.draws


def _action_draws(name: str, side: int) -> list[dict[str, Any]]:
    return _recorded(
        lambda p: icons._PAINTERS[name](p, float(side), QColor("#ffffff"), QColor("#ff0000")), side
    )


def _ink_span(name: str, side: int) -> float:
    """잉크(불투명도 0.25 넘는 화소)를 감싼 상자의 긴 변 / 칸 — 배율 4로 그려 잰다."""
    image = icons.action_pixmap(name, "#ffffff", side, 4.0).toImage()
    xs, ys = [], []
    for y in range(image.height()):
        for x in range(image.width()):
            if image.pixelColor(x, y).alphaF() > 0.25:
                xs.append(x)
                ys.append(y)
    assert xs, f"{name} 그림이 비어 있다"
    return max(max(xs) - min(xs) + 1, max(ys) - min(ys) + 1) / image.width()


@pytest.mark.parametrize("side", GRIDS)
@pytest.mark.parametrize("name", icons.ICON_NAMES)
def test_action_icon_lines_share_one_width_round_cap_and_round_join(
    qapp: QApplication, name: str, side: int
) -> None:
    """동작 아이콘의 선은 칸 크기와 무관하게 모두 1.75px · 둥근 끝 · 둥근 이음이고 면이 없어야 한다.

    아이콘마다, 칸 12 · 16 -> 선을 긋는 그리기 호출이 하나 이상, 그 펜 굵기 == 1.75 · RoundCap · RoundJoin,
    붓 없음
    """
    lines = [d for d in _action_draws(name, side) if d["stroked"]]

    assert lines, f"{name}: 선이 없다"
    for draw in lines:
        assert draw["width"] == pytest.approx(1.75), f"{name} {draw['op']}: 굵기 {draw['width']}"
        assert draw["cap"] == Qt.PenCapStyle.RoundCap, f"{name} {draw['op']}"
        assert draw["join"] == Qt.PenJoinStyle.RoundJoin, f"{name} {draw['op']}"
        assert not draw["filled"], f"{name} {draw['op']}: 선 그림에 면이 있다"


@pytest.mark.parametrize("name", icons.ICON_NAMES)
def test_action_icon_fills_are_only_dots_no_wider_than_the_line(
    qapp: QApplication, name: str
) -> None:
    """동작 아이콘에서 면으로 칠한 것은 지름이 선 굵기(1.75px) 이하인 원 점뿐이어야 한다.

    아이콘마다, 칸 16 -> 선 없는 그리기 호출은 drawEllipse(가운데, rx, ry)이고 2·rx, 2·ry <= 1.75
    """
    fills = [d for d in _action_draws(name, 16) if not d["stroked"]]

    for draw in fills:
        assert draw["op"] == "drawEllipse", f"{name}: 점이 아닌 면 {draw['op']}"
        _center, rx, ry = draw["args"]
        assert 2 * rx <= 1.75 + 1e-9 and 2 * ry <= 1.75 + 1e-9, f"{name}: 점 지름 {2 * rx}"


@pytest.mark.parametrize("side", GRIDS)
@pytest.mark.parametrize("name", icons.ICON_NAMES)
def test_action_icon_ink_takes_the_same_share_of_its_grid(
    qapp: QApplication, name: str, side: int
) -> None:
    """동작 아이콘 잉크의 긴 변은 칸의 0.72 ~ 0.95여야 한다 — 칸 안에서 차지하는 크기가 아이콘마다 같은 범위다.

    아이콘마다, 칸 12 · 16, 배율 4 -> 불투명도 0.25 넘는 화소 상자의 긴 변 / 칸 한 변이 [0.72, 0.95]
    """
    span = _ink_span(name, side)

    assert 0.72 <= span <= 0.95, f"{name} 칸 {side}: {span:.3f}"


GEAR_TEETH = 6  # 톱니바퀴 톱니 수 — 지시 값(#611 176g, 여덟에서 줄임)
GEAR_INK_MAX = 0.82  # 톱니바퀴 잉크 긴 변 / 칸 16 상한 — 지시 「0.8 안팎」(옛 그림 0.875)


def test_gear_has_six_short_teeth_and_lighter_ink(qapp: QApplication) -> None:
    """톱니바퀴는 톱니가 여섯이고 칸 16에서 잉크 긴 변이 0.82 이하여야 한다.

    칸 16 · 배율 4 그림, 가운데에서 반지름 6px(톱니 몸통만 지나는 원)을 720점 돌며 잉크(불투명도 0.25 초과)가
    이어진 덩어리 수 == 6, 잉크 긴 변 / 칸 <= 0.82
    """
    import math

    side, scale = 16, 4
    image = icons.action_pixmap("gear", "#ffffff", side, float(scale)).toImage()
    centre = side * scale / 2
    radius = 6.0 * scale
    marks = []
    for step in range(720):
        angle = 2 * math.pi * step / 720
        x = int(centre + radius * math.cos(angle))
        y = int(centre + radius * math.sin(angle))
        marks.append(image.pixelColor(x, y).alphaF() > 0.25)
    runs = sum(1 for i, inked in enumerate(marks) if inked and not marks[i - 1])
    assert runs == GEAR_TEETH
    assert _ink_span("gear", side) <= GEAR_INK_MAX


def test_open_folder_and_closed_folder_are_different_drawings(qapp: QApplication) -> None:
    """폴더 열기(folder)와 저장 위치 표시(folder_outline)는 둘 다 선 그림이어도 화소가 달라야 한다.

    같은 색 · 칸 12 · 배율 1 -> 불투명도가 다른 화소가 있다
    """
    opened = icons.action_pixmap("folder", "#ffffff", 12).toImage()
    closed = icons.action_pixmap("folder_outline", "#ffffff", 12).toImage()

    assert any(
        opened.pixelColor(x, y).alpha() != closed.pixelColor(x, y).alpha()
        for x in range(opened.width())
        for y in range(opened.height())
    )


def test_icon_grids_are_card_12_and_bar_16() -> None:
    """아이콘 그림 칸 크기 토큰은 카드 12와 바 16 두 값이어야 한다.

    actionGlyph -> 12, barGlyph · narrowIcon · menuIcon -> 16
    """
    metrics = theme.METRICS

    assert metrics["actionGlyph"] == 12
    assert (metrics["barGlyph"], metrics["narrowIcon"], metrics["menuIcon"]) == (16, 16, 16)


def test_icon_button_refuses_a_glyph_size_outside_the_icon_grids(qapp: QApplication) -> None:
    """IconButton은 아이콘 그림 칸 토큰이 아닌 크기 토큰을 받지 않아야 한다.

    setGlyphMetric("cardButton") -> ValueError, "barGlyph" -> glyphSize() == 16
    """
    button = icons.IconButton()

    with pytest.raises(ValueError):
        button.setGlyphMetric("cardButton")
    button.setGlyphMetric("barGlyph")
    assert button.glyphSize() == 16


@pytest.mark.parametrize("state", icons.BUBBLE_STATES)
def test_status_glyph_lines_use_the_action_line_width_on_the_16_grid(
    qapp: QApplication, state: str
) -> None:
    """상태 아이콘(버블) 안쪽 기호의 선은 16 격자에서 동작 아이콘과 같은 1.75 · 둥근 끝 · 둥근 이음이어야 한다.

    상태마다 -> 선을 긋는 그리기 호출의 펜 굵기 == 1.75 · RoundCap · RoundJoin
    """
    lines = [
        d
        for d in _recorded(lambda p: icons._BUBBLE_GLYPHS[state](p, QColor("#ffffff")), 16)
        if d["stroked"]
    ]

    assert lines
    for draw in lines:
        assert draw["width"] == pytest.approx(1.75), f"{state}: {draw['width']}"
        assert draw["cap"] == Qt.PenCapStyle.RoundCap
        assert draw["join"] == Qt.PenJoinStyle.RoundJoin


def test_pick_mark_check_scales_the_status_line_width_with_its_diameter(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """고른 체크 원의 ✓ 선 굵기는 상태 아이콘 선(16 격자 1.75)을 지름 20에 맞춰 키운 2.1875여야 한다.

    썸네일(줄이기 아님) 고른 체크 원을 그림 -> 만든 펜 가운데 테두리(1.5) 밖의 굵기 == [2.1875]
    """
    widths: list[float] = []

    class _CountingPen(QPen):
        def __init__(self, *args: Any) -> None:
            super().__init__(*args)
            widths.append(self.widthF())

    monkeypatch.setattr(card_face, "QPen", _CountingPen)
    host = QWidget()
    thumb = card_face.CardThumbnail(host, False)
    thumb.setFixedSize(40, 40)
    thumb.setPickMark(True, True)
    pixmap = QPixmap(40, 40)
    pixmap.fill(Qt.GlobalColor.transparent)
    thumb.render(pixmap)

    assert theme.METRICS["cardPickMark"] == 20, "전제: 체크 원 지름 20"
    assert [w for w in widths if w != pytest.approx(theme.PICK_MARK_RING_WIDTH)] == [
        pytest.approx(2.1875)
    ]


# ---- 글자 기호 아이콘이 남지 않음 ----

# 기호 글자로 보는 범위 — 화살표 · 기술 기호 · 도형 · 딩벳 · 그림 문자
_SYMBOL_BLOCKS = ((0x2190, 0x21FF), (0x2300, 0x23FF), (0x25A0, 0x25FF), (0x2700, 0x27BF))
# 아이콘이 아니라 키 이름을 적는 글 — 구간 창 순서 바꾸기 안내의 macOS 키(⌥ = Option)
_KEY_TEXTS = {"⌥↑↓"}


def _symbolic(char: str) -> bool:
    code = ord(char)
    return (
        any(low <= code <= high for low, high in _SYMBOL_BLOCKS)
        or code >= 0x1F300
        or (code > 0x7F and unicodedata.category(char) == "So")
    )


def _glyph_strings() -> list[str]:
    """app/의 문자열 상수 가운데 기호 글자가 든 짧은 것(4자 이하 — 아이콘 자리) — 「파일:줄 글」 목록."""
    found = []
    for path in sorted((REPO / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
                if (
                    len(text.strip()) <= 4
                    and any(_symbolic(c) for c in text)
                    and text not in _KEY_TEXTS
                ):
                    found.append(f"{path.relative_to(REPO).as_posix()}:{node.lineno} {text!r}")
    return found


def test_no_icon_is_drawn_with_a_glyph_character() -> None:
    """앱 코드에 기호 글자로 아이콘을 그리는 짧은 문자열이 없어야 한다 — 아이콘은 icons.py의 선 그림이다.

    app/ 아래 파이썬 문자열 상수(f-string 조각 포함) 가운데 화살표 · 기술 기호 · 도형 · 딩벳 · 그림 문자가 든
    4자 이하 -> 없음(키 이름 「⌥↑↓」 제외)
    """
    assert _glyph_strings() == []


def test_glyph_scan_sees_a_glyph_icon(tmp_path: Path) -> None:
    """글자 기호 검사는 f-string 조각의 「↓ 」를 찾아야 한다 — 검사가 살아 있는지 확인한다.

    f"↓ {speed}"를 파싱한 상수 -> 기호 글자로 본다
    """
    tree = ast.parse('label = f"↓ {speed}"')
    texts = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)]

    assert texts == ["↓ "]
    assert any(_symbolic(c) for c in texts[0])


def test_dot_refuses_a_dot_wider_than_the_line(qapp: QApplication) -> None:
    """_dot은 지름이 선 굵기를 넘는 점을 받지 않아야 한다.

    지름 1.8 -> ValueError
    """
    image = QImage(8, 8, QImage.Format.Format_ARGB32_Premultiplied)
    painter = QPainter(image)
    try:
        with pytest.raises(ValueError):
            icons._dot(painter, QColor("#ffffff"), QPointF(4, 4), 1.8)
    finally:
        painter.end()
