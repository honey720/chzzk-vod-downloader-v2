"""창 셸(콘텐츠 열·창 최소폭 바닥·좌우 스크롤 안전망·오버레이 좌표) 게이트 (#244 목록/헤더).

구조(ui/mainWindow.py):

    VodDownloader
    └ windowScrollArea   창 폭이 콘텐츠 최소폭보다 좁을 때만 좌우 스크롤 — 평소엔 안 보인다
      └ contentColumn    상단바·카드 목록·하단바를 담는 열, 최대폭 = contentMaxWidth + 좌우 outerMargin
        ├ headerFrame
        ├ listView       (_Overlay는 이 안에 있다 — 드래그 반투명·빈 목록 안내)
        └ infoFrame

세 가지를 잰다.
① 콘텐츠 열 최대폭 — 넓은 창에서 상단바·카드·하단바가 같은 폭에서 함께 멈추고 중앙에 놓인다.
② 창 최소폭 바닥 — `max(콘텐츠 최소폭, 화면 논리폭 × 0.25)`. 화면을 흉내 내서 두 가지를
   따로 본다(작은 화면이면 콘텐츠 최소가, 큰 화면이면 비율이 이긴다).
③ 좌우 스크롤 — 평소 폭 어디서도 안 보이고, 창이 콘텐츠보다 좁게 강제됐을 때만 뜬다.
④ 오버레이 — 콘텐츠 영역(listView) 좌표계에 있다. 창 기준이 아니다.

폭은 절대 px가 아니라 유도한다: 창 최소폭은 창에서 읽고(콘텐츠 최소는 폰트·OS마다
다르다 — offscreen 674 / Windows 실기 406), 열 최대폭은 theme 토큰을 테스트가 직접
더한다. 콘텐츠 최소폭도 제품의 계산(`_contentMinimumWidth`)을 부르지 않고 leaf
위젯의 힌트를 테스트가 독립 합산한다 — 제품이 틀리면 테스트도 같이 틀리기 때문이다.
"""

import pytest
from PySide6.QtCore import QMimeData, QPoint, QRect, QSize, Qt
from PySide6.QtGui import QDragEnterEvent
from PySide6.QtWidgets import QApplication

import main as main_module
import theme
from application.mainWindow import VodDownloader
from content.data import ContentItem
from core.models.download_state import DownloadState
from tests.unit.card_helpers import drop_new_top_levels, hold_style, snapshot_top_levels

#: 열 최대폭(창 기준) = 콘텐츠 최대폭 + 좌우 outerMargin — 테스트가 토큰을 직접 더한다.
COLUMN_CAP = theme.METRICS["contentMaxWidth"] + 2 * theme.METRICS["outerMargin"]


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp):
    """실제 전역 QSS·스타일을 태운다(폭은 QSS padding·폰트에 좌우된다). ⚠️ function scope 유지."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))  # 참조 보관 — 이중 해제 우회 (#243)
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))


@pytest.fixture(autouse=True)
def _destroy_windows():
    """테스트가 만든 최상위 창은 close()가 아니라 파괴한다 — 숨은 채 남으면 다음 setStyle이 죽는다(#248 CI)."""
    before = snapshot_top_levels()
    yield
    drop_new_top_levels(before)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    class _FailingSession:
        def head(self, *a, **k):
            raise RuntimeError("network disabled in tests")

        def get(self, *a, **k):
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("content.widget.get_thread_session", lambda: _FailingSession())


def _fake_screen(monkeypatch, width: int, height: int = 600) -> None:
    """창이 보는 주 화면 크기를 흉내 낸다 — 작은 화면(비율 < 콘텐츠)과 큰 화면(비율 > 콘텐츠)을 따로 본다."""
    monkeypatch.setattr(VodDownloader, "_screenLogicalSize", lambda self: QSize(width, height))


def _make_item(i: int) -> ContentItem:
    return ContentItem(
        f"https://chzzk.naver.com/video/{i}",
        {
            "title": f"제목 {i}",
            "category": "",
            "channelName": f"채널{i}",
            "createdDate": "",
            "duration": 3600,
        },
        [],
        None,
        "",
        "C:/dl",
        "video",
        None,
    )


def _window(cards: int = 0) -> VodDownloader:
    win = VodDownloader()
    for i in range(cards):
        item = _make_item(i)
        item.downloadState = DownloadState.WAITING
        win.contentManager.model.addItem(item)
    QApplication.processEvents()
    return win


def _at_width(win: VodDownloader, width: int) -> None:
    """창을 `width`로 놓고 실제 폭이 요청과 같은지 단언한다 — resize()는 요청이지 결과가 아니다."""
    win.resize(width, 700)
    win.show()
    QApplication.processEvents()
    QApplication.processEvents()
    assert win.width() == width, (
        f"요청 폭 {width}px인데 실제 {win.width()}px — 최소폭({win.minimumWidth()})에 클램프됐다"
    )


def _in_window(win, w) -> QRect:
    """위젯의 창 좌표계 사각형."""
    return QRect(w.mapTo(win, QPoint(0, 0)), w.size())


def _leaf_min(w) -> int:
    """레이아웃이 없는 leaf 위젯의 가로 최소 — 명시 minimumWidth가 있으면 그것, 없으면 힌트."""
    return w.minimumWidth() if w.minimumWidth() > 0 else w.minimumSizeHint().width()


def _independent_content_minimum(win: VodDownloader) -> int:
    """콘텐츠 열 최소폭을 leaf 위젯 힌트로 **독립 합산**한다(제품의 `_contentMinimumWidth` 미사용).

    상단바: 두 행 중 넓은 쪽(입력창 최소 + 간격 + 텍스트 버튼) + 간격 + ⚙ + 프레임 안쪽 여백·테두리.
    하단바: 라벨 + 완료 삭제 + 다운로드 + 중지 + 간격 3개 + 프레임 안쪽 여백·테두리.
    열 = max(상단바, 하단바, 목록 최소) + 좌우 outerMargin.
    """
    pad, outer = theme.METRICS["framePadding"], theme.METRICS["outerMargin"]
    border = win.headerFrame.frameWidth()
    row_gap = win.urlRowLayout.spacing()
    url_row = _leaf_min(win.urlInput) + row_gap + _leaf_min(win.fetchButton)
    path_row = _leaf_min(win.downloadPathInput) + row_gap + _leaf_min(win.downloadPathButton)
    header = max(url_row, path_row) + win.headerRowsLayout.spacing() + _leaf_min(win.settingButton)
    header += 2 * (pad + border)
    info_gap = win.infoLayout.spacing()
    info = sum(
        _leaf_min(w)
        for w in (
            win.downloadCountLabel,
            win.clearFinishedButton,
            win.downloadButton,
            win.stopButton,
        )
    )
    info += 3 * info_gap + 2 * (pad + border)
    return max(header, info, win.listView.minimumSizeHint().width()) + 2 * outer


# ---------------------------------------------------------------------------
# ① 콘텐츠 열 최대폭 + 중앙 정렬
# ---------------------------------------------------------------------------


class TestContentColumnStopsAtTheCapAndSitsInTheMiddle:
    """넓은 창에서 상단바·카드·하단바는 콘텐츠 최대폭에서 함께 멈추고, 남는 폭은 좌우로 반씩."""

    @pytest.mark.parametrize("extra", (1, 200, 800))
    def test_header_cards_and_footer_share_the_content_width(self, extra):
        win = _window(cards=3)
        _at_width(win, COLUMN_CAP + extra)
        cap = theme.METRICS["contentMaxWidth"]
        widths = {
            "header": win.headerFrame.width(),
            "list": win.listView.width(),
            "info": win.infoFrame.width(),
        }
        assert set(widths.values()) == {cap}, (
            f"창 {win.width()}px에서 콘텐츠 폭이 최대폭 {cap}에서 멈추지 않는다: {widths}"
        )
        card = next(iter(win.listView._widgets.values()))
        assert card.width() <= cap, f"카드({card.width()})가 콘텐츠 최대폭({cap})을 넘는다"
        assert card.width() < win.width() - 2 * theme.METRICS["outerMargin"], (
            "카드가 창 폭 전체로 늘어났다"
        )

    @pytest.mark.parametrize("extra", (1, 200, 800))
    def test_the_column_is_centred_and_the_three_share_one_left_edge(self, extra):
        win = _window(cards=3)
        _at_width(win, COLUMN_CAP + extra)
        header, lst, info = (
            _in_window(win, w) for w in (win.headerFrame, win.listView, win.infoFrame)
        )
        assert header.left() == lst.left() == info.left(), (
            "상단바·목록·하단바의 왼쪽 정렬선이 어긋난다"
        )
        assert header.right() == lst.right() == info.right(), (
            "상단바·목록·하단바의 오른쪽 끝이 어긋난다"
        )
        left_margin = header.left()
        right_margin = win.width() - 1 - header.right()
        assert abs(left_margin - right_margin) <= 1, (
            f"열이 중앙에 있지 않다: 왼쪽 여백 {left_margin} / 오른쪽 여백 {right_margin}"
        )
        assert left_margin >= theme.METRICS["outerMargin"] + extra // 2, (
            "넘는 폭이 여백이 되지 않았다"
        )

    def test_below_the_cap_the_column_fills_the_window(self):
        """캡 아래에서는 예전과 같다 — 열이 창을 가득 채우고 여백은 outerMargin뿐."""
        win = _window(cards=3)
        low = max(win.minimumWidth(), COLUMN_CAP - 100)
        _at_width(win, low)
        outer = theme.METRICS["outerMargin"]
        header = _in_window(win, win.headerFrame)
        assert header.left() == outer and header.width() == low - 2 * outer

    def test_inputs_stop_growing_past_the_cap(self):
        """헤더 입력창은 캡까지만 는다 — test_header_layout의 '창을 따라 는다'의 상한."""
        win = _window()
        _at_width(win, COLUMN_CAP)
        at_cap = win.urlInput.width()
        _at_width(win, COLUMN_CAP + 600)
        assert win.urlInput.width() == at_cap, "열 최대폭 너머에서도 입력창이 계속 늘어난다"


# ---------------------------------------------------------------------------
# ② 창 최소폭 바닥
# ---------------------------------------------------------------------------


class TestWindowMinimumWidthHasAContentFloor:
    """창 최소폭 = max(콘텐츠 최소폭, 화면 논리폭 × 0.25). 비율은 유지하고 바닥만 깐다."""

    def test_small_screen_floors_at_the_content_minimum(self, monkeypatch):
        """800px 화면: 비율(200)이 콘텐츠 최소보다 작다 → 콘텐츠 최소가 바닥이다."""
        _fake_screen(monkeypatch, 800)
        win = _window()
        win.show()
        QApplication.processEvents()
        content_min = _independent_content_minimum(win)
        assert content_min > 200, "전제: 콘텐츠 최소폭이 비율값(200)보다 커야 바닥이 의미 있다"
        assert win.minimumWidth() == content_min, (
            f"창 최소폭 {win.minimumWidth()} ≠ 콘텐츠 최소폭 {content_min} — 작은 화면에서 바닥이 안 깔렸다"
        )

    def test_large_screen_keeps_the_owner_ratio(self, monkeypatch):
        """4000px 화면: 비율(1000)이 콘텐츠 최소보다 크다 → 비율이 그대로 최소폭이다(바닥은 max일 뿐)."""
        _fake_screen(monkeypatch, 4000)
        win = _window()
        win.show()
        QApplication.processEvents()
        assert win.minimumWidth() == 1000, (
            f"큰 화면에서 비율 고정(4000×0.25)이 깨졌다: {win.minimumWidth()}"
        )

    def test_at_the_floor_nothing_overflows_its_frame(self, monkeypatch):
        """바닥 폭에서 상단 버튼이 프레임 밖으로 나가지 않는다 — 800×600 실기에서 무너지던 그 증상."""
        _fake_screen(monkeypatch, 800)
        win = _window(cards=2)
        _at_width(win, win.minimumWidth())
        for frame, widgets in (
            (
                win.headerFrame,
                (
                    win.urlInput,
                    win.fetchButton,
                    win.downloadPathInput,
                    win.downloadPathButton,
                    win.settingButton,
                ),
            ),
            (
                win.infoFrame,
                (
                    win.downloadCountLabel,
                    win.clearFinishedButton,
                    win.downloadButton,
                    win.stopButton,
                ),
            ),
        ):
            frame_rect = _in_window(win, frame)
            for w in widgets:
                assert frame_rect.contains(_in_window(win, w)), (
                    f"{w.objectName()} {_in_window(win, w)}이(가) {frame.objectName()} {frame_rect} 밖으로 넘친다"
                )
        column = _in_window(win, win.contentColumn)
        assert column.width() == win.width(), (
            "바닥 폭에서 열이 창보다 넓다(가로 스크롤이 필요한 상태)"
        )

    def test_the_floor_is_asked_from_the_layout_not_a_constant(self, monkeypatch):
        """콘텐츠가 넓어지면 바닥도 따라간다 — 하단 버튼을 300px 넓히면 콘텐츠 최소 계산이 그만큼 커져야 한다.

        창 최소 크기는 생성 시점에 한 번 정해지므로, 계산 자체(`_contentMinimumWidth`)가
        레이아웃을 읽는지를 본다. 상수를 박았다면 버튼을 넓혀도 움직이지 않는다.
        """
        _fake_screen(monkeypatch, 800)
        win = _window()
        win.show()
        QApplication.processEvents()
        base = win.minimumWidth()
        win.stopButton.setMinimumWidth(win.stopButton.width() + 300)
        QApplication.processEvents()
        recomputed = win._contentMinimumWidth()
        assert recomputed >= base + 300, (
            f"콘텐츠를 300px 넓혔는데 최소폭 계산이 {base} → {recomputed}로만 움직였다(상수 냄새)"
        )


# ---------------------------------------------------------------------------
# ③ 창 전체 좌우 스크롤 — 안전망
# ---------------------------------------------------------------------------


class TestHorizontalScrollIsOnlyASafetyNet:
    @pytest.mark.parametrize("which", ("min", "cap", "wide"))
    def test_no_scrollbar_at_any_ordinary_width(self, which):
        win = _window(cards=30)  # 세로 스크롤바가 떠 있는 상태여도 가로는 없어야 한다
        width = {"min": win.minimumWidth(), "cap": COLUMN_CAP, "wide": COLUMN_CAP + 800}[which]
        width = max(width, win.minimumWidth())
        _at_width(win, width)
        hbar = win.windowScrollArea.horizontalScrollBar()
        assert not hbar.isVisible(), (
            f"창 {width}px에서 좌우 스크롤바가 보인다(range {hbar.maximum()})"
        )
        assert not win.windowScrollArea.verticalScrollBar().isVisible(), (
            "창 전체 세로 스크롤은 없어야 한다(목록 것 하나뿐)"
        )

    def test_scrollbar_appears_only_when_the_window_is_forced_narrower_than_the_content(self):
        """OS가 창을 콘텐츠보다 좁게 만드는 상황(접근성 배율로 콘텐츠 최소가 화면보다 큰 경우)."""
        win = _window(cards=3)
        win.show()
        QApplication.processEvents()
        content_min = win.contentColumn.minimumSizeHint().width()
        win.setMinimumWidth(
            0
        )  # 창 최소폭 바닥을 걷어내야 그 아래로 내려갈 수 있다 — 화면이 작은 상황의 대역
        _at_width(win, content_min - 120)
        hbar = win.windowScrollArea.horizontalScrollBar()
        assert hbar.isVisible(), (
            "콘텐츠보다 좁은데 좌우 스크롤바가 없다 — 레이아웃이 무너지는 대신 받아내야 한다"
        )
        assert win.contentColumn.width() == content_min, (
            "열이 콘텐츠 최소폭 아래로 눌렸다(스크롤 대신 찌그러짐)"
        )
        assert hbar.maximum() == content_min - win.windowScrollArea.viewport().width()

    def test_the_list_scrollbar_stays_inside_the_column(self):
        """목록 세로 스크롤바는 열 안에 있다 — 창 오른쪽 끝이 아니라 카드 옆."""
        win = _window(cards=30)
        _at_width(win, COLUMN_CAP + 800)
        vbar = win.listView.verticalScrollBar()
        assert vbar.isVisible()
        assert _in_window(win, win.contentColumn).contains(_in_window(win, vbar))


# ---------------------------------------------------------------------------
# ④ 오버레이 좌표계 — 콘텐츠 영역 기준
# ---------------------------------------------------------------------------


def _drag_enter(view) -> None:
    """실제 이벤트 배선으로 드래그 진입 — 텍스트 mime을 든 QDragEnterEvent를 뷰포트에 보낸다.

    실제 드래그도 뷰포트가 받아 `QAbstractScrollArea.viewportEvent` → `dragEnterEvent`로 간다.
    """
    mime = QMimeData()
    mime.setText("https://chzzk.naver.com/video/1")
    viewport = view.viewport()
    event = QDragEnterEvent(
        QPoint(viewport.width() // 2, viewport.height() // 2),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(viewport, event)
    QApplication.processEvents()


class TestOverlaysLiveInTheContentArea:
    """드래그 반투명·빈 목록 안내는 카드 목록(콘텐츠 영역) 위에 그려진다 — 창 기준이 아니다."""

    @pytest.mark.parametrize("cards", (0, 3))
    def test_overlay_rect_equals_the_list_rect_not_the_window(self, cards):
        win = _window(cards=cards)
        _at_width(win, COLUMN_CAP + 800)
        overlay = _in_window(win, win.listView._overlay)
        lst = _in_window(win, win.listView)
        assert overlay == lst, f"오버레이 {overlay} ≠ 목록 {lst}"
        assert lst.width() < win.width() and lst.left() > theme.METRICS["outerMargin"], (
            "전제: 넓은 창이라 목록이 창보다 좁고 중앙에 있어야 한다"
        )

    def test_drag_overlay_dims_the_list_but_not_the_margins(self):
        """실제 렌더로 확인 — 드래그 중 어두워지는 픽셀은 목록 안에만 있고 열 바깥 여백은 그대로다."""
        win = _window(cards=3)
        _at_width(win, COLUMN_CAP + 800)
        before = win.grab().toImage()
        _drag_enter(win.listView)
        assert win.listView._dragActive
        after = win.grab().toImage()
        lst = _in_window(win, win.listView)
        inside = QPoint(lst.center().x(), lst.top() + 3)  # 카드 사이 여백이 아닌 목록 위쪽 배경
        margin = QPoint(lst.left() // 2, lst.center().y())  # 열 바깥 여백
        assert before.pixel(inside) != after.pixel(inside), (
            "드래그 중인데 목록 안 픽셀이 어두워지지 않았다"
        )
        assert before.pixel(margin) == after.pixel(margin), (
            "열 바깥 여백까지 어두워졌다 — 오버레이가 창 기준으로 그려졌다"
        )

    def test_empty_hint_pixels_stay_within_the_list_rect(self):
        """빈 목록 안내 글자는 목록 사각형 안에만 찍힌다(글꼴 무관 — 위치만 본다)."""
        win = _window(cards=0)
        _at_width(win, COLUMN_CAP + 800)
        image = win.grab().toImage()
        lst = _in_window(win, win.listView)
        background = image.pixel(QPoint(lst.left() // 2, lst.center().y()))
        ink = [
            (x, y)
            for y in range(0, win.height())
            for x in range(0, win.width(), 2)
            if image.pixel(QPoint(x, y)) != background
            and not _in_window(win, win.headerFrame).contains(QPoint(x, y))
            and not _in_window(win, win.infoFrame).contains(QPoint(x, y))
        ]
        assert ink, "빈 목록 안내가 전혀 그려지지 않았다"
        outside = [p for p in ink if not lst.contains(QPoint(*p))]
        assert not outside, (
            f"안내 픽셀 {len(outside)}개가 목록 사각형 {lst} 밖에 있다(예: {outside[:3]})"
        )
