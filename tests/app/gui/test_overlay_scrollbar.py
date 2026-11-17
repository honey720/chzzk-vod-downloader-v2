"""카드 목록의 겹친 스크롤바(overlay scrollbar) — 자리 · 흐려짐 · 나타남 · 동작 (#611, 오너 실기 2026-11-15).

실제 배선을 탄다: 메인 창 · 카드 목록 · `StepScroller` · 겹친 막대. 휠은 창 경로(`QTest.wheelEvent`), 막대 누름 ·
끌기는 막대에 합성 마우스 이벤트(포인터는 옮기지 않는다), 들어감 · 나감은 `card_helpers.enter` · `leave`다.
기대값은 손으로 정한 값이다(행 안쪽 여백 12, 굵기 6 · 8, 한 칸 81).
"""

from collections.abc import Callable, Iterator

import pytest
from pytestqt.qtbot import QtBot
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt, QVariantAnimation
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

import app.viewmodels.card_display as card_display
from app.views.mainWindow import VodDownloader
from app.widgets import overlay_scrollbar, step_scroll
from app.widgets.overlay_scrollbar import OverlayScrollBar
from tests.app.gui import card_helpers
from tests.app.gui.card_helpers import enter, leave
from tests.app.gui.test_list_scrollbar_layout import (  # noqa: F401 — 픽스처는 이름으로 쓴다
    MANY,
    _apply_production_qss,
    _destroy_windows,
    _pump,
    make_window,
)

ROW_PAD = 12  # 행 안쪽 오른쪽 여백 = 막대 자리 폭(px) — 지시 값
THIN = 6  # 평소 막대 굵기(px)
THICK = 8  # 포인터를 대거나 끄는 동안의 굵기(px)
CELL = 81  # 카드 목록 한 칸(px) — 행 80 + 구분선 1
WHEEL_NOTCH = 120  # 휠 한 칸의 angleDelta
HIDE_MS = 60  # 테스트의 흐려지기 전 대기(ms) — 제품 값(1000)을 줄인다
FADE_MS = 30  # 테스트의 나타남 · 흐려짐 시간(ms)


@pytest.fixture(autouse=True)
def _hover_from_tests_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """카드 호버는 테스트가 보낸 들어감 · 나감만 받는다 — 실제 포인터 자리에 기대지 않는다 (#587)."""
    card_helpers.hover_from_tests_only(monkeypatch)


@pytest.fixture(autouse=True)
def _no_high_contrast(monkeypatch: pytest.MonkeyPatch) -> None:
    """OS 고대비 설정을 끈 것으로 고정한다 — 고대비 테스트만 다시 켠다(실행 머신 설정에 기대지 않는다)."""
    monkeypatch.setattr(overlay_scrollbar, "high_contrast", lambda: False)


@pytest.fixture(autouse=True)
def _quick_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    """흐려지기 전 대기 · 나타남 · 흐려짐 · 굵어짐 시간을 줄이고 휠 굴리기는 바로 옮긴다(창을 만들기 전에)."""
    monkeypatch.setattr(overlay_scrollbar, "HIDE_DELAY_MS", HIDE_MS)
    monkeypatch.setattr(overlay_scrollbar, "FADE_IN_MS", FADE_MS)
    monkeypatch.setattr(overlay_scrollbar, "FADE_OUT_MS", FADE_MS)
    monkeypatch.setattr(overlay_scrollbar, "THICKEN_MS", FADE_MS)
    monkeypatch.setattr(step_scroll, "SCROLL_ANIMATION_MS", 0)


@pytest.fixture
def build(request: pytest.FixtureRequest) -> Callable[..., VodDownloader]:
    """창 만들기 — 스크롤바 레이아웃 테스트의 `make_window`를 그대로 쓴다(만든 창은 그 픽스처가 지운다)."""
    return request.getfixturevalue("make_window")


@pytest.fixture
def animations() -> Iterator[Callable[[bool], None]]:
    """애니메이션 설정을 정한다 — 창을 만들기 전에 부른다. 끝나면 설정에서 다시 읽게 되돌린다."""

    def use(on: bool) -> None:
        card_display.use(card_display.CardDisplay(animations=on))

    yield use
    card_display.use(None)


def _overlay(win: VodDownloader) -> OverlayScrollBar:
    return win.listView.overlayScrollBar()


def _right_in_window(win: VodDownloader, widget: QWidget) -> int:
    """위젯 오른쪽 끝(끝 포함 안 함)의 x — 창 좌표."""
    return widget.mapTo(win, QPoint(widget.width(), 0)).x()


def _wheel_list(win: VodDownloader, notches: int) -> None:
    """카드 목록 뷰포트 왼쪽(x=4 · y=10)에 창 경로로 휠을 보낸다 — 음수는 아래로."""
    pos = QPointF(win.listView.viewport().mapTo(win, QPoint(4, 10)))
    QTest.wheelEvent(win.windowHandle(), pos, QPoint(0, WHEEL_NOTCH * notches), QPoint(0, 0))
    _pump()


def _mouse(widget: QWidget, kind: QEvent.Type, y: float) -> None:
    """막대 가운데 x · y 자리에 왼쪽 버튼 합성 마우스 이벤트를 보낸다(포인터는 옮기지 않는다)."""
    pos = QPointF(widget.width() / 2, y)
    held = (
        Qt.MouseButton.NoButton
        if kind == QEvent.Type.MouseButtonRelease
        else Qt.MouseButton.LeftButton
    )
    event = QMouseEvent(
        kind,
        pos,
        QPointF(widget.mapToGlobal(pos.toPoint())),
        Qt.MouseButton.LeftButton,
        held,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(widget, event)
    _pump()


def _hovered_card_buttons(win: VodDownloader) -> list[QWidget]:
    """첫 카드에 들어가 보이는 카드 버튼들 — 보이는지 먼저 단언한다."""
    card = win.listView.widgetFor(win.contentManager.model.itemAt(0))
    enter(card)
    _pump()
    buttons = [b for b in (card.editButton, card.sectionButton, card.deleteButton) if b.isVisible()]
    assert buttons, "전제: 호버한 대기 카드에 버튼이 보인다"
    return buttons


# ---------------------------------------------------------------- 자리


class TestPlace:
    def test_card_buttons_and_text_keep_their_x_with_and_without_the_scrollbar(
        self, build: Callable[..., VodDownloader], animations: Callable[[bool], None]
    ) -> None:
        """카드 버튼 · 3줄 오른쪽 끝의 x가 스크롤바 유무와 상관없이 같아야 한다.

        카드 2장(스크롤바 없음) · 40장(있음), 같은 창 폭, 첫 카드 호버 -> 버튼마다 오른쪽 끝 x 같음,
        3줄 오른쪽 끝 x 같음 = 창 폭 − 12
        """
        animations(False)
        few, many = build(cards=2), build(cards=MANY)
        assert few.width() == many.width(), "전제: 두 창의 폭이 같다"
        assert not _overlay(few).isVisible() and _overlay(many).isVisible(), "전제: 스크롤바 유무"
        rights = []
        for win in (few, many):
            buttons = _hovered_card_buttons(win)
            card = win.listView.widgetFor(win.contentManager.model.itemAt(0))
            rights.append(
                (
                    [_right_in_window(win, b) for b in buttons],
                    _right_in_window(win, card.statusRow),
                )
            )
        assert rights[0] == rights[1]
        assert rights[1][1] == many.width() - ROW_PAD

    @pytest.mark.parametrize("cards", [2, MANY], ids=["스크롤바 없음", "스크롤바 있음"])
    def test_rows_are_as_wide_as_the_list(
        self, build: Callable[..., VodDownloader], cards: int
    ) -> None:
        """행 폭이 스크롤바 유무와 상관없이 목록 폭과 같아야 한다.

        카드 2장 · 40장 -> 카드마다 폭 == 목록 폭 == 창 폭
        """
        win = build(cards=cards)
        view = win.listView
        widths = {view.widgetFor(win.contentManager.model.itemAt(r)).width() for r in range(cards)}
        assert widths == {view.width()}
        assert view.width() == win.width()

    def test_bar_lies_outside_the_card_buttons(
        self,
        build: Callable[..., VodDownloader],
        animations: Callable[[bool], None],
        qtbot: QtBot,
    ) -> None:
        """막대 자리는 창 오른쪽 끝 12px이고 굵어진 손잡이도 카드 버튼 오른쪽 끝보다 오른쪽이어야 한다.

        카드 40장, 첫 카드 호버, 막대 자리 들어감 -> 막대 자리 = (창 폭 − 12, 창 폭), 손잡이 굵기 8 ·
        손잡이 왼쪽 끝(창 좌표) = 창 폭 − 10 > 버튼 오른쪽 끝 최댓값 = 창 폭 − 12
        """
        animations(False)
        win = build(cards=MANY)
        bar = _overlay(win)
        buttons = _hovered_card_buttons(win)
        enter(bar)
        qtbot.waitUntil(lambda: bar.thickness() == THICK)
        left = bar.mapTo(win, QPoint(0, 0)).x()
        assert (left, _right_in_window(win, bar)) == (win.width() - ROW_PAD, win.width())
        handle = bar.handleRect()
        assert handle.width() == THICK
        assert left + handle.left() == win.width() - ROW_PAD + (ROW_PAD - THICK) / 2
        assert max(_right_in_window(win, b) for b in buttons) <= left + handle.left()

    def test_no_bar_when_every_card_shows(self, build: Callable[..., VodDownloader]) -> None:
        """카드가 다 보이면 막대가 없고 그 자리는 카드가 받아야 한다.

        카드 2장 -> 막대 숨음, 창 오른쪽 끝 − 6 · 첫 카드 가운데 높이 자리의 위젯 = 첫 카드 안
        """
        win = build(cards=2)
        assert _overlay(win).isHidden()
        card = win.listView.widgetFor(win.contentManager.model.itemAt(0))
        point = QPoint(win.width() - ROW_PAD // 2, card.mapTo(win, card.rect().center()).y())
        under = win.childAt(point)
        assert under is card or card.isAncestorOf(under)


# ---------------------------------------------------------------- 흐려짐 · 나타남


class TestFade:
    def test_bar_starts_faded_and_appears_on_wheel_then_fades_again(
        self,
        build: Callable[..., VodDownloader],
        animations: Callable[[bool], None],
        qtbot: QtBot,
    ) -> None:
        """애니메이션이 켜져 있으면 막대는 흐린 채 시작해 휠에 나타나고 가만히 두면 다시 흐려져야 한다.

        카드 40장 -> 불투명도 0, 목록에 휠 아래로 1칸 -> 값 81 · 불투명도 1까지 오름, 그 뒤 가만히 -> 0
        """
        animations(True)
        win = build(cards=MANY)
        bar = _overlay(win)
        assert bar.isVisible() and bar.opacity() == 0.0
        _wheel_list(win, -1)
        assert win.listView.verticalScrollBar().value() == CELL
        qtbot.waitUntil(lambda: bar.opacity() == 1.0)
        qtbot.waitUntil(lambda: bar.opacity() == 0.0)

    def test_keyboard_scroll_works_while_faded_and_reveals_the_bar(
        self,
        build: Callable[..., VodDownloader],
        animations: Callable[[bool], None],
        qtbot: QtBot,
    ) -> None:
        """막대가 흐려져 있어도 키보드 스크롤은 그대로 돌고 막대가 나타나야 한다.

        카드 40장, 불투명도 0, 목록에 ↓ 키 -> 값 81, 불투명도 1까지 오름
        """
        animations(True)
        win = build(cards=MANY)
        bar = _overlay(win)
        assert bar.opacity() == 0.0, "전제: 흐려져 있다"
        key = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(win.listView, key)
        _pump()
        assert win.listView.verticalScrollBar().value() == CELL
        qtbot.waitUntil(lambda: bar.opacity() == 1.0)

    def test_hover_reveals_and_thickens_and_leaving_thins_and_fades(
        self,
        build: Callable[..., VodDownloader],
        animations: Callable[[bool], None],
        qtbot: QtBot,
    ) -> None:
        """막대 자리에 들어가면 나타나 굵어지고 머무는 동안 흐려지지 않으며 떠나면 얇아지고 흐려져야 한다.

        카드 40장, 들어감 -> 불투명도 1 · 굵기 8, 흐려지기 전 대기의 4배 머묾 -> 불투명도 1,
        나감 -> 굵기 6 · 불투명도 0
        """
        animations(True)
        win = build(cards=MANY)
        bar = _overlay(win)
        enter(bar)
        qtbot.waitUntil(lambda: bar.opacity() == 1.0 and bar.thickness() == THICK)
        qtbot.wait(HIDE_MS * 4)
        assert bar.opacity() == 1.0
        leave(bar)
        qtbot.waitUntil(lambda: bar.thickness() == THIN)
        qtbot.waitUntil(lambda: bar.opacity() == 0.0)

    def test_animations_off_shows_a_steady_bar_and_changes_at_once(
        self, build: Callable[..., VodDownloader], animations: Callable[[bool], None]
    ) -> None:
        """애니메이션을 끄면 막대는 늘 보이고 굵기는 바로 바뀌어야 한다.

        애니메이션 끔 · 카드 40장 -> 불투명도 1, 휠 뒤 흐려지기 전 대기의 4배 -> 1, 들어감 -> 굵기 8(바로),
        나감 -> 6(바로)
        """
        animations(False)
        win = build(cards=MANY)
        bar = _overlay(win)
        assert bar.opacity() == 1.0
        _wheel_list(win, -1)
        QTest.qWait(HIDE_MS * 4)
        assert bar.opacity() == 1.0
        enter(bar)
        assert bar.thickness() == THICK
        leave(bar)
        assert bar.thickness() == THIN
        assert bar.findChildren(QVariantAnimation) == []

    def test_animation_objects_are_gone_after_they_finish(
        self,
        build: Callable[..., VodDownloader],
        animations: Callable[[bool], None],
        qtbot: QtBot,
    ) -> None:
        """나타남 · 굵어짐 애니메이션 객체는 끝나면 지워져야 한다.

        카드 40장, 들어감 -> 불투명도 1 · 굵기 8까지 기다림 -> 지연 삭제 처리 뒤 막대 아래 애니메이션 0개
        """
        animations(True)
        win = build(cards=MANY)
        bar = _overlay(win)
        enter(bar)
        qtbot.waitUntil(lambda: bar.opacity() == 1.0 and bar.thickness() == THICK)
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert bar.findChildren(QVariantAnimation) == []


# ---------------------------------------------------------------- 동작


class TestDrive:
    def test_dragging_the_handle_snaps_to_a_cell_and_never_fades(
        self,
        build: Callable[..., VodDownloader],
        animations: Callable[[bool], None],
        qtbot: QtBot,
    ) -> None:
        """손잡이를 끄는 동안은 흐려지지 않고 놓으면 칸 경계에 서야 한다.

        카드 40장, 손잡이 가운데 누름 -> 원래 스크롤바 눌림, 막대 높이의 1/3 + 7px 아래로 끎 ->
        흐려지기 전 대기의 4배 동안 불투명도 1, 놓음 -> 값 > 0 · 81의 배수 또는 맨 끝, 눌림 풀림
        """
        animations(True)
        win = build(cards=MANY)
        bar = _overlay(win)
        scroll = win.listView.verticalScrollBar()
        start = bar.handleRect().center().y()
        _mouse(bar, QEvent.Type.MouseButtonPress, start)
        assert scroll.isSliderDown()
        _mouse(bar, QEvent.Type.MouseMove, start + bar.height() / 3 + 7)
        qtbot.waitUntil(lambda: bar.opacity() == 1.0)
        qtbot.wait(HIDE_MS * 4)
        assert bar.opacity() == 1.0
        _mouse(bar, QEvent.Type.MouseButtonRelease, start + bar.height() / 3 + 7)
        assert not scroll.isSliderDown()
        value = scroll.value()
        assert value > 0
        assert value % CELL == 0 or value == scroll.maximum()

    def test_pressing_below_the_handle_steps_to_a_cell(
        self, build: Callable[..., VodDownloader], animations: Callable[[bool], None]
    ) -> None:
        """손잡이 아래 바탕을 누르면 아래로 움직여 칸 경계에 서야 한다.

        카드 40장, 값 0, 막대 맨 아래 − 2px 누름 → 뗌 -> 값 > 0 · 81의 배수 또는 맨 끝
        """
        animations(False)
        win = build(cards=MANY)
        bar = _overlay(win)
        scroll = win.listView.verticalScrollBar()
        assert scroll.value() == 0 and bar.handleRect().bottom() < bar.height() - 2, "전제"
        _mouse(bar, QEvent.Type.MouseButtonPress, bar.height() - 2)
        _mouse(bar, QEvent.Type.MouseButtonRelease, bar.height() - 2)
        value = scroll.value()
        assert value > 0
        assert value % CELL == 0 or value == scroll.maximum()

    def test_wheel_over_the_bar_moves_one_cell(
        self, build: Callable[..., VodDownloader], animations: Callable[[bool], None]
    ) -> None:
        """흐려진 막대 자리 위의 휠 한 칸도 카드 한 칸만큼 움직여야 한다.

        애니메이션 켬(흐려짐) · 카드 40장, 막대 자리 가운데 y=30에 창 경로로 아래로 1칸 -> 값 81
        """
        animations(True)
        win = build(cards=MANY)
        bar = _overlay(win)
        assert bar.opacity() == 0.0, "전제: 흐려져 있다"
        pos = QPointF(bar.mapTo(win, QPoint(bar.width() // 2, 30)))
        assert win.childAt(pos.toPoint()) is bar, "전제: 그 자리는 막대다"
        QTest.wheelEvent(win.windowHandle(), pos, QPoint(0, -WHEEL_NOTCH), QPoint(0, 0))
        _pump()
        assert win.listView.verticalScrollBar().value() == CELL


# ---------------------------------------------------------------- 목록 안 포인터 (오너 2026-11-15)


class TestPointerInList:
    def test_entering_the_list_reveals_the_thin_bar(
        self,
        build: Callable[..., VodDownloader],
        animations: Callable[[bool], None],
        qtbot: QtBot,
    ) -> None:
        """포인터가 카드 목록(뷰포트)에 들어가면 흐려져 있던 막대가 얇은 굵기로 나타나야 한다.

        애니메이션 켬 · 카드 40장, 불투명도 0, 뷰포트에 들어감 -> 불투명도 1, 굵기 6
        """
        animations(True)
        win = build(cards=MANY)
        bar = _overlay(win)
        assert bar.isVisible() and bar.opacity() == 0.0, "전제: 흐려져 있다"
        enter(win.listView.viewport())
        qtbot.waitUntil(lambda: bar.opacity() == 1.0)
        assert bar.thickness() == THIN

    def test_bar_does_not_fade_while_the_pointer_stays_in_the_list(
        self,
        build: Callable[..., VodDownloader],
        animations: Callable[[bool], None],
        qtbot: QtBot,
    ) -> None:
        """포인터가 목록 안에 있는 동안은 가만히 두어도 · 굴려도 막대가 흐려지지 않아야 한다.

        애니메이션 켬 · 카드 40장, 뷰포트에 들어감 -> 불투명도 1, 흐려지기 전 대기의 4배 -> 1,
        휠 아래로 1칸 · 다시 4배 -> 1
        """
        animations(True)
        win = build(cards=MANY)
        bar = _overlay(win)
        enter(win.listView.viewport())
        qtbot.waitUntil(lambda: bar.opacity() == 1.0)
        qtbot.wait(HIDE_MS * 4)
        assert bar.opacity() == 1.0
        _wheel_list(win, -1)
        assert win.listView.verticalScrollBar().value() == CELL, "전제: 굴렀다"
        qtbot.wait(HIDE_MS * 4)
        assert bar.opacity() == 1.0

    def test_leaving_the_list_fades_the_bar(
        self,
        build: Callable[..., VodDownloader],
        animations: Callable[[bool], None],
        qtbot: QtBot,
    ) -> None:
        """포인터가 목록 밖으로 나가면 막대가 흐려져야 한다.

        애니메이션 켬 · 카드 40장, 뷰포트에 들어감 -> 불투명도 1, 뷰포트에서 나감 -> 0
        """
        animations(True)
        win = build(cards=MANY)
        bar = _overlay(win)
        enter(win.listView.viewport())
        qtbot.waitUntil(lambda: bar.opacity() == 1.0)
        leave(win.listView.viewport())
        qtbot.waitUntil(lambda: bar.opacity() == 0.0)

    def test_moving_from_the_list_onto_the_bar_keeps_it_shown(
        self,
        build: Callable[..., VodDownloader],
        animations: Callable[[bool], None],
        qtbot: QtBot,
    ) -> None:
        """목록에서 막대 자리로 옮겨 가면(뷰포트 나감 → 막대 들어감) 막대가 흐려지지 않고 굵어져야 한다.

        애니메이션 켬 · 카드 40장, 뷰포트 들어감 -> 1, 뷰포트 나감 · 막대 들어감 -> 굵기 8,
        흐려지기 전 대기의 4배 -> 불투명도 1
        """
        animations(True)
        win = build(cards=MANY)
        bar = _overlay(win)
        enter(win.listView.viewport())
        qtbot.waitUntil(lambda: bar.opacity() == 1.0)
        leave(win.listView.viewport())
        enter(bar)
        qtbot.waitUntil(lambda: bar.thickness() == THICK)
        qtbot.wait(HIDE_MS * 4)
        assert bar.opacity() == 1.0

    def test_nothing_to_scroll_shows_no_bar_when_the_pointer_enters(
        self,
        build: Callable[..., VodDownloader],
        animations: Callable[[bool], None],
        qtbot: QtBot,
    ) -> None:
        """카드가 다 보여 굴릴 게 없으면 포인터가 목록에 들어가도 막대가 나타나지 않아야 한다.

        애니메이션 켬 · 카드 2장(범위 없음), 뷰포트에 들어감 · 흐려지기 전 대기의 2배 -> 막대 숨음 · 불투명도 0
        """
        animations(True)
        win = build(cards=2)
        bar = _overlay(win)
        scroll = win.listView.verticalScrollBar()
        assert scroll.maximum() == scroll.minimum(), "전제: 굴릴 게 없다"
        enter(win.listView.viewport())
        qtbot.wait(HIDE_MS * 2)
        assert not bar.isVisible()
        assert bar.opacity() == 0.0


# ---------------------------------------------------------------- 고대비


class TestHighContrast:
    def test_high_contrast_keeps_the_bar_fully_shown(
        self,
        build: Callable[..., VodDownloader],
        animations: Callable[[bool], None],
        monkeypatch: pytest.MonkeyPatch,
        qtbot: QtBot,
    ) -> None:
        """OS가 고대비면 애니메이션이 켜져 있어도 막대가 늘 불투명도 1이어야 한다.

        고대비 참 · 애니메이션 켬 · 카드 40장 -> 불투명도 1, 휠 뒤 흐려지기 전 대기의 4배 -> 1
        """
        monkeypatch.setattr(overlay_scrollbar, "high_contrast", lambda: True)
        animations(True)
        win = build(cards=MANY)
        bar = _overlay(win)
        assert bar.opacity() == 1.0
        _wheel_list(win, -1)
        qtbot.wait(HIDE_MS * 4)
        assert bar.opacity() == 1.0

    def test_turning_high_contrast_on_while_running_shows_the_bar(
        self,
        build: Callable[..., VodDownloader],
        animations: Callable[[bool], None],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """실행 중에 고대비가 켜지면(설정 바뀜 시그널) 흐려져 있던 막대가 바로 불투명도 1이어야 한다.

        고대비 거짓 · 애니메이션 켬 · 카드 40장 -> 0, 고대비 참 · contrastPreferenceChanged(HighContrast) -> 1
        """
        contrast = [False]
        monkeypatch.setattr(overlay_scrollbar, "high_contrast", lambda: contrast[0])
        animations(True)
        win = build(cards=MANY)
        bar = _overlay(win)
        assert bar.opacity() == 0.0, "전제: 흐려져 있다"
        contrast[0] = True
        hints = QApplication.styleHints()
        hints.accessibility().contrastPreferenceChanged.emit(Qt.ContrastPreference.HighContrast)
        _pump()
        assert bar.opacity() == 1.0


# ---------------------------------------------------------------- 색


def _handle_row(win: VodDownloader) -> list[str]:
    """손잡이 세로 가운데 줄의 안쪽 화소 색(왼쪽 끝 · 오른쪽 끝 한 칸씩 뺌) — 창을 그려서 읽는다."""
    bar = _overlay(win)
    assert bar.isVisible() and bar.opacity() == 1.0, "전제: 막대가 다 보인다"
    rect = bar.handleRect()
    image = win.grab().toImage()
    ratio = image.devicePixelRatio()
    y = int(rect.center().y())
    colours = []
    for x in range(int(rect.left()) + 1, int(rect.right()) - 1):
        point = bar.mapTo(win, QPoint(x, y))
        colours.append(image.pixelColor(int(point.x() * ratio), int(point.y() * ratio)).name())
    assert colours, "전제: 손잡이 안쪽 화소가 있다"
    return colours


def _card_under_handle(win: VodDownloader) -> QWidget:
    """손잡이 세로 가운데 바로 아래(겹쳐 그려진 자리)에 놓인 카드."""
    bar = _overlay(win)
    viewport = win.listView.viewport()
    point = bar.mapTo(viewport, bar.handleRect().center().toPoint())
    card = next(
        (
            c
            for c in (
                win.listView.widgetFor(win.contentManager.model.itemAt(r)) for r in range(MANY)
            )
            if c is not None
            and c.isVisible()
            and c.geometry().contains(c.parentWidget().mapFrom(viewport, point))
        ),
        None,
    )
    assert card is not None, "전제: 손잡이 아래에 카드가 있다"
    return card


class TestHandleColour:
    def test_handle_colour_is_the_same_over_a_plain_hovered_and_picked_card(
        self,
        build: Callable[..., VodDownloader],
        animations: Callable[[bool], None],
    ) -> None:
        """손잡이 화소는 아래 카드가 평소 · 호버 · 고름 · 고름+호버여도 평소 손잡이 색 그대로여야 한다(#611 176g).

        애니메이션 끔 · 카드 40장, 아래로 10칸 굴림(손잡이가 선택 줄 아래 목록 가운데로),
        포인터 목록 밖 -> 손잡이 안쪽 화소 전부 scrollHandle,
        뷰포트 · 손잡이 아래 카드에 들어감 -> 같음, 선택 모드 · 그 카드 고름 -> 같음, 카드에서 나감 -> 같음
        """
        from app.theme import current_tokens

        animations(False)
        win = build(cards=MANY)
        _wheel_list(win, -10)
        assert win.listView.verticalScrollBar().value() == CELL * 10, "전제: 굴렀다"
        want = current_tokens()["scrollHandle"]
        plain = _handle_row(win)
        assert set(plain) == {want}
        card = _card_under_handle(win)
        enter(win.listView.viewport())
        enter(card)
        _pump()
        assert win.listView.hoverCard() is card, "전제: 손잡이 아래 카드가 호버다"
        assert _handle_row(win) == plain, "카드 호버"
        win.listView.setSelecting(True)
        win.listView.cardPicked(card, Qt.KeyboardModifier.NoModifier)
        _pump()
        assert card.isSelected(), "전제: 손잡이 아래 카드를 골랐다"
        assert _handle_row(win) == plain, "고름 + 호버"
        leave(card)
        _pump()
        assert _handle_row(win) == plain, "고름"

    def test_handle_turns_the_hover_colour_only_when_the_bar_itself_is_pointed(
        self,
        build: Callable[..., VodDownloader],
        animations: Callable[[bool], None],
    ) -> None:
        """손잡이는 포인터가 막대 자리에 들어왔을 때만 scrollHandleHover여야 한다.

        애니메이션 끔 · 카드 40장, 뷰포트에 들어감 -> scrollHandle, 뷰포트 나감 · 막대에 들어감 -> scrollHandleHover
        """
        from app.theme import current_tokens

        animations(False)
        win = build(cards=MANY)
        tokens = current_tokens()
        enter(win.listView.viewport())
        _pump()
        assert set(_handle_row(win)) == {tokens["scrollHandle"]}
        leave(win.listView.viewport())
        enter(_overlay(win))
        _pump()
        assert set(_handle_row(win)) == {tokens["scrollHandleHover"]}
