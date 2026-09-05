"""창 셸(창 최소폭 바닥·창 전체 좌우 스크롤 안전망·오버레이 좌표) 게이트 (#249).

구조(ui/mainWindow.py):

    VodDownloader
    └ windowScrollArea   창 폭이 콘텐츠 최소폭보다 좁을 때만 좌우 스크롤 — 평소엔 안 보인다
      └ contentColumn    상단바·카드 목록·하단바를 담는 컨테이너 — QScrollArea가 자식을 하나만
        ├ headerFrame    받으므로 셋을 스크롤 영역에 넣으려면 필요하다. 폭 제한·정렬은 없다
        ├ listView       (_Overlay는 이 안에 있다 — 드래그 반투명·빈 목록 안내)
        └ infoFrame

세 가지를 잰다.
② 창 최소폭 = 콘텐츠 최소폭만(#251) — 화면 크기를 작게(800)·크게(3440) 주입해도 변하지
   않는다. 초기 크기는 별개의 관심사(#253): 폭 = min(작업 영역 폭 × 0.45, 상한 토큰), 높이 =
   작업 영역 높이 × 0.5. 초기 폭이 최소보다 작게 나오는 작은 화면에서는 Qt가 최소로
   클램프한다. 화면 기준은 전체 화면이 아니라 availableGeometry다 — 둘을 각각 잰다.
③ 좌우 스크롤 — 평소 폭 어디서도 안 보이고, 창이 콘텐츠보다 좁게 강제됐을 때만 뜬다.
   그 안전망이 서려면 셋을 담는 컨테이너(contentColumn)가 스크롤 영역의 유일한 자식이어야
   한다 — 컨테이너의 존재 이유를 재는 게이트가 함께 있다.
④ 오버레이 — 콘텐츠 영역(listView) 좌표계에 있다. 창 기준이 아니다.

폭은 절대 px가 아니라 유도한다: 창 최소폭은 창에서 읽고(콘텐츠 최소는 폰트·OS마다
다르다 — offscreen 674 / Windows 실기 406), 넓은 폭은 거기에 더한다. 콘텐츠 최소폭도
제품의 계산(`_contentMinimumWidth`)을 부르지 않고 leaf 위젯의 힌트를 테스트가 독립
합산한다 — 제품이 틀리면 테스트도 같이 틀리기 때문이다.
"""

import pytest
from PySide6.QtCore import QMimeData, QPoint, QRect, QSize, Qt
from PySide6.QtGui import QDragEnterEvent
from PySide6.QtWidgets import QApplication, QScrollArea

import main as main_module
import theme
from app.views.mainWindow import VodDownloader
from content.data import ContentItem
from core.models.download_state import DownloadState
from tests.unit.card_helpers import drop_new_top_levels, hold_style, snapshot_top_levels

#: "넓은 창" = 창 최소폭 + 이만큼 — 절대 px 대신 창 최소폭에서 유도한다.
WIDE_EXTRA = 800


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
    """카드의 썸네일·크기 조회가 실호출로 새지 않게 세션을 막는다 — 호출되면 즉시 실패한다."""

    class _FailingSession:
        def head(self, *a, **k):
            """HEAD 요청은 금지 — 호출 자체가 테스트 실패다."""
            raise RuntimeError("network disabled in tests")

        def get(self, *a, **k):
            """GET 요청은 금지 — 호출 자체가 테스트 실패다."""
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())


#: 테스트가 주입하는 화면 논리폭 — 초기 폭(× 0.25)이 콘텐츠 최소보다 작은 쪽과 큰 쪽.
SCREENS = (800, 3440)


def _fake_screen(monkeypatch, width: int, height: int = 600) -> None:
    """창이 보는 작업 영역(availableGeometry)을 흉내 낸다 — 초기 크기에는 반영되고 최소폭에는 반영되지 않아야 한다."""
    monkeypatch.setattr(
        VodDownloader, "_availableGeometry", lambda self, near=None: QRect(0, 0, width, height)
    )


def _make_item(i: int) -> ContentItem:
    """대기 카드용 최소 아이템 — 제목·채널만 다르고 해상도 목록은 비어 있다."""
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
    """실제 메인 창을 만들고 대기 카드 `cards`장을 모델 경로로 넣는다(실배선)."""
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
# ② 창 최소폭 = 콘텐츠 최소폭 (화면과 무관) / 초기 크기 = 화면 비율
# ---------------------------------------------------------------------------


class TestMinimumWidthIsTheContentMinimumRegardlessOfScreen:
    """창 최소폭은 콘텐츠 최소폭 하나로 정해진다 — 화면 크기 항이 없다 (#251).

    ★ 핵심 단언: 화면을 작게(800)·크게(3440) 주입해도 최소폭이 같고, 그 값이 leaf 위젯
    힌트로 독립 합산한 콘텐츠 최소폭과 일치한다. 비율 항이 max()로 섞이면 큰 화면 쪽이
    콘텐츠 최소보다 올라가 실패한다.
    """

    @pytest.mark.parametrize("screen", SCREENS)
    def test_minimum_width_equals_the_content_minimum(self, monkeypatch, screen):
        """주입한 화면 크기가 무엇이든 최소폭 == 콘텐츠 최소폭(독립 합산)."""
        _fake_screen(monkeypatch, screen)
        win = _window()
        win.show()
        QApplication.processEvents()
        content_min = _independent_content_minimum(win)
        assert content_min != int(screen * 0.25), (
            "전제: 주입한 화면의 비율값이 콘텐츠 최소와 우연히 같으면 못 가른다"
        )
        assert win.minimumWidth() == content_min, (
            f"화면 {screen}px 주입: 창 최소폭 {win.minimumWidth()} ≠ 콘텐츠 최소폭 {content_min}"
        )

    def test_minimum_width_does_not_move_between_a_small_and_a_large_screen(self, monkeypatch):
        """같은 콘텐츠면 화면이 800이든 3440이든 최소폭은 같은 숫자다."""
        widths = {}
        for screen in SCREENS:
            _fake_screen(monkeypatch, screen)
            win = _window()
            win.show()
            QApplication.processEvents()
            widths[screen] = win.minimumWidth()
        assert len(set(widths.values())) == 1, f"화면 크기에 따라 최소폭이 움직인다: {widths}"


class TestInitialSizeRule:
    """초기 크기(#253): 폭 = min(작업 영역 폭 × 0.45, `theme.METRICS["initialWidthMax"]`), 높이 = 작업 영역 × 0.5.

    기대값은 테스트가 토큰과 주입 화면에서 직접 계산한다(제품 함수 미사용). 콘텐츠 최소폭
    아래로 내려가는 작은 화면에서는 Qt 클램프가 개입하므로 그 경우만 max()를 씌운다.
    """

    @pytest.mark.parametrize("screen_w", (1366, 1536, 1707, 1920, 2560, 3440))
    def test_width_is_the_ratio_capped_by_the_token(self, monkeypatch, screen_w):
        """작업 영역 폭을 주입하면 초기 폭 = min(폭 × 0.45, 상한) (콘텐츠 최소 아래면 최소)."""
        _fake_screen(monkeypatch, screen_w, 800)
        win = _window()
        win.show()
        QApplication.processEvents()
        expected = max(
            win.minimumWidth(), min(int(screen_w * 0.45), theme.METRICS["initialWidthMax"])
        )
        assert (win.width(), win.height()) == (expected, 400), (
            f"작업 영역 {screen_w}px: 초기 크기 {win.size().toTuple()} ≠ ({expected}, 400)"
        )

    def test_the_cap_wins_on_a_large_screen(self, monkeypatch):
        """3440 주입: 0.45면 1548인데 상한 토큰에서 멈춘다 — 비율은 선형이지만 적당한 첫 크기는 선형이 아니다."""
        _fake_screen(monkeypatch, 3440, 1440)
        cap = theme.METRICS["initialWidthMax"]
        assert int(3440 * 0.45) > cap, "전제: 비율값이 상한보다 커야 상한이 드러난다"
        win = _window()
        win.show()
        QApplication.processEvents()
        assert win.width() == cap, (
            f"큰 화면에서 초기 폭이 상한({cap})에 멈추지 않았다: {win.width()}"
        )
        assert win.maximumWidth() > cap, "상한은 첫 크기에만 걸린다 — 창 최대폭 제한이 아니다"

    def test_the_ratio_wins_on_a_small_screen(self, monkeypatch):
        """1366 주입: 0.45 → 614, 상한 아래이므로 비율값 그대로(콘텐츠 최소보다 크면)."""
        _fake_screen(monkeypatch, 1366, 768)
        win = _window()
        win.show()
        QApplication.processEvents()
        expected = max(win.minimumWidth(), int(1366 * 0.45))
        assert expected < theme.METRICS["initialWidthMax"], (
            "전제: 작은 화면에서는 비율값이 상한 아래여야 한다"
        )
        assert win.width() == expected, (
            f"작은 화면에서 비율이 반영되지 않았다: {win.width()} ≠ {expected}"
        )

    def test_tiny_screen_is_clamped_up_to_the_minimum_width(self, monkeypatch):
        """800×600 주입: 초기 폭 요청 360은 콘텐츠 최소 아래 → Qt가 최소폭으로 클램프한다(높이는 300 그대로)."""
        _fake_screen(monkeypatch, 800, 600)
        win = _window()
        win.show()
        QApplication.processEvents()
        assert win.minimumWidth() > int(800 * 0.45), (
            "전제: 콘텐츠 최소폭이 비율값(360)보다 커야 클램프가 일어난다"
        )
        assert win.width() == win.minimumWidth(), (
            f"초기 폭이 최소폭으로 클램프되지 않았다: {win.width()} vs {win.minimumWidth()}"
        )
        assert win.height() == 300, f"초기 높이는 비율(600×0.5) 그대로여야 한다: {win.height()}"

    def test_minimum_height_still_follows_the_screen_ratio(self, monkeypatch):
        """최소 높이는 바꾸지 않았다 — 작업 영역 높이 × 0.5 그대로(가로만 바꾼 비대칭이 의도, #251)."""
        heights = {}
        for screen_h in (600, 1440):
            _fake_screen(monkeypatch, 3440, screen_h)
            win = _window()
            win.show()
            QApplication.processEvents()
            heights[screen_h] = win.minimumHeight()
        assert heights == {600: 300, 1440: 720}, f"최소 높이가 화면 비율을 따르지 않는다: {heights}"

    def test_screen_basis_is_the_available_geometry_not_the_full_screen(self, monkeypatch):
        """화면 기준은 availableGeometry(작업표시줄·독 제외)다 — 전체 화면(size())이면 창이 화면 밖으로 나간다.

        이음새(`_availableGeometry`)를 흉내 내지 않고 그 **안**의 화면 객체를 흉내 낸다 — 전체
        화면과 작업 영역이 다른 화면을 주면 어느 쪽을 읽는지 창 크기에 드러난다.
        """

        class _Screen:
            def size(self):
                """전체 화면 크기 — 작업 영역과 다르게 둔다."""
                return QSize(2400, 1000)

            def availableGeometry(self):
                """작업 영역 — 전체 화면보다 작다."""
                return QRect(0, 0, 1600, 900)

        class _App:
            @staticmethod
            def primaryScreen():
                """주 화면은 위의 가짜 화면 하나."""
                return _Screen()

            @staticmethod
            def screenAt(point):
                """어느 점에도 화면이 없다고 답한다 — 주 화면 경로로 떨어진다."""
                return None

        import app.views.mainWindow as module

        monkeypatch.setattr(module, "QApplication", _App)
        win = _window()
        win.show()
        QApplication.processEvents()
        from_available = (min(int(1600 * 0.45), theme.METRICS["initialWidthMax"]), 450)
        from_full = (min(int(2400 * 0.45), theme.METRICS["initialWidthMax"]), 500)
        assert from_available != from_full, "전제: 두 기준이 다른 크기를 내야 가를 수 있다"
        assert win.minimumWidth() < from_available[0], (
            "전제: 작업 영역 기준 폭이 콘텐츠 최소보다 커야 클램프에 가려지지 않는다"
        )
        assert (win.width(), win.height()) == from_available, (
            f"초기 크기 {win.size().toTuple()} — 작업 영역 {from_available}이 아니라 전체 화면 {from_full} 기준으로 계산됐다"
        )


class TestWindowMinimumWidthHasAContentFloor:
    """바닥 폭에서 레이아웃이 깨지지 않고, 바닥은 상수가 아니라 레이아웃에서 온다."""

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
    @pytest.mark.parametrize("which", ("min", "mid", "wide"))
    def test_no_scrollbar_at_any_ordinary_width(self, which):
        """평소 폭(최소·중간·넓게) 어디서도 창 전체 스크롤바는 가로·세로 모두 보이지 않는다."""
        win = _window(cards=30)  # 세로 스크롤바가 떠 있는 상태여도 가로는 없어야 한다
        minimum = win.minimumWidth()
        width = {"min": minimum, "mid": minimum + 300, "wide": minimum + WIDE_EXTRA}[which]
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

    def test_the_container_is_the_scroll_areas_only_child_and_holds_all_three(self):
        """contentColumn의 존재 이유 — QScrollArea는 자식 위젯을 하나만 받는다.

        상단바·목록·하단바가 그 하나(컨테이너) 안에 있어야 셋이 함께 스크롤 영역 안에서
        움직인다. 컨테이너를 걷어내고 셋을 중앙 위젯에 직접 넣으면 창 전체 좌우 스크롤이
        성립하지 않는다.
        """
        win = _window(cards=3)
        win.show()
        QApplication.processEvents()
        area = win.centralWidget()
        assert isinstance(area, QScrollArea), (
            "중앙 위젯이 스크롤 영역이 아니면 창 전체 좌우 스크롤이 없다"
        )
        container = area.widget()
        assert container is win.contentColumn
        for w in (win.headerFrame, win.listView, win.infoFrame):
            assert w.parentWidget() is container, f"{w.objectName()}이(가) 컨테이너 밖에 있다"
        assert container.width() == area.viewport().width(), (
            "컨테이너는 창 폭을 그대로 채운다(폭 제한 없음)"
        )

    def test_the_container_fills_the_window_at_every_width(self):
        """폭 제한이 없다 — 어느 폭에서도 컨테이너와 상단바·목록·하단바가 창을 가득 채운다."""
        win = _window(cards=3)
        outer = theme.METRICS["outerMargin"]
        for width in (
            win.minimumWidth(),
            win.minimumWidth() + 300,
            win.minimumWidth() + WIDE_EXTRA,
        ):
            _at_width(win, width)
            assert win.contentColumn.width() == width
            for w in (win.headerFrame, win.listView, win.infoFrame):
                rect = _in_window(win, w)
                assert (rect.left(), rect.width()) == (outer, width - 2 * outer), (
                    f"{w.objectName()} {rect} — 창 {width}px"
                )


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
        """오버레이 사각형은 목록 사각형과 같다 — 창 사각형이 아니다(카드 유무 무관)."""
        win = _window(cards=cards)
        _at_width(win, win.minimumWidth() + WIDE_EXTRA)
        overlay = _in_window(win, win.listView._overlay)
        lst = _in_window(win, win.listView)
        assert overlay == lst, f"오버레이 {overlay} ≠ 목록 {lst}"
        assert lst != win.rect() and lst.top() > 0 and lst.width() < win.width(), (
            "전제: 목록은 창 전체가 아니다(상단바 아래, 좌우 여백 안)"
        )

    def test_drag_overlay_dims_the_list_but_not_the_margins(self):
        """실제 렌더로 확인 — 드래그 중 어두워지는 픽셀은 목록 안에만 있고 목록 바깥 여백은 그대로다."""
        win = _window(cards=3)
        _at_width(win, win.minimumWidth() + WIDE_EXTRA)
        before = win.grab().toImage()
        _drag_enter(win.listView)
        assert win.listView._dragActive
        after = win.grab().toImage()
        lst = _in_window(win, win.listView)
        inside = QPoint(lst.center().x(), lst.top() + 3)  # 카드 사이 여백이 아닌 목록 위쪽 배경
        margin = QPoint(lst.left() // 2, lst.center().y())  # 목록 왼쪽 바깥 여백(outerMargin)
        assert before.pixel(inside) != after.pixel(inside), (
            "드래그 중인데 목록 안 픽셀이 어두워지지 않았다"
        )
        assert before.pixel(margin) == after.pixel(margin), (
            "목록 바깥 여백까지 어두워졌다 — 오버레이가 창 기준으로 그려졌다"
        )

    def test_empty_hint_pixels_stay_within_the_list_rect(self):
        """빈 목록 안내 글자는 목록 사각형 안에만 찍힌다(글꼴 무관 — 위치만 본다)."""
        win = _window(cards=0)
        _at_width(win, win.minimumWidth() + WIDE_EXTRA)
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
