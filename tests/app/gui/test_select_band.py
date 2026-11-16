"""고른 행의 왼쪽 띠 — 굵기 4 · 진행 막대 위 · 연달아 고른 행 잇기 · 자라는 애니메이션 (#611, 오너 2026-11-15).

실제 배선을 탄다: 메인 창 · 카드 목록 · 선택 버튼 · 카드 누름(`QTest.mouseClick`) · Ctrl/⌘+A. 화소는 창을 그려서
읽는다(`grab`). 기대값은 손으로 정한 값이다(띠 4, 행 80 · 구분선 1, 막대 4).
"""

from collections.abc import Callable, Iterator

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from app.viewmodels import card_display
from app.viewmodels.data import ContentItem
from app.views.mainWindow import VodDownloader
from app.widgets import contentItemWidget
from app.widgets.widget import ContentItemWidget
from core.models.download_state import DownloadState
from tests.app.gui.test_card_c4 import (  # noqa: F401 — 픽스처는 이름으로 쓴다
    _add,
    _apply_production_qss,
    _card,
    _click,
    _colour_at,
    _drop_windows,
    _is,
    _item,
    _toggle_select_mode,
    no_network,
    window,
)

D = DownloadState
BAND = 4  # 띠 굵기(px) — 지시 값
BAR = 4  # 진행 막대 높이(px)
GROW_MS = 400  # 테스트의 자라는 시간(ms) — 화면 한 번 그리는 사이에 끝나지 않게 늘린다
MANY = 14  # 창 높이 700에서 아래 카드가 화면 밖에 있는 카드 수


@pytest.fixture
def animations() -> Iterator[Callable[[bool], None]]:
    """애니메이션 설정을 정한다 — 끝나면 설정에서 다시 읽게 되돌린다."""

    def use(on: bool) -> None:
        card_display.use(card_display.CardDisplay(animations=on))

    use(False)
    yield use
    card_display.use(None)


@pytest.fixture
def slow_band(monkeypatch: pytest.MonkeyPatch) -> None:
    """띠가 자라고 줄어드는 시간을 늘린다 — 도는 중의 띠 높이를 잴 수 있게."""
    monkeypatch.setattr(contentItemWidget, "BAND_GROW_MS", GROW_MS)
    monkeypatch.setattr(contentItemWidget, "BAND_SHRINK_MS", GROW_MS)


@pytest.fixture
def shown(request: pytest.FixtureRequest) -> VodDownloader:
    """c4의 `window` 창을 띄운 것 — 화면에 보여야 띠 애니메이션이 돈다(화면 밖 카드는 바로 바뀐다)."""
    win = request.getfixturevalue("window")
    win.show()
    QApplication.processEvents()
    return win


def _face_pixel(
    win: VodDownloader, image: QImage, card: ContentItemWidget, x: int, y: int
) -> QColor:
    """카드 면 좌표 (x, y)의 창 그림 화소."""
    spot = card.contentFrame.mapTo(win, QPoint(x, y))
    return _colour_at(image, spot.x(), spot.y())


def _select_all(win: VodDownloader) -> None:
    """목록에 Ctrl/⌘+A — 보이는 카드를 모두 고른다."""
    win.listView.setFocus()
    QTest.keyClick(win.listView, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QApplication.processEvents()


def _all_picked(win: VodDownloader, items: list[ContentItem]) -> None:
    assert win.listView.selectedItems() == items, "전제: 모두 골랐다"


class TestBandShape:
    def test_band_is_4_wide(self, shown: VodDownloader, animations: Callable[[bool], None]) -> None:
        """고른 행의 왼쪽 띠는 4px 폭이어야 한다.

        대기 카드 하나, 선택 모드에서 클릭(애니메이션 끔) -> 면 세로 가운데 화소 0 ~ 3 ≈ accent, 4는 accent 아님
        """
        win = shown
        items = _add(win, D.WAITING)
        _toggle_select_mode(win, True)
        _click(win, items[0])
        card = _card(win, items[0])
        image = win.grab().toImage()
        middle = card.contentFrame.height() // 2
        assert all(_is(_face_pixel(win, image, card, x, middle), "accent") for x in range(BAND))
        assert not _is(_face_pixel(win, image, card, BAND, middle), "accent")

    @pytest.mark.parametrize("state", [D.RUNNING, D.PAUSED])
    def test_band_lies_over_the_progress_bar(
        self, shown: VodDownloader, animations: Callable[[bool], None], state: DownloadState
    ) -> None:
        """막대가 보이는 카드를 고르면 띠가 진행 막대 위에 그려져 막대 자리 왼쪽 아래 화소가 띠 색이어야 한다.

        받는 중 · 일시정지 카드 하나(진행 50%), 고르기 전 막대 자리(아래에서 둘째 줄) 화소 0 ≈ accent 아님,
        선택 모드에서 클릭 -> 화소 0 ~ 3 ≈ accent, 화소 4 ≈ accent 아님
        """
        win = shown
        item = _item("카드", state)
        item.download_progress = 50  # 막대 채운 몫이 띠 자리를 덮는다
        win.contentManager.model.addItem(item)
        QApplication.processEvents()
        items = [item]
        card = _card(win, item)
        assert card.progressBar.isVisible(), "전제: 진행 막대가 보인다"
        assert card.progressBar.value() > 0, "전제: 채운 몫이 있다"
        low = card.contentFrame.height() - 2  # 막대 4줄 가운데 구분선 바로 위 줄
        before = win.grab().toImage()
        assert not _is(_face_pixel(win, before, card, 0, low), "accent"), (
            "전제: 막대 색은 강조색 아님"
        )
        _toggle_select_mode(win, True)
        _click(win, items[0])
        after = win.grab().toImage()
        assert all(_is(_face_pixel(win, after, card, x, low), "accent") for x in range(BAND))
        assert not _is(_face_pixel(win, after, card, BAND, low), "accent")


class TestBandJoin:
    def test_consecutive_picks_paint_the_separator_between_them(
        self, shown: VodDownloader, animations: Callable[[bool], None]
    ) -> None:
        """이어서 고른 두 행 사이의 구분선 자리 왼쪽 4px는 띠 색이고, 아래 행을 안 골랐으면 띠가 끊겨야 한다.

        대기 · 받는 중 · 대기 · 대기 카드, 선택 모드에서 앞의 셋 클릭 -> 첫째 · 둘째 행 아래끝 줄 화소 0 ~ 3 ≈ accent,
        화소 4 ≈ border, 셋째 행 아래끝 줄 화소 0 ≈ accent 아님
        """
        win = shown
        items = _add(win, D.WAITING, D.RUNNING, D.WAITING, D.WAITING)
        _toggle_select_mode(win, True)
        for item in items[:3]:
            _click(win, item)
        image = win.grab().toImage()
        cards = [_card(win, item) for item in items]
        for card in cards[:2]:
            line = card.contentFrame.height() - 1
            assert all(_is(_face_pixel(win, image, card, x, line), "accent") for x in range(BAND))
            assert not _is(_face_pixel(win, image, card, BAND, line), "accent")
        third = cards[2]
        line = third.contentFrame.height() - 1
        assert not _is(_face_pixel(win, image, third, 0, line), "accent")

    def test_unpicking_the_lower_row_cuts_the_join(
        self, shown: VodDownloader, animations: Callable[[bool], None]
    ) -> None:
        """이어서 고른 두 행 가운데 아래 행을 풀면 위 행의 구분선 자리가 다시 띠 색이 아니어야 한다.

        대기 카드 셋, 선택 모드에서 첫째 · 둘째 클릭 -> 첫째 아래끝 화소 0 ≈ accent, 둘째 다시 클릭 -> 첫째 아래끝
        화소 0 ≈ border
        """
        win = shown
        items = _add(win, D.WAITING, D.WAITING, D.WAITING)
        _toggle_select_mode(win, True)
        _click(win, items[0])
        _click(win, items[1])
        first = _card(win, items[0])
        line = first.contentFrame.height() - 1
        assert _is(_face_pixel(win, win.grab().toImage(), first, 0, line), "accent"), "전제: 이었다"
        _click(win, items[1])
        assert _is(_face_pixel(win, win.grab().toImage(), first, 0, line), "border")


class TestBandMotion:
    def test_one_pick_grows_the_band_from_the_middle(
        self,
        shown: VodDownloader,
        animations: Callable[[bool], None],
        slow_band: None,
        qtbot: QtBot,
    ) -> None:
        """카드 한 장을 고르면 띠가 행 세로 가운데에서 위아래로 자라야 한다(바탕은 바로 바뀐다).

        애니메이션 켬, 대기 카드 하나, 선택 모드에서 클릭 -> 띠 애니메이션 있음 · 띠 비율 < 1 · 고른 바탕(picked
        속성) 참, 도는 중의 띠 사각형 세로 가운데 == 구분선 위 행의 세로 가운데, 끝나면 비율 1 · 애니메이션 없음
        """
        win = shown
        items = _add(win, D.WAITING)
        _toggle_select_mode(win, True)
        animations(True)
        _click(win, items[0])
        face = _card(win, items[0]).contentFrame
        assert face.property("picked") is True
        assert face.bandMotion() is not None and face.band() < 1.0
        qtbot.waitUntil(lambda: 0.0 < face.band() < 1.0)
        rect = face.bandRect()
        assert rect.center().y() == pytest.approx((face.height() - 1) / 2)
        qtbot.waitUntil(lambda: face.band() == 1.0 and face.bandMotion() is None)

    def test_one_unpick_shrinks_the_band(
        self,
        shown: VodDownloader,
        animations: Callable[[bool], None],
        slow_band: None,
        qtbot: QtBot,
    ) -> None:
        """고른 카드 한 장을 풀면 띠가 줄어들어 없어져야 한다(바탕은 바로 돌아간다).

        대기 카드 하나, 고름(애니메이션 끔) 뒤 애니메이션 켬 · 다시 클릭 -> 애니메이션 있음 · picked 거짓,
        끝나면 띠 비율 0
        """
        win = shown
        items = _add(win, D.WAITING)
        _toggle_select_mode(win, True)
        _click(win, items[0])
        face = _card(win, items[0]).contentFrame
        assert face.band() == 1.0, "전제: 바로 다 자랐다"
        animations(True)
        _click(win, items[0])
        assert face.property("picked") is False
        assert face.bandMotion() is not None
        qtbot.waitUntil(lambda: face.band() == 0.0 and face.bandMotion() is None)

    def test_select_all_changes_every_band_at_once(
        self, shown: VodDownloader, animations: Callable[[bool], None], slow_band: None
    ) -> None:
        """모두 선택(Ctrl/⌘+A)은 띠 애니메이션 없이 모든 카드의 띠가 바로 다 자라야 한다.

        애니메이션 켬, 대기 카드 셋, 목록에 Ctrl+A -> 셋 모두 띠 비율 1 · 띠 애니메이션 없음
        """
        win = shown
        items = _add(win, D.WAITING, D.WAITING, D.WAITING)
        animations(True)
        _select_all(win)
        _all_picked(win, items)
        faces = [_card(win, item).contentFrame for item in items]
        assert [face.band() for face in faces] == [1.0, 1.0, 1.0]
        assert [face.bandMotion() for face in faces] == [None, None, None]

    def test_shift_range_changes_every_band_at_once(
        self, shown: VodDownloader, animations: Callable[[bool], None], slow_band: None
    ) -> None:
        """Shift 범위로 여러 장을 한꺼번에 고르면 띠가 애니메이션 없이 바로 다 자라야 한다.

        애니메이션 켬, 대기 카드 넷, 선택 모드에서 첫째 클릭 · 띠가 다 자람 -> 넷째 Shift+클릭 -> 둘째 ~ 넷째 띠
        비율 1 · 띠 애니메이션 없음
        """
        win = shown
        items = _add(win, D.WAITING, D.WAITING, D.WAITING, D.WAITING)
        _toggle_select_mode(win, True)
        _click(win, items[0])
        animations(True)
        _click(win, items[3], Qt.KeyboardModifier.ShiftModifier)
        faces = [_card(win, item).contentFrame for item in items[1:]]
        assert [face.band() for face in faces] == [1.0, 1.0, 1.0]
        assert [face.bandMotion() for face in faces] == [None, None, None]

    def test_card_out_of_view_changes_at_once_and_scrolls_in_drawn(
        self, shown: VodDownloader, animations: Callable[[bool], None], slow_band: None
    ) -> None:
        """화면 밖 카드의 고름이 바뀌면 띠가 바로 바뀌고, 스크롤로 들어올 때 다 자란 띠로 보여야 한다.

        애니메이션 켬, 카드 14장(마지막 카드는 화면 밖), 선택 모드에서 마지막 카드를 Space 없이 목록 함수
        `cardPicked`(Ctrl)로 고름 -> 띠 비율 1 · 애니메이션 없음, 끝까지 굴림 -> 띠 비율 1 · 애니메이션 없음
        """
        win = shown
        items = _add(win, *([D.WAITING] * MANY))
        _toggle_select_mode(win, True)
        last = _card(win, items[-1])
        assert last.visibleRegion().isEmpty(), "전제: 마지막 카드는 화면 밖이다"
        animations(True)
        win.listView.cardPicked(last, Qt.KeyboardModifier.ControlModifier)
        QApplication.processEvents()
        face = last.contentFrame
        assert face.property("picked") is True
        assert (face.band(), face.bandMotion()) == (1.0, None)
        bar = win.listView.verticalScrollBar()
        bar.setValue(bar.maximum())
        QApplication.processEvents()
        assert not last.visibleRegion().isEmpty(), "전제: 굴러 들어왔다"
        assert (face.band(), face.bandMotion()) == (1.0, None)

    def test_animations_off_changes_the_band_at_once(
        self, shown: VodDownloader, animations: Callable[[bool], None], slow_band: None
    ) -> None:
        """애니메이션을 끄면 한 장을 골라도 · 풀어도 띠가 바로 바뀌어야 한다.

        애니메이션 끔, 대기 카드 하나, 선택 모드에서 클릭 -> 띠 비율 1 · 애니메이션 없음, 다시 클릭 -> 0 · 없음
        """
        win = shown
        items = _add(win, D.WAITING)
        _toggle_select_mode(win, True)
        _click(win, items[0])
        face = _card(win, items[0]).contentFrame
        assert (face.band(), face.bandMotion()) == (1.0, None)
        _click(win, items[0])
        assert (face.band(), face.bandMotion()) == (0.0, None)

    def test_band_animation_object_is_gone_after_it_finishes(
        self,
        shown: VodDownloader,
        animations: Callable[[bool], None],
        qtbot: QtBot,
    ) -> None:
        """띠 애니메이션 객체는 끝나면 지워져야 한다(카드마다 객체가 남지 않는다).

        애니메이션 켬, 대기 카드 하나, 선택 모드에서 클릭 · 다 자람 -> 지연 삭제 처리 뒤 카드 면 아래 애니메이션 0개
        """
        from PySide6.QtCore import QVariantAnimation

        win = shown
        items = _add(win, D.WAITING)
        _toggle_select_mode(win, True)
        animations(True)
        _click(win, items[0])
        face = _card(win, items[0]).contentFrame
        qtbot.waitUntil(lambda: face.band() == 1.0 and face.bandMotion() is None)
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert face.findChildren(QVariantAnimation) == []
