"""툴팁 꼬리와 몸통이 한 윤곽인지 — 꼬리 밑변에 테두리 줄이 없고 바깥 윤곽 테두리는 그대로 (#606).

툴팁(`TipBubble`)을 대상 사각형 옆에 직접 띄우고 배율 1 이미지에 `render()`한 화소로 잰다 — 화면 배율과
무관하게 위젯 좌표가 화소 좌표다(#609 리뷰). 창 합성 경계는 재지 않는다 — 소유자 실기.
"""

from collections.abc import Iterator

import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QWidget

import app.theme as theme
from app.widgets.tip_bubble import TipBubble
from tests.app.gui.card_helpers import drop_new_top_levels, snapshot_top_levels

TARGET = QRect(150, 200, 30, 20)  # 대상 사각형(전역) — 화면 가운데쯤이라 바라는 방향 그대로 뜬다
SIDES = ["below", "above"]  # 꼬리 위(몸통이 대상 아래) · 꼬리 아래
SCHEMES = ["dark", "light"]


@pytest.fixture(autouse=True)
def _environment(qapp: QApplication) -> Iterator[None]:
    """테스트가 띄운 창은 끝에 파괴하고 색 스킴을 다크로 되돌린다."""
    before = snapshot_top_levels()
    yield
    drop_new_top_levels(before)
    theme.set_color_scheme("dark")


def _shown(qapp: QApplication, scheme: str, side: str) -> tuple[TipBubble, QImage]:
    theme.set_color_scheme(scheme)
    host = QWidget()
    host.resize(300, 300)
    host.show()
    bubble = TipBubble()
    bubble.showFor(host, "downloads 폴더 경로", rect=TARGET, side=side)
    qapp.processEvents()
    assert bubble.isVisible() and bubble.side == side, "전제: 바라는 방향으로 떴다"
    return bubble, _render(bubble)


def _render(bubble: TipBubble) -> QImage:
    """말풍선을 배율 1인 투명 이미지에 그린다 — `grab()`은 화면 배율(1.25 · 1.5)만큼 큰 이미지를 돌려준다."""
    image = QImage(bubble.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(1.0)
    image.fill(Qt.GlobalColor.transparent)
    bubble.render(image)
    return image


def _border_share(pixel: QColor, scheme_tokens: dict) -> float:
    """면 색 → 테두리 색 사이에서 그 화소가 테두리 쪽으로 간 몫(0 = 면 색, 1 = 테두리 색)."""
    face, border = QColor(scheme_tokens["tipBg"]), QColor(scheme_tokens["borderStrong"])
    span = [border.red() - face.red(), border.green() - face.green(), border.blue() - face.blue()]
    moved = [pixel.red() - face.red(), pixel.green() - face.green(), pixel.blue() - face.blue()]
    return sum(s * m for s, m in zip(span, moved, strict=True)) / sum(s * s for s in span)


def _edge(bubble: TipBubble) -> float:
    """꼬리 쪽 몸통 변의 y(위젯 좌표)."""
    body = bubble.bodyRect()
    return body.top() if bubble.side == "below" else body.bottom()


def _tail_center(bubble: TipBubble) -> int:
    body = bubble.bodyRect()
    low = theme.METRICS["tipRadius"] + theme.METRICS["tipTailWidth"] / 2
    return int(max(body.left() + low, min(bubble._tail, body.right() - low)))


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("side", SIDES)
def test_tail_base_is_filled_with_the_face(qapp: QApplication, scheme: str, side: str) -> None:
    """꼬리 밑변 자리 화소가 꼬리 위 · 아래, 다크 · 라이트 모두에서 면 색이어야 한다.

    꼬리 가운데 ±3열 x 몸통 변 ±1줄 -> 모든 화소의 테두리 몫 < 0.1
    """
    bubble, image = _shown(qapp, scheme, side)
    tokens = theme.current_tokens()
    edge = int(_edge(bubble))
    center = _tail_center(bubble)
    reach = (
        theme.METRICS["tipTailWidth"] // 2 - 3
    )  # 꼬리 빗변은 변 ±1줄에서 가운데 ±5 밖 — 3열 남긴다
    shares = {
        (x, y): _border_share(image.pixelColor(x, y), tokens)
        for x in range(center - reach, center + reach + 1)
        for y in range(edge - 1, edge + 2)
    }
    lined = {point: round(share, 2) for point, share in shares.items() if share >= 0.1}
    assert not lined, f"꼬리 밑변에 테두리 색: {lined}"


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("side", SIDES)
def test_outer_outline_keeps_its_border(qapp: QApplication, scheme: str, side: str) -> None:
    """꼬리 빗변 두 쪽과 꼬리 밖 몸통 변에는 테두리가 그대로 있어야 한다.

    꼬리 길이 가운데 줄의 꼬리 왼쪽 · 오른쪽 반, 꼬리에서 떨어진 몸통 변 열
    -> 각 자리에 테두리 몫 0.4 이상인 불투명 화소가 있다
    """
    bubble, image = _shown(qapp, scheme, side)
    tokens = theme.current_tokens()
    edge = _edge(bubble)
    half = theme.METRICS["tipTailWidth"] // 2
    length = theme.METRICS["tipTail"]
    center = _tail_center(bubble)
    middle = int(edge - length / 2) if side == "below" else int(edge + length / 2)

    def bordered(points: list[tuple[int, int]]) -> bool:
        return any(
            image.pixelColor(x, y).alpha() == 255
            and _border_share(image.pixelColor(x, y), tokens) >= 0.4
            for x, y in points
        )

    left_slant = [(x, middle) for x in range(center - half, center)]
    right_slant = [(x, middle) for x in range(center + 1, center + half + 1)]
    away = center + 3 * half  # 꼬리 밖 몸통 변 — 꼬리 밑변 폭의 세 배 떨어진 열
    assert away < bubble.bodyRect().right() - theme.METRICS["tipRadius"], "전제: 둥근 모서리 앞"
    body_edge = [(away, y) for y in range(int(edge) - 2, int(edge) + 3)]
    assert bordered(left_slant), "꼬리 왼쪽 빗변 테두리"
    assert bordered(right_slant), "꼬리 오른쪽 빗변 테두리"
    assert bordered(body_edge), "꼬리 밖 몸통 변 테두리"
