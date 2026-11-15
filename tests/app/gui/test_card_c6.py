"""카드 C6 — 카드 사이 간격 · 카드 목록 한 칸 스크롤 · 구간 편집 버튼 · 정보 글 툴팁 (#599).

실제 배선을 탄다: 휠은 창 경로(`QTest.wheelEvent`), 카드 버튼은 마우스 클릭 → 카드 시그널 → 목록 릴레이 →
`VodDownloader.onCardSectionEdit` → 구간 창. 구간 창의 기준값 조회는 대역(`basis`, 60fps · 1시간)이다.

기대값은 손으로 계산한 값이다 — 제품의 표기 함수로 만들지 않는다.
"""

from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt, QVariantAnimation
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractSlider, QApplication, QWidget

from app.viewmodels.data import ContentItem
from app.views.mainWindow import VodDownloader
from app.widgets import step_scroll
from app.widgets.widget import ContentItemWidget
from core.models.download_state import DownloadState
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
LIST_TOP = 12  # 카드 목록 위 여백(px) — 카드 사이와 같다(설계 §11.2)
LIST_BOTTOM = 12  # 카드 목록 아래 여백(px)
RING_REACH = 4  # focus ring이 카드 밖으로 나간 px — 띄움 2 + 굵기 2(#561)
WHEEL_NOTCH = 120  # 휠 한 칸의 angleDelta(1/8도 단위 — Qt 문서)
ANIMATION_MS = 120  # 애니메이션 테스트가 쓰는 굴리기 시간(ms)


@pytest.fixture(autouse=True)
def _hover_from_tests_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """카드 호버는 테스트가 보낸 들어감 · 나감만 받는다 — 실제 포인터 자리에 기대지 않는다 (#587)."""
    card_helpers.hover_from_tests_only(monkeypatch)


@pytest.fixture(autouse=True)
def _no_scroll_animation(monkeypatch: pytest.MonkeyPatch) -> None:
    """카드 목록 휠 애니메이션을 끈다(시간 0 — 바로 옮김). 애니메이션을 재는 테스트가 스스로 켠다."""
    monkeypatch.setattr(step_scroll, "SCROLL_ANIMATION_MS", 0)


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

        카드 3 · 창 폭 1000 -> 카드 왼쪽 끝 x = 12, 둘째 윗선 − 첫째 아래끝 = 12, 셋째도 같다
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


# ---- 카드 목록 한 칸 스크롤 ----


def _list_wheel(win: VodDownloader, notches: int) -> None:
    """카드 목록 뷰포트 왼쪽 여백(카드 밖, x=4 · y=10)에 창 경로로 휠을 보낸다 — 음수는 아래로.

    포인터는 옮기지 않는다. 위젯에 `sendEvent`로 보낸 휠은 Qt가 부모로 넘기지 않는다 — 실제 휠처럼
    창이 받는 쪽을 고르게 창 경로(`QTest.wheelEvent`)로 보낸다(168a).
    """
    viewport = win.listView.viewport()
    pos = QPointF(viewport.mapTo(win, QPoint(4, 10)))
    QTest.wheelEvent(win.windowHandle(), pos, QPoint(0, WHEEL_NOTCH * notches), QPoint(0, 0))
    _pump()


def _on_grid(win: VodDownloader) -> bool:
    """카드 목록 위치가 칸 경계(92의 배수)이거나 맨 끝인지."""
    bar = win.listView.verticalScrollBar()
    return bar.value() % CELL == 0 or bar.value() == bar.maximum()


def _off_grid_end(win: VodDownloader) -> None:
    """맨 끝(스크롤 범위 최댓값)이 칸 경계가 아니게 창 높이를 맞춘다 — 높이를 10px씩 줄여 최대 9번."""
    bar = win.listView.verticalScrollBar()
    for _ in range(9):
        if bar.maximum() % CELL:
            return
        win.resize(win.width(), win.height() - 10)
        _pump()
    raise AssertionError("맨 끝이 칸 경계가 아닌 창 높이를 찾지 못했다")


def _focus_card(qtbot: QtBot, win: VodDownloader, card: ContentItemWidget) -> None:
    """창이 활성이 된 뒤 카드에 Tab 초점을 준다(#561 — 활성 전 초점은 사유가 바뀐다)."""
    win.activateWindow()
    qtbot.waitUntil(win.isActiveWindow, timeout=3000)
    card.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(card.hasFocus, timeout=3000)


class TestListScroll:
    """카드 목록의 휠 · 스크롤바는 카드 한 칸(카드 80 + 카드 사이 12 = 92) 단위다(오너 2026-11-14)."""

    @pytest.mark.parametrize(("notches", "expected"), [(1, CELL), (3, 3 * CELL), (-1, 0)])
    def test_one_wheel_notch_moves_one_card_cell(
        self, tmp_path: Path, notches: int, expected: int
    ) -> None:
        """카드 목록 위 휠 한 칸은 카드 한 칸(92)만큼 움직여야 한다 — 한 이벤트에 여러 칸이면 그 칸 수만큼.

        카드 30 · 아래로 1칸 -> 92, 아래로 3칸(angleDelta −360 한 번) -> 276, 맨 위에서 위로 1칸 -> 0
        """
        win = open_window(tmp_path, *_plain_items(tmp_path, 30))
        _list_wheel(win, -notches)
        assert win.listView.verticalScrollBar().value() == expected

    def test_ten_quick_notches_move_ten_cells_with_a_card_top_at_the_list_top(
        self, tmp_path: Path
    ) -> None:
        """휠을 빠르게 열 칸 굴리면 열 칸(920) 움직이고 칸마다 카드 윗선이 목록 위 여백 자리에 와야 한다.

        카드 30 · 아래로 1칸씩 10번 -> k번째 뒤 값 92k, k번째 카드(0부터)의 뷰포트 윗선 = 12
        """
        items = _plain_items(tmp_path, 30)
        win = open_window(tmp_path, *items)
        bar = win.listView.verticalScrollBar()
        for notch in range(1, 11):
            _list_wheel(win, -1)
            assert bar.value() == CELL * notch
            card = _card_any(win, items[notch])
            assert card.y() - bar.value() == LIST_TOP

    def test_the_end_shows_the_last_card_whole_and_steps_back_to_a_cell(
        self, tmp_path: Path
    ) -> None:
        """맨 끝이 칸 경계가 아니면 맨 끝까지 내려가 마지막 카드가 다 보이고, 올리면 앞 칸 경계로 가야 한다.

        카드 20 · 맨 끝이 92의 배수가 아닌 창 높이 · 아래로 1칸씩 40번 -> 값 = 맨 끝, 마지막 카드 아래 끝 =
        뷰포트 높이 − 아래 여백 12; 맨 끝에서 아래로 1칸 -> 그대로; 위로 1칸 -> 92 × (맨 끝 // 92);
        아래로 1칸 -> 맨 끝
        """
        items = _plain_items(tmp_path, 20)
        win = open_window(tmp_path, *items)
        _off_grid_end(win)
        bar = win.listView.verticalScrollBar()
        end = bar.maximum()
        for _ in range(40):
            _list_wheel(win, -1)
        assert bar.value() == end
        last = _card_any(win, items[-1])
        height = win.listView.viewport().height()
        assert last.y() + last.height() - bar.value() == height - LIST_BOTTOM
        _list_wheel(win, -1)
        assert bar.value() == end
        _list_wheel(win, 1)
        assert bar.value() == CELL * (end // CELL)
        _list_wheel(win, -1)
        assert bar.value() == end

    def test_wheel_while_sliding_adds_to_the_target(
        self, qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """카드 목록이 굴러가는 중에 온 휠은 앞 목표에 칸을 더해야 한다.

        SCROLL_ANIMATION_MS 120 · 카드 30 · 아래로 1칸 -> 굴리는 중 · 목표 92 · 값 92 미만,
        굴리는 중 아래로 2칸 -> 목표 276, 끝나면 276 · 목록 아래 애니메이션 0개
        """
        monkeypatch.setattr(step_scroll, "SCROLL_ANIMATION_MS", ANIMATION_MS)
        win = open_window(tmp_path, *_plain_items(tmp_path, 30))
        view = win.listView
        bar = view.verticalScrollBar()
        _list_wheel(win, -1)
        assert view.isAnimating() and view.scrollTarget() == CELL
        assert bar.value() < CELL
        _list_wheel(win, -2)
        assert view.scrollTarget() == 3 * CELL
        qtbot.waitUntil(lambda: not view.isAnimating(), timeout=2000)
        assert bar.value() == 3 * CELL
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert view.findChildren(QVariantAnimation) == []

    def test_pressing_the_handle_while_sliding_stops_the_slide(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """카드 목록이 굴러가는 중에 손잡이를 누르면 굴리기가 멈춰 끄는 자리를 덮지 않아야 한다.

        SCROLL_ANIMATION_MS 120 · 카드 30 · 아래로 3칸 · 굴리는 중 손잡이 누름 · 500으로 끎
        -> 굴리는 중 아님, 처리 뒤에도 500; 놓음 -> 460(가장 가까운 칸 경계 92 × 5)
        """
        win = open_window(tmp_path, *_plain_items(tmp_path, 30))
        monkeypatch.setattr(step_scroll, "SCROLL_ANIMATION_MS", ANIMATION_MS)
        view = win.listView
        bar = view.verticalScrollBar()
        _list_wheel(win, -3)
        assert view.isAnimating(), "전제: 굴리는 중"
        bar.setSliderDown(True)
        bar.setSliderPosition(500)
        assert not view.isAnimating()
        _pump()
        assert bar.value() == 500
        monkeypatch.setattr(step_scroll, "SCROLL_ANIMATION_MS", 0)
        bar.setSliderDown(False)
        _pump()
        assert bar.value() == 5 * CELL

    @pytest.mark.parametrize(("dragged", "expected"), [(130, CELL), (140, 2 * CELL)])
    def test_releasing_the_dragged_handle_snaps_to_a_cell_boundary(
        self, tmp_path: Path, dragged: int, expected: int
    ) -> None:
        """카드 목록 손잡이를 끌다 놓으면 가장 가까운 칸 경계로 맞춰져야 한다.

        카드 30 · 손잡이 누름 · 130으로 끎 -> 놓음 -> 92(130 − 92 = 38 < 184 − 130 = 54);
        140으로 끎 -> 184(140 − 92 = 48 > 184 − 140 = 44)
        """
        win = open_window(tmp_path, *_plain_items(tmp_path, 30))
        bar = win.listView.verticalScrollBar()
        bar.setSliderDown(True)
        bar.setSliderPosition(dragged)
        _pump()
        assert bar.value() == dragged
        bar.setSliderDown(False)
        _pump()
        assert bar.value() == expected

    def test_arrow_is_one_cell_and_page_drops_the_partial_cell(self, tmp_path: Path) -> None:
        """스크롤바 화살표는 한 칸, 바탕 누름(페이지)은 뷰포트 높이에서 넘친 칸을 버린 칸 경계여야 한다.

        카드 30 · singleStep 92, 화살표 한 걸음 -> 92; 0에서 페이지 한 걸음 -> 92 × (뷰포트 높이 // 92)
        (92보다 작으면 92), 거기서 페이지 뒤로 -> 0
        """
        win = open_window(tmp_path, *_plain_items(tmp_path, 30))
        bar = win.listView.verticalScrollBar()
        assert bar.singleStep() == CELL
        bar.triggerAction(QAbstractSlider.SliderAction.SliderSingleStepAdd)
        _pump()
        assert bar.value() == CELL
        bar.setValue(0)
        _pump()
        page = max(CELL, CELL * (win.listView.viewport().height() // CELL))
        bar.triggerAction(QAbstractSlider.SliderAction.SliderPageStepAdd)
        _pump()
        assert bar.value() == page
        bar.triggerAction(QAbstractSlider.SliderAction.SliderPageStepSub)
        _pump()
        assert bar.value() == 0

    @pytest.mark.parametrize(
        ("action", "expected"),
        [("up", "앞 칸 경계"), ("down", "맨 끝")],
        ids=["위로", "아래로"],
    )
    def test_arrow_from_the_end_off_the_grid_lands_on_a_boundary(
        self, tmp_path: Path, action: str, expected: str
    ) -> None:
        """칸 경계가 아닌 맨 끝에서 스크롤바 화살표를 누르면 칸 경계(위로)나 맨 끝(아래로)에 서야 한다.

        카드 30 · 맨 끝이 92의 배수가 아닌 창 높이 · 맨 끝에서 화살표 위로 한 걸음 -> 92 × (맨 끝 // 92);
        아래로 한 걸음 -> 맨 끝 그대로
        """
        win = open_window(tmp_path, *_plain_items(tmp_path, 30))
        _off_grid_end(win)
        bar = win.listView.verticalScrollBar()
        end = bar.maximum()
        bar.setValue(end)
        _pump()
        step = (
            QAbstractSlider.SliderAction.SliderSingleStepSub
            if action == "up"
            else QAbstractSlider.SliderAction.SliderSingleStepAdd
        )
        bar.triggerAction(step)
        _pump()
        assert bar.value() == (CELL * (end // CELL) if expected == "앞 칸 경계" else end)

    def test_arrow_keys_keep_the_list_on_cell_boundaries(
        self, qtbot: QtBot, tmp_path: Path
    ) -> None:
        """↑↓로 초점 카드를 옮기면 목록이 그 카드(· focus ring)를 보이게 따라가되 칸 경계에 서야 한다.

        카드 20 · 첫 카드에 Tab 초점 · ↓ 19번 · ↑ 19번 -> 키마다 위치가 92의 배수이거나 맨 끝, 초점 카드의
        위끝 − 4 ≥ 위치 · 아래끝 + 4 ≤ 위치 + 뷰포트 높이(키보드 초점일 때), 굴리는 중 아님; 끝에 오면 맨 끝 ·
        다시 맨 위에서 0
        """
        items = _plain_items(tmp_path, 20)
        win = open_window(tmp_path, *items)
        cards = [_card_any(win, item) for item in items]
        view = win.listView
        bar = view.verticalScrollBar()
        _focus_card(qtbot, win, cards[0])
        height = view.viewport().height()

        def check(card: ContentItemWidget) -> None:
            qtbot.waitUntil(card.hasFocus, timeout=3000)
            reach = RING_REACH if card.hasKeyFocus() else 0
            assert _on_grid(win), f"위치 {bar.value()}"
            assert not view.isAnimating()
            assert card.y() - reach >= bar.value()
            assert card.y() + card.height() + reach <= bar.value() + height

        for index in range(1, 20):
            QTest.keyClick(cards[index - 1], Qt.Key.Key_Down)
            _pump()
            check(cards[index])
        assert bar.value() == bar.maximum()
        for index in range(18, -1, -1):
            QTest.keyClick(cards[index + 1], Qt.Key.Key_Up)
            _pump()
            check(cards[index])
        assert bar.value() == 0

    def test_filter_sort_removal_and_resize_keep_the_list_on_a_boundary(
        self, tmp_path: Path
    ) -> None:
        """필터 · 정렬 · 지우기 · 창 크기 바꾸기로 목록 높이가 바뀌어도 위치가 칸 경계여야 한다.

        카드 30(짝수 번째 완료) · 위치 276 -> 완료 필터 · 전체로 · 제목 내림차순 · 카드 둘 지움 · 맨 끝으로 간
        뒤 창 높이 +37 · −53 · +101 각각 뒤 위치가 92의 배수이거나 맨 끝
        """
        items = _plain_items(tmp_path, 30)
        for index in range(0, 30, 2):
            items[index].downloadState = DownloadState.FINISHED
        win = open_window(tmp_path, *items)
        bar = win.listView.verticalScrollBar()
        bar.setValue(3 * CELL)
        _pump()
        order = win.contentManager.order
        steps = {
            "완료 필터": lambda: order.setFilter("finished"),
            "전체로": lambda: order.setFilter("all"),
            "제목 내림차순": lambda: order.setSort("title", True),
            "지움": lambda: win.contentManager.removeItems(items[10:12]),
            "맨 끝": lambda: bar.setValue(bar.maximum()),
            "창 +37": lambda: win.resize(win.width(), win.height() + 37),
            "창 −53": lambda: win.resize(win.width(), win.height() - 53),
            "창 +101": lambda: win.resize(win.width(), win.height() + 101),
        }
        for name, step in steps.items():
            step()
            _pump()
            assert _on_grid(win), f"{name} 뒤 위치 {bar.value()}(맨 끝 {bar.maximum()})"

    def test_the_list_at_the_end_stays_at_the_end_when_the_range_changes(
        self, tmp_path: Path
    ) -> None:
        """맨 끝에 있던 카드 목록은 창 높이 · 카드 수가 바뀌어도 새 맨 끝에 남아야 한다.

        카드 30 · 맨 끝 -> 창 높이 −53 · +37 · 카드 하나 더 · 카드 둘 지움 각각 뒤 위치 = 맨 끝;
        대조: 위치 276 -> 창 높이 −53 뒤 276(맨 끝으로 가지 않음)
        """
        items = _plain_items(tmp_path, 30)
        win = open_window(tmp_path, *items)
        bar = win.listView.verticalScrollBar()
        bar.setValue(bar.maximum())
        _pump()
        steps = {
            "창 −53": lambda: win.resize(win.width(), win.height() - 53),
            "창 +37": lambda: win.resize(win.width(), win.height() + 37),
            "카드 더함": lambda: win.contentManager.model.addItem(
                _make_item(str(tmp_path), title="더한 카드")
            ),
            "지움": lambda: win.contentManager.removeItems(items[3:5]),
        }
        for name, step in steps.items():
            before = bar.maximum()
            step()
            _pump()
            assert bar.maximum() != before, f"전제: {name}로 맨 끝이 바뀐다"
            assert bar.value() == bar.maximum(), f"{name} 뒤 {bar.value()}(맨 끝 {bar.maximum()})"
        bar.setValue(3 * CELL)
        _pump()
        win.resize(win.width(), win.height() - 53)
        _pump()
        assert bar.value() == 3 * CELL
