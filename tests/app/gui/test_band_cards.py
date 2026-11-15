"""띠 카드 목록 · 바 경계 · 바 버튼 · 설정 톱니바퀴 · 진행 막대만 — 설계 §13 화면 모양 정리 (#611).

기대값은 테스트가 지시 값으로 따로 둔다(제품 토큰을 읽지 않는다). 화소는 창을 그려서(`grab`) 읽는다.
"""

from collections.abc import Callable, Iterator

import pytest
from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QWidget

import app.theme as theme
import CVDv2 as main_module
from app.viewmodels.data import ContentItem
from app.views.mainWindow import VodDownloader
from app.widgets.widget import ContentItemWidget
from core.models.download_state import DownloadState
from tests.app.gui.card_helpers import drop_new_top_levels, hold_style, snapshot_top_levels

OUTER = 12  # 창 바깥 좌우 여백 = 행 안쪽 좌우 여백(px) — 지시 값
ROW = 80  # 행 높이(px) — 지시 값
SEPARATOR = 1  # 행 사이 구분선(px) — 지시 값
CELL = ROW + SEPARATOR  # 한 칸(px) — 81
BAR = 4  # 진행 막대 높이(px)
SLACK = 24  # 색이 같다고 보는 한도 — 세 채널 차의 합
MANY = 12  # 창 높이 600에서 스크롤바가 보이는 카드 수


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp: QApplication) -> Iterator[None]:
    """실제 전역 QSS · 다크 테마로 잰다."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))
    before = snapshot_top_levels()
    yield
    drop_new_top_levels(before)


def _item(i: int, state: DownloadState = DownloadState.WAITING) -> ContentItem:
    item = ContentItem(
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
    item.downloadState = state
    return item


def _pump() -> None:
    for _ in range(4):
        QApplication.processEvents()


@pytest.fixture
def window(qapp: QApplication) -> Callable[..., VodDownloader]:
    """창을 만드는 함수 — 카드 상태 목록을 받아 창 폭 900 · 높이 600으로 띄운다. 창은 픽스처가 붙잡는다."""
    made: list[VodDownloader] = []

    def build(*states: DownloadState) -> VodDownloader:
        win = VodDownloader()
        made.append(win)
        win.monitorTimer.stop()
        win.show()
        _pump()
        for i, state in enumerate(states):
            item = _item(i, state)
            if state in (DownloadState.RUNNING, DownloadState.PAUSED):
                item.download_progress = 50
            win.contentManager.model.addItem(item)
        win.resize(900, 600)
        _pump()
        return win

    yield build
    for win in made:
        win.hide()


def _card(win: VodDownloader, row: int) -> ContentItemWidget:
    return win.listView.widgetFor(win.contentManager.model.itemAt(row))


def _rect(win: QWidget, widget: QWidget) -> QRect:
    return QRect(widget.mapTo(win, QPoint(0, 0)), widget.size())


def _pixel(image: QImage, x: int, y: int) -> QColor:
    ratio = image.devicePixelRatio()
    return image.pixelColor(int(x * ratio), int(y * ratio))


def _is(colour: QColor, token: str) -> bool:
    want = QColor(theme.current_tokens()[token])
    gap = (
        abs(colour.red() - want.red())
        + abs(colour.green() - want.green())
        + abs(colour.blue() - want.blue())
    )
    return gap <= SLACK


class TestBandRows:
    """카드 목록은 띠 — 바깥 여백 · 카드 사이 0, 행 사이 1px 구분선, 한 칸 81(설계 §13)."""

    def test_rows_are_separated_by_one_border_line(self, window) -> None:
        """이웃한 두 행 사이에는 1px `border` 선 하나만 있고 그 위아래는 행 바탕이어야 한다.

        대기 카드 둘, 창 폭 900 -> 첫 행 아래 끝 줄 ≈ border, 그 위 줄 ≈ cardBg, 둘째 행 첫 줄 ≈ cardBg,
        둘째 행 윗선 = 첫 행 아래 끝 + 1
        """
        win = window(DownloadState.WAITING, DownloadState.WAITING)
        first, second = _card(win, 0), _card(win, 1)
        top, below = _rect(win, first), _rect(win, second)
        assert below.top() == top.bottom() + 1
        image = win.grab().toImage()
        x = top.left() + top.width() * 3 // 4  # 글 · 버튼 밖 바탕 자리
        assert _is(_pixel(image, x, top.bottom()), "border")
        assert _is(_pixel(image, x, top.bottom() - 1), "cardBg")
        assert _is(_pixel(image, x, below.top()), "cardBg")

    def test_row_is_80_with_a_1px_separator_and_one_cell_is_81(self, window) -> None:
        """행은 80 + 구분선 1 = 81이고 목록의 한 칸(휠 · 화살표 걸음)이 81이어야 한다.

        대기 카드 12장(스크롤바 보임) -> 카드 높이 81, 스크롤바 singleStep 81
        """
        win = window(*[DownloadState.WAITING] * MANY)
        assert win.listView.verticalScrollBar().isVisible(), "전제: 스크롤바가 보인다"
        assert _card(win, 0).height() == CELL
        assert win.listView.verticalScrollBar().singleStep() == CELL

    @pytest.mark.parametrize("cards", [2, MANY], ids=["스크롤바 없음", "스크롤바 보임"])
    def test_thumbnail_and_row_text_share_the_bar_lines(self, window, cards: int) -> None:
        """썸네일 왼쪽 끝 = URL 칸 왼쪽 끝 = 12이고 3줄 오른쪽 끝 = 등록 버튼 오른쪽 끝 = 창 폭 − 12여야 한다.

        창 폭 900, 카드 2장 · 12장 -> 썸네일 x 12 == URL 칸 x, 3줄 오른쪽 끝 888 == 등록 버튼 오른쪽 끝,
        행 왼쪽 끝 0
        """
        win = window(*[DownloadState.WAITING] * cards)
        card = _card(win, 0)
        thumb, row = _rect(win, card.thumbnailLabel), _rect(win, card.statusRow)
        url, fetch = _rect(win, win.urlInput), _rect(win, win.fetchButton)
        assert thumb.left() == url.left() == OUTER
        assert row.right() == fetch.right() == win.width() - OUTER - 1
        assert _rect(win, card).left() == 0

    @pytest.mark.parametrize("cards", [2, MANY], ids=["스크롤바 없음", "스크롤바 보임"])
    def test_rows_span_the_list_at_the_minimum_window_width(self, window, cards: int) -> None:
        """창이 최소폭이어도 행은 창 왼쪽 끝부터 목록 오른쪽 끝(스크롤바가 보이면 그 왼쪽)까지 가야 한다.

        받는 중 카드 2장 · 12장, 창 폭 = 최소폭 -> 행 왼쪽 끝 0, 행 폭 + 보이는 스크롤바 폭 = 창 폭,
        3줄 오른쪽 끝 = 창 폭 − 12 − 1, 가로 스크롤 없음
        """
        win = window(*[DownloadState.RUNNING] * cards)
        win.resize(win.minimumWidth(), 600)
        _pump()
        assert win.width() == win.minimumWidth(), "전제: 창이 최소폭이다"
        bar = win.listView.verticalScrollBar()
        assert bar.isVisible() == (cards == MANY), "전제: 스크롤바 보임 여부"
        bar_width = bar.width() if bar.isVisible() else 0
        for row in range(cards):
            card = _card(win, row)
            rect = _rect(win, card)
            assert rect.left() == 0
            assert rect.width() + bar_width == win.width()
            assert _rect(win, card.statusRow).right() == win.width() - OUTER - 1
        assert win.windowScrollArea.horizontalScrollBar().maximum() == 0

    @pytest.mark.parametrize("state", [DownloadState.RUNNING, DownloadState.PAUSED])
    def test_progress_bar_lies_on_the_separator_without_a_track(self, window, state) -> None:
        """진행 막대는 행 아래끝(구분선 자리)에 겹치고 트랙을 그리지 않아야 한다.

        받는 중 · 일시정지 50% 카드 -> 막대 아래 끝 = 행 아래 끝 · 막대 폭 = 행 폭, 행 아래 끝 줄의 25% 자리 ≈
        상태 색(진행분), 75% 자리 ≈ border(트랙 없음 — 구분선)
        """
        win = window(state)
        card = _card(win, 0)
        bar = card.progressBar
        assert bar.isVisible(), "전제: 막대가 보인다"
        frame = card.contentFrame
        assert bar.geometry().getRect() == (0, frame.height() - BAR, frame.width(), BAR)
        rect = _rect(win, frame)
        image = win.grab().toImage()
        token = "stateRunning" if state == DownloadState.RUNNING else "statePaused"
        assert _is(_pixel(image, rect.left() + rect.width() // 4, rect.bottom()), token)
        assert _is(_pixel(image, rect.left() + rect.width() * 3 // 4, rect.bottom()), "border")

    @pytest.mark.parametrize(
        ("state", "token"),
        [(DownloadState.FINISHED, "cardTintFinished"), (DownloadState.FAILED, "cardTintFailed")],
    )
    def test_tint_covers_the_whole_row_width(self, window, state, token: str) -> None:
        """완료 · 실패 색조는 행 왼쪽 끝부터 오른쪽 끝까지(구분선 위)여야 한다.

        완료 · 실패 카드 -> 행 왼쪽 끝 화소 · 오른쪽 끝 화소(세로 가운데) ≈ 그 색조
        """
        win = window(state)
        rect = _rect(win, _card(win, 0))
        image = win.grab().toImage()
        middle = rect.top() + ROW // 2
        assert _is(_pixel(image, rect.left(), middle), token)
        assert _is(_pixel(image, rect.right(), middle), token)


class TestBarLines:
    """바 ↔ 목록 경계는 헤더바 아래 · 하단바 위 선 한 줄씩, 메뉴바 ↔ 헤더바 사이는 선 없음(설계 §13)."""

    def test_header_bottom_and_footer_top_are_lines_and_the_menu_bar_has_none(self, window) -> None:
        """헤더바 맨 아래 줄 · 하단바 맨 위 줄은 border, 메뉴바 맨 아래 줄은 바 면이어야 한다.

        대기 카드 둘, 창 폭 900 -> 헤더바 아래 줄 ≈ border, 하단바 위 줄 ≈ border, 메뉴바 아래 줄 ≈ surface,
        첫 행 윗선 = 헤더바 아래 끝 + 1(목록 위 여백 0)
        """
        win = window(DownloadState.WAITING, DownloadState.WAITING)
        image = win.grab().toImage()
        x = win.width() // 2
        header, footer, menu = (
            _rect(win, win.headerFrame),
            _rect(win, win.infoFrame),
            _rect(win, win.appMenuBar),
        )
        assert _is(_pixel(image, x, header.bottom()), "border")
        assert _is(_pixel(image, x, footer.top()), "border")
        assert _is(_pixel(image, x, menu.bottom()), "surface")
        assert _rect(win, _card(win, 0)).top() == header.bottom() + 1


BAR_BUTTONS = ["settingsButton", "fetchButton", "selectButton", "stopButton"]  # 동작 버튼(설계 §13)
DROP_FIELDS = ["saveLocationButton", "sortButton"]  # 넓은 폭의 값 고르는 칸(#611)
FIELD_PAD = (
    10  # 펼침 칸 오른쪽 안쪽 여백(px) — ⌄ 오른쪽 끝 ↔ 칸 오른쪽 안쪽 끝(테두리 안쪽 첫 화소)
)


class TestBarButtons:
    """동작 버튼은 한 규칙 — 평소 면 · 테두리 없음, 호버 때 면, 테두리는 입력 칸만(설계 §13)."""

    @pytest.mark.parametrize("name", BAR_BUTTONS)
    def test_bar_button_has_no_face_or_border_at_rest(self, window, name: str) -> None:
        """평소의 동작 버튼은 위 가장자리 · 왼쪽 안쪽 화소가 바 면 그대로여야 한다(테두리 · 면 없음).

        대기 카드 하나(■ 켬 — 받는 중 아님이라 꺼진 모양도 같은 규칙), 창 폭 900 -> 버튼 위 가장자리 가운데 ·
        왼쪽 안쪽 3px 자리 ≈ surface
        """
        win = window(DownloadState.WAITING)
        button = getattr(win, name)
        assert button.isVisible(), "전제: 버튼이 보인다"
        rect = _rect(win, button)
        image = win.grab().toImage()
        assert _is(_pixel(image, rect.center().x(), rect.top()), "surface")
        assert _is(_pixel(image, rect.left() + 3, rect.center().y()), "surface")

    @pytest.mark.parametrize("name", BAR_BUTTONS[:3])
    def test_bar_button_shows_the_hover_face(self, window, name: str) -> None:
        """포인터가 동작 버튼 위에 오면 버튼 면이 surfaceHover여야 한다(테두리는 없음).

        창에 합성 포인터 이동(버튼 가운데) -> 버튼 왼쪽 안쪽 3px 자리 ≈ surfaceHover, 위 가장자리 안쪽 2px 가운데 ≈
        surfaceHover(가장자리 줄은 둥근 면의 안티에일리어싱이 섞인다)
        """
        from PySide6.QtTest import QTest

        win = window(DownloadState.WAITING)
        win.urlInput.setText(
            "https://chzzk.naver.com/video/1"
        )  # 등록 버튼이 켜진다(빈 칸이면 꺼짐)
        _pump()
        button = getattr(win, name)
        assert button.isEnabled(), "전제: 버튼이 켜져 있다"
        rect = _rect(win, button)
        QTest.mouseMove(win.windowHandle(), rect.center())
        _pump()
        assert button.underMouse(), "전제: 포인터가 버튼 위다"
        image = win.grab().toImage()
        assert _is(_pixel(image, rect.left() + 3, rect.center().y()), "surfaceHover")
        assert _is(_pixel(image, rect.center().x(), rect.top() + 2), "surfaceHover")

    @pytest.mark.parametrize(
        ("name", "edge"),
        [
            ("urlInput", "border"),
            ("saveLocationButton", "border"),
            ("sortButton", "border"),
            ("settingsButton", "surface"),
            ("fetchButton", "surface"),
            ("selectButton", "surface"),
            ("stopButton", "surface"),
        ],
    )
    def test_only_input_fields_have_a_border(self, window, name: str, edge: str) -> None:
        """평소 테두리가 보이는 것은 입력 칸(URL · 펼침 칸)뿐이고 동작 버튼은 바 면이어야 한다.

        창 폭 900 -> 위 가장자리 가운데 화소: urlInput · saveLocationButton · sortButton ≈ border,
        settingsButton · fetchButton · selectButton · stopButton ≈ surface
        """
        win = window(DownloadState.WAITING)
        widget = getattr(win, name)
        assert widget.isVisible(), "전제: 보인다"
        rect = _rect(win, widget)
        image = win.grab().toImage()
        assert _is(_pixel(image, rect.center().x(), rect.top()), edge)


def _corner(image: QImage, rect: QRect) -> list[QColor]:
    """왼쪽 위 모서리 3×3 화소 — 모서리 반경이 같으면 같은 바 면 위에서 같은 화소가 나온다."""
    return [_pixel(image, rect.left() + dx, rect.top() + dy) for dy in range(3) for dx in range(3)]


def _same(a: QColor, b: QColor) -> bool:
    gap = abs(a.red() - b.red()) + abs(a.green() - b.green()) + abs(a.blue() - b.blue())
    return gap <= SLACK


class TestDropFields:
    """값을 고르는 칸(펼침 버튼)은 URL 입력칸과 같은 입력 칸 모양 — 테두리 · 면 · 모서리 · 높이(#611)."""

    @pytest.mark.parametrize("name", DROP_FIELDS)
    def test_drop_field_has_the_url_border_and_face_at_rest(self, window, name: str) -> None:
        """평소의 펼침 칸은 위 가장자리 · 왼쪽 안쪽 화소가 URL 칸과 같아야 한다(border · surfaceAlt).

        창 폭 900 -> 펼침 칸 위 가장자리 가운데 ≈ URL 칸 위 가장자리 가운데 ≈ border, 왼쪽 안쪽 3px 자리 ≈
        URL 칸 왼쪽 안쪽 3px 자리 ≈ surfaceAlt
        """
        win = window(DownloadState.WAITING)
        image = win.grab().toImage()
        url, field = _rect(win, win.urlInput), _rect(win, getattr(win, name))
        top = _pixel(image, field.center().x(), field.top())
        face = _pixel(image, field.left() + 3, field.center().y())
        assert _is(top, "border")
        assert _same(top, _pixel(image, url.center().x(), url.top()))
        assert _is(face, "surfaceAlt")
        assert _same(face, _pixel(image, url.left() + 3, url.center().y()))

    @pytest.mark.parametrize("name", DROP_FIELDS)
    def test_drop_field_has_the_url_height_and_corner(self, window, name: str) -> None:
        """펼침 칸은 URL 칸과 높이가 같고 왼쪽 위 모서리 3×3 화소가 같아야 한다(모서리 반경이 같다).

        창 폭 900 -> 높이 == urlInput 높이, 모서리 3×3 화소 각각 ≈ URL 칸의 같은 자리
        """
        win = window(DownloadState.WAITING)
        image = win.grab().toImage()
        url, field = _rect(win, win.urlInput), _rect(win, getattr(win, name))
        assert field.height() == url.height()
        for got, want in zip(_corner(image, field), _corner(image, url), strict=True):
            assert _same(got, want), (got.name(), want.name())

    @pytest.mark.parametrize("name", DROP_FIELDS)
    def test_caret_sits_at_the_right_inner_end(self, window, name: str) -> None:
        """⌄ 오른쪽 끝은 칸 오른쪽 안쪽 끝에서 FIELD_PAD 안이고, 그 자리에 ⌄가 그려지고, 글과 떨어져야 한다.

        창 폭 900, 글 "A"(칸 폭보다 훨씬 짧은 값) -> (칸 폭 − 2) − caretRect.right() == 10, caretRect 안에
        surfaceAlt 아닌 화소(⌄)가 있다, caretRect 왼쪽 끝 > 그린 글 오른쪽 끝
        """
        win = window(DownloadState.WAITING)
        button = getattr(win, name)
        button.setFullText("A")  # 글 바로 뒤 자리와 칸 오른쪽 안쪽 끝 자리가 갈린다
        _pump()
        caret = button.caretRect()
        assert (button.width() - 2) - caret.right() == FIELD_PAD
        image = button.grab().toImage()
        ink = [
            _pixel(image, x, y)
            for x in range(caret.left(), caret.right() + 1)
            for y in range(caret.top(), caret.bottom() + 1)
        ]
        assert any(not _is(colour, "surfaceAlt") for colour in ink), "⌄가 그 칸에 그려진다"
        text_right = button._textRect().left() + button.fontMetrics().horizontalAdvance(
            button.shownText()
        )
        assert caret.left() > text_right

    def test_drop_field_hover_shows_the_strong_border(self, window) -> None:
        """포인터가 펼침 칸 위에 오면 테두리가 borderStrong, 면은 surfaceAlt 그대로여야 한다.

        창에 합성 포인터 이동(정렬 칸 가운데) -> 위 가장자리 가운데 ≈ borderStrong, 왼쪽 안쪽 3px ≈ surfaceAlt
        """
        from PySide6.QtTest import QTest

        win = window(DownloadState.WAITING)
        button = win.sortButton
        rect = _rect(win, button)
        QTest.mouseMove(win.windowHandle(), rect.center())
        _pump()
        assert button.underMouse(), "전제: 포인터가 칸 위다"
        image = win.grab().toImage()
        assert _is(_pixel(image, rect.center().x(), rect.top()), "borderStrong")
        assert _is(_pixel(image, rect.left() + 3, rect.center().y()), "surfaceAlt")

    def test_pressed_drop_field_shows_the_accent_border(self, window) -> None:
        """눌린 펼침 칸(펼친 동안과 같은 규칙)은 테두리가 accent, 면은 surfaceAlt여야 한다.

        정렬 칸 setDown(True) -> 위 가장자리 가운데 ≈ accent, 왼쪽 안쪽 3px ≈ surfaceAlt
        """
        win = window(DownloadState.WAITING)
        button = win.sortButton
        button.setDown(True)
        _pump()
        assert button.isDown(), "전제: 눌린 상태다"
        rect = _rect(win, button)
        image = win.grab().toImage()
        assert _is(_pixel(image, rect.center().x(), rect.top()), "accent")
        assert _is(_pixel(image, rect.left() + 3, rect.center().y()), "surfaceAlt")

    def test_disabled_drop_field_keeps_the_field_border(self, window) -> None:
        """꺼진 펼침 칸도 테두리 border · 면 surfaceAlt가 남아야 한다(글만 흐려진다).

        정렬 칸 setEnabled(False) -> 위 가장자리 가운데 ≈ border, 왼쪽 안쪽 3px ≈ surfaceAlt
        """
        win = window(DownloadState.WAITING)
        button = win.sortButton
        button.setEnabled(False)
        _pump()
        rect = _rect(win, button)
        image = win.grab().toImage()
        assert _is(_pixel(image, rect.center().x(), rect.top()), "border")
        assert _is(_pixel(image, rect.left() + 3, rect.center().y()), "surfaceAlt")

    @pytest.mark.parametrize("width", ["first", "min"])
    def test_narrow_status_dropdown_is_a_bordered_field(self, window, width: str) -> None:
        """칩이 상태 드롭다운으로 바뀐 좁은 폭에서 그 드롭다운도 테두리 있는 입력 칸이어야 한다.

        창 폭 = 칩이 드롭다운으로 바뀌는 첫 폭(900에서 2px씩 줄여 찾음) · 최소폭 -> filterDropButton 보임,
        위 가장자리 가운데 ≈ border · 왼쪽 안쪽 3px ≈ surfaceAlt
        """
        win = window(DownloadState.WAITING)
        if width == "min":
            target = win.minimumWidth()
            win.resize(target, 600)
            _pump()
        else:
            target = 900
            while not win.filterChipBar.isCompact() and target > win.minimumWidth():
                target -= 2
                win.resize(target, 600)
                _pump()
        assert win.width() == target, "전제: 요청한 폭이다"
        drop = win.filterChipBar.dropButton
        assert drop.isVisible(), "전제: 상태 드롭다운이 보인다"
        rect = _rect(win, drop)
        image = win.grab().toImage()
        assert _is(_pixel(image, rect.center().x(), rect.top()), "border")
        assert _is(_pixel(image, rect.left() + 3, rect.center().y()), "surfaceAlt")

    @pytest.mark.parametrize("name", DROP_FIELDS)
    def test_icon_drop_field_is_a_bordered_square(self, window, name: str) -> None:
        """아이콘 모양 저장 위치 · 정렬 칸도 테두리 있는 정사각 입력 칸이어야 한다.

        창 폭 900, setCompact(True) -> 폭 == 높이, 위 가장자리 가운데 ≈ border, 왼쪽 안쪽 3px ≈ surfaceAlt
        """
        win = window(DownloadState.WAITING)
        button = getattr(win, name)
        button.setCompact(True)
        _pump()
        assert button.isCompact(), "전제: 아이콘 모양이다"
        rect = _rect(win, button)
        assert rect.width() == rect.height()
        image = win.grab().toImage()
        assert _is(_pixel(image, rect.center().x(), rect.top()), "border")
        assert _is(_pixel(image, rect.left() + 3, rect.center().y()), "surfaceAlt")


class TestSettingsGear:
    """설정은 메뉴바 오른쪽 끝 톱니바퀴 · Ctrl+,로 여는 모달 창 — 메뉴바 탭은 작업 화면만(설계 §4 · §13)."""

    @pytest.fixture
    def opened(self, monkeypatch: pytest.MonkeyPatch) -> list:
        """설정 창 `exec_`를 가로채 연 창을 모은다(실제 모달을 띄우지 않는다)."""
        import app.views.mainWindow as main_window_module

        seen: list = []
        monkeypatch.setattr(
            main_window_module.SettingDialog, "exec_", lambda self: seen.append(self) or 0
        )
        return seen

    def test_gear_sits_at_the_right_end_of_the_menu_bar(self, window) -> None:
        """톱니바퀴는 메뉴바 오른쪽 끝(창 폭 − 12)의 32 정사각이고, 메뉴바 탭은 「VOD 다운」 하나여야 한다.

        창 폭 900 -> 톱니바퀴 오른쪽 끝 888 · 크기 32×32 · 글 없음 · 툴팁 「Settings」로 시작, 메뉴바 글 버튼은
        menuDownloadButton 하나
        """
        from PySide6.QtWidgets import QPushButton

        win = window(DownloadState.WAITING)
        gear = win.settingsButton
        rect = _rect(win, gear)
        assert rect.right() == win.width() - OUTER - 1
        assert (gear.width(), gear.height()) == (32, 32)
        assert gear.text() == "" and gear.toolTip().startswith("Settings")
        texts = [b.objectName() for b in win.appMenuBar.findChildren(QPushButton) if b.text()]
        assert texts == ["menuDownloadButton"]

    def test_gear_opens_the_settings_dialog(self, window, opened: list) -> None:
        """톱니바퀴를 누르면 설정 창이 한 번 열려야 한다.

        톱니바퀴 클릭 -> 설정 창 exec_ 1번, 고른 메뉴는 「VOD 다운」 그대로
        """
        win = window(DownloadState.WAITING)
        win.settingsButton.click()
        _pump()
        assert len(opened) == 1
        assert win.menuDownloadButton.isChecked()

    def test_ctrl_comma_opens_the_settings_dialog(self, window, opened: list, qtbot) -> None:
        """Ctrl+,(macOS는 Cmd+,)를 누르면 설정 창이 한 번 열려야 한다.

        창 활성 · 목록에 초점, Ctrl+, 키 -> 설정 창 exec_ 1번
        """
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        win = window(DownloadState.WAITING)
        win.activateWindow()
        qtbot.waitUntil(win.isActiveWindow, timeout=3000)
        QTest.keyClick(win.listView, Qt.Key.Key_Comma, Qt.KeyboardModifier.ControlModifier)
        _pump()
        assert len(opened) == 1


class TestBarOnlyProgress:
    """진행 표시는 막대 하나 — 배경 채움 방식과 그 설정 키는 없다(설계 §13)."""

    def test_a_leftover_fill_row_still_shows_the_bar(self, window) -> None:
        """설정 DB에 옛 `cardProgress` = "fill" 행이 남아 있어도 받는 중 카드는 막대를 보이고 면을 칠하지 않아야 한다.

        DB 행 cardProgress = "fill"(JSON) · 카드 화면 설정 다시 읽음, 받는 중 50% 카드 -> 막대 보임 · 막대 값 5000,
        카드 면 25% 자리(막대 위 글 밖) ≈ cardBg, 그 행은 지워지지 않고 남음(읽을 때 버린다)
        """
        import app.viewmodels.card_display as card_display
        from tests.support.settings_db import read_rows, write_rows

        write_rows({"cardProgress": '"fill"'})
        card_display.use(None)
        try:
            win = window(DownloadState.RUNNING)
            card = _card(win, 0)
            assert card.progressBar.isVisible()
            assert card.progressBar.value() == 5000
            frame = card.contentFrame
            rect = _rect(win, frame)
            image = win.grab().toImage()
            assert _is(_pixel(image, rect.left() + rect.width() // 4, rect.top() + 4), "cardBg")
            assert read_rows().get("cardProgress") == '"fill"'
        finally:
            card_display.use(None)
