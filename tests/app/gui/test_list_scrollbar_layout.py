"""카드 목록 스크롤바 재배치 + 카드 폭 고정 + 칩 숫자 자릿수 게이트 (v2.10.1 · #495).

오너가 정한 것:
  ① 세로 스크롤바 유무로 카드 행 안 내용(썸네일 왼쪽 끝 · 3줄 오른쪽 끝)이 움직이지 않는다. 스크롤바는 필요할
     때만 보이고(항상 켜 두는 안은 빈 목록에도 바가 보여 기각), 행 위에 겹쳐 그려져 자리를 차지하지 않는다 —
     띠 행은 늘 목록 끝까지 간다(설계 §13, #611 · 오너 실기 2026-11-15). **이 파일의 핵심 게이트.**
  ② 목록 · 카드 행은 창 좌우 끝까지 가서 스크롤바가 창 우측 끝에 붙고, 행 안 내용은 좌우 outerMargin 안쪽이다.
     바(메뉴바 · 헤더바 · 하단바)도 창 끝까지 가고, 바 안 내용 끝과 카드 행 안 내용 끝이 같은 두 선이다
     (#536 · #611).
  ③ 칩 숫자가 늘어도 콘텐츠 최소폭이 그대로다 — 칩마다 숫자 세 자리(`999`) 폭을 미리 잡는다.
     시작 때 고정된 창 최소폭을 숫자가 넘어 좌우 스크롤 안전망이 뜨던 결함(v2.10.1 하단 카운트)을
     #495에서 카운트 대신 칩이 이어 받는다. 창 최소폭을 내용 따라 다시 재지 않는다.

전부 폰트 무의존 기하 관계로 잰다. 결함 주입: 스크롤바가 자리를 차지하면 ①이, 행 안쪽 좌우 여백을
바꾸면 ②가, 칩의 세 자리 예약을 빼면 ③이 실패한다.
"""

from collections.abc import Callable, Iterator

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import QApplication, QLayout, QWidget

import CVDv2 as main_module
import app.theme as theme
from app.viewmodels.data import ContentItem
from app.views.mainWindow import VodDownloader
from app.widgets.widget import ContentItemWidget
from tests.app.gui.card_helpers import drop_new_top_levels, hold_style, snapshot_top_levels

#: 뷰포트 높이를 확실히 넘기는 카드 수(카드 하나가 100px 안팎, 창 높이 600)
MANY = 40


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp) -> None:
    """실제 전역 QSS·스타일을 태운다 — QSS 없이는 목록에 프레임(1~2px)과 Fusion 스크롤바(14px)가
    붙어 제품(프레임 0, 스크롤바 scrollBarWidth)과 다른 기하가 나온다. ⚠️ function scope 유지."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))  # 참조 보관 — 이중 해제 우회 (#243)
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))


@pytest.fixture(autouse=True)
def _destroy_windows() -> Iterator[None]:
    """테스트가 만든 최상위 창은 close()가 아니라 파괴한다(#248 CI)."""
    before = snapshot_top_levels()
    yield
    drop_new_top_levels(before)


def _item(i: int) -> ContentItem:
    return ContentItem(
        f"https://chzzk.naver.com/video/{i}",
        {
            "title": f"{i:02d} 제목",
            "category": "",
            "channelName": "채널",
            "createdDate": "",
            "duration": 3600,
        },
        [["1080", "u1"], ["720", "u2"]],
        None,
        "",
        "",
        "video",
        None,
    )


def _pump() -> None:
    for _ in range(4):
        QApplication.processEvents()


@pytest.fixture
def make_window(qapp) -> Iterator[Callable[..., VodDownloader]]:
    made = []

    def build(cards: int, width_extra: int = 200, height: int = 600) -> VodDownloader:
        win = VodDownloader()
        made.append(win)
        win.show()
        _pump()
        for i in range(cards):
            win.contentManager.model.addItem(_item(i))
        win.resize(win.minimumWidth() + width_extra, height)
        _pump()
        return win

    yield build
    for win in made:
        win.close()
        win.deleteLater()
    _pump()


def _in_window(win: VodDownloader, widget) -> QRect:
    return QRect(widget.mapTo(win, QPoint(0, 0)), widget.size())


def _first_card(win: VodDownloader) -> ContentItemWidget | None:
    return win.listView.widgetFor(win.contentManager.model.itemAt(0))


def _content_span(win: VodDownloader, card: ContentItemWidget) -> tuple[int, int]:
    """카드 행 안 내용의 (썸네일 왼쪽 끝 x, 3줄 오른쪽 끝 x + 1) — 창 좌표."""
    thumb = _in_window(win, card.thumbnailLabel)
    row = _in_window(win, card.statusRow)
    return thumb.left(), row.right() + 1


# ================================================================ ① 스크롤바 유무와 카드 폭


def test_vertical_scrollbar_appears_only_when_the_list_overflows(make_window) -> None:
    """겹친 스크롤바는 목록이 넘칠 때만 있어야 하고 원래 스크롤바는 늘 숨어야 한다(#611).

    카드 1장 · 40장 -> 겹친 막대 1장 숨음 · 40장 보임, 원래 스크롤바 정책 AlwaysOff · 둘 다 숨음
    """
    few, many = make_window(cards=1), make_window(cards=MANY)
    for win in (few, many):
        assert win.listView.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        assert not win.listView.verticalScrollBar().isVisible(), "원래 스크롤바가 보인다"
    assert not few.listView.overlayScrollBar().isVisible(), "카드 1장인데 스크롤바가 있다"
    assert many.listView.overlayScrollBar().isVisible(), "40장인데 스크롤바가 없다"


def test_card_width_is_the_same_whether_or_not_the_list_overflows(make_window) -> None:
    """카드가 뷰포트에 다 들어갈 때(1장)와 넘칠 때(40장) 카드 행 안 내용의 두 끝이 같다 — 이 작업의 목적.

    스크롤바는 행 위에 겹쳐 그려져 뷰포트 폭을 바꾸지 않는다(#611).
    """
    few, many = make_window(cards=1), make_window(cards=MANY)
    assert few.width() == many.width(), "전제 — 두 창의 폭이 같아야 비교가 된다"
    assert many.listView.verticalScrollBar().maximum() > 0, "전제 — 40장이 뷰포트를 넘쳐야 한다"
    span_few = _content_span(few, _first_card(few))
    span_many = _content_span(many, _first_card(many))
    assert span_few == span_many, (
        f"행 안 내용이 스크롤바 유무에 따라 다르다: 1장 {span_few} vs 40장 {span_many}"
    )


@pytest.mark.parametrize("width_extra", [0, 300])
@pytest.mark.parametrize("cards", [1, MANY])
def test_scrollbar_hugs_the_window_edge_and_cards_keep_the_outer_margin(
    make_window, width_extra, cards
) -> None:
    """목록 · 카드 행은 창 좌우 끝까지, 행 안 내용은 좌우 outerMargin 안쪽에, 스크롤바(있을 때)는 창 우측 끝에.

    카드 1장 · 40장, 창 폭 최소폭 + 0 · 300 -> 목록 (0, 창 폭), 행 왼쪽 0 · 오른쪽 끝 = 창 폭, 썸네일 왼쪽 =
    outerMargin, 3줄 오른쪽 끝 = 창 폭 − outerMargin, 겹친 막대(40장) 오른쪽 끝 = 창 폭 · 왼쪽 끝 ≥ 3줄 오른쪽 끝,
    세 바 (0, 창 폭)
    """
    win = make_window(cards=cards, width_extra=width_extra)
    outer = theme.METRICS["outerMargin"]
    lst = _in_window(win, win.listView)
    assert (lst.left(), lst.width()) == (0, win.width()), f"목록이 창 끝까지 안 간다: {lst}"
    card = _in_window(win, _first_card(win))
    assert card.left() == 0, f"카드 행이 창 왼쪽 끝에서 시작하지 않는다: {card}"
    assert card.right() + 1 == win.width(), f"카드 행 오른쪽 끝이 창 폭이 아니다: {card}"
    assert _content_span(win, _first_card(win)) == (outer, win.width() - outer), (
        f"행 안 내용이 좌우 outerMargin({outer}) 안쪽이 아니다"
    )
    bar_widget = win.listView.overlayScrollBar()
    assert bar_widget.isVisible() is (cards == MANY), "전제: 넘칠 때만 스크롤바가 있다"
    if bar_widget.isVisible():
        bar = _in_window(win, bar_widget)
        assert bar.right() + 1 == win.width(), f"스크롤바가 창 우측 끝에 안 붙는다: {bar}"
        assert bar.left() >= _content_span(win, _first_card(win))[1], (
            f"스크롤바 자리가 행 안 내용을 덮는다: 스크롤바 {bar}"
        )
    # 바는 창 끝까지 간다 — 바깥 여백 0(#536 — 설계 §11.1)
    for frame in (win.appMenuBar, win.headerFrame, win.infoFrame):
        rect = _in_window(win, frame)
        assert (rect.left(), rect.width()) == (0, win.width()), f"{frame.objectName()} {rect}"


def _content_edges(win: VodDownloader, frame: QWidget, inner: QLayout) -> tuple[int, int]:
    """그 바 안 내용의 (왼쪽 x, 오른쪽 끝 x + 1) — 창 좌표. 바 레이아웃의 내용 사각형으로 잰다."""
    rect = inner.contentsRect()
    left = frame.mapTo(win, rect.topLeft()).x()
    return left, left + rect.width()


@pytest.mark.parametrize("width_extra", [0, 300, 900])
def test_bar_contents_and_card_edges_share_two_lines(
    make_window: Callable[..., VodDownloader], width_extra: int
) -> None:
    """메뉴바 · 헤더바 · 하단바 안 내용의 왼쪽 · 오른쪽 끝이 카드 행 안 내용의 두 끝과 같아야 한다.

    창 폭 = 최소폭 + 0 · 300 · 900, 카드 2장
    -> 세 바 내용 (왼쪽, 오른쪽 끝) == 카드 (썸네일 왼쪽, 3줄 오른쪽 끝) (#536 · #611 — 정렬선 둘)
    """
    win = make_window(cards=2, width_extra=width_extra)
    edges = _content_span(win, _first_card(win))
    for frame, inner in (
        (win.appMenuBar, win.appMenuLayout),
        (win.headerFrame, win.headerFrameLayout),
        (win.infoFrame, win.infoLayout),
    ):
        assert _content_edges(win, frame, inner) == edges, (
            f"{frame.objectName()} 내용 {_content_edges(win, frame, inner)} ≠ 카드 {edges} — 창 {win.width()}"
        )


# ================================================================ ③ 상태 칩 숫자 자릿수


def test_content_minimum_width_ignores_chip_count_digits(make_window) -> None:
    """카드가 9 → 10 → 99 → 100장으로 늘어 칩 숫자가 한 자리 → 세 자리가 돼도 콘텐츠 열 최소폭이 그대로이고 좌우 스크롤이 안 떠야 한다."""
    win = make_window(cards=0, width_extra=0)
    base = win.contentColumn.minimumSizeHint().width()
    added = 0
    for total in (9, 10, 99, 100):
        while added < total:
            win.contentManager.model.addItem(_item(added))
            added += 1
        _pump()
        assert win.filterChips["all"].text().endswith(str(total)), (
            "전제: 전체 칩 숫자가 카드 수를 따라야 한다"
        )
        assert win.contentColumn.minimumSizeHint().width() == base, (
            f"카드 {total}장에서 콘텐츠 열 최소폭이 {base} → {win.contentColumn.minimumSizeHint().width()}"
        )
        assert win.windowScrollArea.horizontalScrollBar().maximum() == 0, (
            f"카드 {total}장에서 좌우 스크롤이 생겼다"
        )


def test_chips_fit_their_shown_text_from_their_own_font(make_window) -> None:
    """칩마다 폭은 그 칩의 글꼴로 잰 지금 글 「이름 N」 폭이어야 한다 — 세 자리를 비워 두지 않는다(#651)."""
    win = make_window(cards=0)
    for key, chip in win.filterChips.items():
        assert chip.text().endswith(" 0"), f"전제: {key} 칩 숫자 0 — {chip.text()!r}"
        needed = chip.sizeHint().width()
        assert chip.width() == needed, f"{key} 칩 폭 {chip.width()} ≠ 지금 글 폭 {needed}"
