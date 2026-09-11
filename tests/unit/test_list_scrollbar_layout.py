"""카드 목록 스크롤바 재배치 + 카드 폭 고정 + 하단 카운트 라벨 자릿수 게이트 (v2.10.1).

오너가 정한 것:
  ① 세로 스크롤바 유무로 카드 폭이 변하지 않는다. 정책은 AsNeeded(필요할 때만 보인다 —
     항상 켜 두는 안은 빈 목록에도 바가 보여 기각)이고, 스크롤바가 보이는 동안은 카드
     컨테이너 오른쪽 패딩에서 그 폭을 빼 스크롤바가 여백 안에 들어간다. **이 파일의 핵심 게이트.**
  ② 목록은 창 좌우 끝까지 가서 스크롤바가 창 우측 끝에 붙고, 창 쪽에 있던 좌우 여백
     (outerMargin)은 목록 안 카드 컨테이너의 패딩으로 옮긴다 — 카드의 보이는 왼쪽 위치는
     그대로다.
  ③ 하단 바가 요구하는 폭을 카운트 라벨 자릿수 최대치(`999/999`) 기준으로 예약한다 —
     `0/0` 기준으로 시작 때 고정된 창 최소폭을 `0/10`·`0/100`이 넘어서 좌우 스크롤 안전망이
     뜨던 결함. 라벨은 자연 폭, 모자라는 몫은 스페이서 최소폭. 창 최소폭을 내용 따라 다시
     재지 않는다.

전부 폰트 무의존 기하 관계로 잰다. 고장 주입: 스크롤바 폭 보정을 빼면 ①이, 컨테이너
패딩을 0으로 되돌리면 ②가, 예약 균형(스페이서 최소폭)을 빼면 ③이 실패한다.
"""

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import QApplication

import main as main_module
import app.theme as theme
from app.viewmodels.data import ContentItem
from app.views.mainWindow import VodDownloader
from tests.unit.card_helpers import drop_new_top_levels, hold_style, snapshot_top_levels

#: 뷰포트 높이를 확실히 넘기는 카드 수(카드 하나가 100px 안팎, 창 높이 600)
MANY = 40


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp):
    """실제 전역 QSS·스타일을 태운다 — QSS 없이는 목록에 프레임(1~2px)과 Fusion 스크롤바(14px)가
    붙어 제품(프레임 0, 스크롤바 10px)과 다른 기하가 나온다. ⚠️ function scope 유지."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))  # 참조 보관 — 이중 해제 우회 (#243)
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))


@pytest.fixture(autouse=True)
def _destroy_windows():
    """테스트가 만든 최상위 창은 close()가 아니라 파괴한다(#248 CI)."""
    before = snapshot_top_levels()
    yield
    drop_new_top_levels(before)


def _item(i: int) -> ContentItem:
    return ContentItem(
        f"https://chzzk.naver.com/video/{i}",
        {"title": f"{i:02d} 제목", "category": "", "channelName": "채널", "createdDate": "", "duration": 3600},
        [["1080", "u1"], ["720", "u2"]], None, "", "", "video", None,
    )


def _pump() -> None:
    for _ in range(4):
        QApplication.processEvents()


@pytest.fixture
def make_window(qapp):
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


def _first_card(win: VodDownloader):
    return win.listView.widgetFor(win.contentManager.model.itemAt(0))


# ================================================================ ① 스크롤바 유무와 카드 폭


def test_vertical_scrollbar_appears_only_when_the_list_overflows(make_window):
    """스크롤바는 필요할 때만 보인다(AsNeeded) — 항상 켜 두는 안은 빈 목록에도 바가 보여 기각."""
    few, many = make_window(cards=1), make_window(cards=MANY)
    assert few.listView.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded
    assert not few.listView.verticalScrollBar().isVisible(), "카드 1장인데 스크롤바가 보인다"
    assert many.listView.verticalScrollBar().isVisible(), "40장인데 스크롤바가 없다"


def test_card_width_is_the_same_whether_or_not_the_list_overflows(make_window):
    """카드가 뷰포트에 다 들어갈 때(1장)와 넘칠 때(40장) 카드 폭·위치가 같다 — 이 작업의 목적.

    AsNeeded는 넘치는 순간 스크롤바 폭만큼 뷰포트가 좁아진다. 그만큼 컨테이너 오른쪽 패딩을
    줄여 스크롤바가 여백 안에 들어가므로 카드는 움직이지 않는다.
    """
    few, many = make_window(cards=1), make_window(cards=MANY)
    assert few.width() == many.width(), "전제 — 두 창의 폭이 같아야 비교가 된다"
    assert many.listView.verticalScrollBar().maximum() > 0, "전제 — 40장이 뷰포트를 넘쳐야 한다"
    card_few, card_many = _in_window(few, _first_card(few)), _in_window(many, _first_card(many))
    assert (card_few.left(), card_few.width()) == (card_many.left(), card_many.width()), (
        f"카드 x·폭이 스크롤바 유무에 따라 다르다: 1장 {card_few} vs 40장 {card_many}"
    )


# ================================================================ ② 목록은 창 끝까지, 카드는 outerMargin


@pytest.mark.parametrize("width_extra", [0, 300])
@pytest.mark.parametrize("cards", [1, MANY])
def test_scrollbar_hugs_the_window_edge_and_cards_keep_the_outer_margin(make_window, width_extra, cards):
    """목록은 창 좌우 끝까지, 카드는 좌우 outerMargin 안쪽에, 스크롤바(보일 때)는 창 우측 끝 그 여백 안에."""
    win = make_window(cards=cards, width_extra=width_extra)
    outer = theme.METRICS["outerMargin"]
    lst = _in_window(win, win.listView)
    assert (lst.left(), lst.width()) == (0, win.width()), f"목록이 창 끝까지 안 간다: {lst}"
    card = _in_window(win, _first_card(win))
    assert card.left() == outer, f"카드 왼쪽이 outerMargin({outer})이 아니다: {card}"
    assert card.right() + 1 == win.width() - outer, f"카드 오른쪽 여백이 outerMargin({outer})이 아니다: {card}"
    bar_widget = win.listView.verticalScrollBar()
    if bar_widget.isVisible():
        bar = _in_window(win, bar_widget)
        assert bar.right() + 1 == win.width(), f"스크롤바가 창 우측 끝에 안 붙는다: {bar}"
        assert bar.left() >= card.right() + 1, f"스크롤바가 카드를 덮는다: 카드 {card}, 스크롤바 {bar}"
    for frame in (win.headerFrame, win.infoFrame):
        rect = _in_window(win, frame)
        assert (rect.left(), rect.width()) == (outer, win.width() - 2 * outer), f"{frame.objectName()} {rect}"


def test_card_left_edge_lines_up_with_the_bars(make_window):
    """카드 프레임 왼쪽이 상단바·하단바 왼쪽과 한 선이다 — 여백이 자리만 옮겼지 값은 그대로다(#244 정렬선)."""
    win = make_window(cards=2)
    card_left = _in_window(win, _first_card(win)).left()
    assert card_left == _in_window(win, win.headerFrame).left() == _in_window(win, win.infoFrame).left()


# ================================================================ ③ 하단 카운트 라벨 자릿수


@pytest.mark.parametrize("total", [9, 10, 99, 100])
def test_content_minimum_width_ignores_count_label_digits(make_window, total):
    """총계가 한 자리→두 자리→세 자리가 돼도 콘텐츠 열 최소폭이 그대로고 좌우 스크롤이 안 뜬다."""
    win = make_window(cards=0, width_extra=0)
    base = win.contentColumn.minimumSizeHint().width()
    win.completed_downloads = 0
    win.total_downloads = total
    win.updateDownloadCountLabel()
    _pump()
    assert win.contentColumn.minimumSizeHint().width() == base, (
        f"총계 {total}에서 콘텐츠 열 최소폭이 {base} → {win.contentColumn.minimumSizeHint().width()}"
    )
    assert win.windowScrollArea.horizontalScrollBar().maximum() == 0, f"총계 {total}에서 좌우 스크롤이 생겼다"


def test_footer_reserves_three_digits_from_the_label_font(make_window):
    """예약은 상수가 아니라 이 라벨의 글꼴로 잰 `999/999` 기준이다 — 글꼴·DPI·번역을 따라간다.

    라벨은 자연 폭 그대로(`0/0`일 때 뒤 버튼이 안 밀린다)이고, 모자라는 몫은 하단 바 스페이서의
    최소폭이 진다 — 라벨 + 스페이서 최소 = `999/999` 폭. 라벨 최대폭이 그 폭이라 1000+는 라벨
    안에서 잘린다. 하단 바에 명시 최소폭을 박지 않는다(버튼이 넓어지면 바닥도 따라가야 한다).
    """
    win = make_window(cards=0)
    label, spacer, frame = win.downloadCountLabel, win.horizontalSpacer, win.infoFrame
    shown = label.text()
    label.setText(win.tr("Downloads: {}/{}").format(999, 999))
    needed = label.sizeHint().width()
    label.setText(shown)
    assert label.maximumWidth() == needed, f"라벨 최대폭 {label.maximumWidth()} ≠ 999/999 폭 {needed}"
    assert label.width() == label.sizeHint().width() < needed, "0/0일 때 라벨은 자연 폭이어야 한다(뒤 버튼이 안 밀림)"
    assert label.sizeHint().width() + spacer.minimumSize().width() == needed, (
        f"라벨 {label.sizeHint().width()} + 스페이서 최소 {spacer.minimumSize().width()} ≠ 예약 {needed}"
    )
    assert frame.minimumWidth() == 0, "하단 바에 명시 최소폭을 박으면 안 된다 — 내용 최소를 가린다"
