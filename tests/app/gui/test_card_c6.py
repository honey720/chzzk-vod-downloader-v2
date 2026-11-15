"""카드 C6 — 카드 사이 간격 · 카드 목록 한 칸 스크롤 · 구간 편집 버튼 · 정보 글 툴팁 (#599).

실제 배선을 탄다: 휠은 창 경로(`QTest.wheelEvent`), 카드 버튼은 마우스 클릭 → 카드 시그널 → 목록 릴레이 →
`VodDownloader.onCardSectionEdit` → 구간 창. 구간 창의 기준값 조회는 대역(`basis`, 60fps · 1시간)이다.

기대값은 손으로 계산한 값이다 — 제품의 표기 함수로 만들지 않는다.
"""

from pathlib import Path

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QWidget

from app.viewmodels.data import ContentItem
from app.views.mainWindow import VodDownloader
from app.widgets.widget import ContentItemWidget
from tests.app.gui import card_helpers
from tests.app.gui.test_section_editor import (  # noqa: F401 — 픽스처는 이름으로 쓴다
    _apply_production_qss,
    _destroy_windows,
    _make_item,
    _no_network,
    _pump,
    open_window,
)

OUTER_MARGIN = 12  # 창 바깥 좌우 여백(px) — 카드 왼쪽 끝 ↔ 목록 왼쪽 끝(#580)
CARD_GAP = 12  # 카드 사이(px) — 바깥 좌우 여백과 같다(오너 2026-11-15)
CARD_HEIGHT = 80  # 카드 높이(px, 설계 §11.3)
CELL = CARD_HEIGHT + CARD_GAP  # 카드 목록의 한 칸(px) — 92


@pytest.fixture(autouse=True)
def _hover_from_tests_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """카드 호버는 테스트가 보낸 들어감 · 나감만 받는다 — 실제 포인터 자리에 기대지 않는다 (#587)."""
    card_helpers.hover_from_tests_only(monkeypatch)


def _plain_items(tmp_path: Path, count: int) -> list[ContentItem]:
    return [_make_item(str(tmp_path), title=f"카드 {n:02d}") for n in range(count)]


def _card_any(win: VodDownloader, item: ContentItem) -> ContentItemWidget:
    """목록의 그 카드 — 뷰포트 밖이어도 된다."""
    card = win.listView.widgetFor(item)
    assert card is not None, "전제: 카드가 목록에 있다"
    return card


def _left_in_list(win: VodDownloader, widget: QWidget) -> int:
    """위젯 왼쪽 끝의 x — 카드 목록(스크롤 영역) 왼쪽 끝 기준."""
    return widget.mapTo(win.listView, QPoint(0, 0)).x()


class TestGap:
    """카드 사이 간격은 창 바깥 좌우 여백과 같다(오너 2026-11-15)."""

    def test_gap_between_cards_equals_the_outer_side_margin(self, tmp_path: Path) -> None:
        """이웃한 두 카드 사이 간격이 카드 왼쪽 바깥 여백과 같은 12px이어야 한다.

        접힌 카드 3 · 창 폭 1000 -> 카드 왼쪽 끝 x = 12, 둘째 윗선 − 첫째 아래끝 = 12, 셋째도 같다
        """
        items = _plain_items(tmp_path, 3)
        win = open_window(tmp_path, *items)
        cards = [_card_any(win, item) for item in items]
        assert all(card.isVisible() for card in cards), "전제: 카드 셋이 보인다"
        margin = _left_in_list(win, cards[0])
        gaps = [lower.y() - (upper.y() + upper.height()) for upper, lower in zip(cards, cards[1:])]
        assert margin == OUTER_MARGIN
        assert gaps == [CARD_GAP, CARD_GAP]
        assert gaps[0] == margin
