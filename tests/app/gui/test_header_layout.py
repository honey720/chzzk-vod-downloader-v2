"""헤더바 1줄 가로 · 세로 배치 게이트 (#492 — 셸 S0, #245 두 행 헤더를 대신한다).

    ┌ [치지직 URL 입력          ][C:\\...\\cvdv2      ✕][경로 찾기][VOD 추가] ┐
    └ 해상도 가져오기 성공.                                                   ┘

- 한 줄에 왼쪽부터 URL 입력 · 저장 위치 입력 · 경로 찾기 · 등록(VOD 추가). 조회 상태 글은 그 아래 한 줄
- 두 텍스트 버튼([경로 찾기] · [VOD 추가])은 폭이 같다(#245에서 이어 온 규칙 — 번역 · 폰트를 따른다)
- 남는 폭은 두 입력창만 흡수하고 URL 입력이 더 많이 받는다(2 : 1). 버튼은 고정 폭
- 설정 톱니(#245)는 메뉴바 「설정」으로 옮겼다 — 두 행 · 톱니 세로 중앙 게이트는 대상이 없어져 뺐다

폰트에 의존하지 않는 기하 관계만 잰다(차례 · 같은 줄 · 폭 동일 · 증가 방향).
폭 3점은 절대 px가 아니라 창 최소폭에서 유도한다(#249).
"""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import CVDv2 as main_module
import app.theme as theme
from app.views.mainWindow import VodDownloader
from tests.app.gui.card_helpers import hold_style

#: 창 최소폭 / 조금 넓게 / 아주 넓게 — `_at_width`가 창 최소폭에서 유도한다.
WIDTHS = ("min", "mid", "wide")


def _resolve_width(win, which: str) -> int:
    """폭 이름을 실제 px로 — 창 최소폭에서 유도한다."""
    minimum = win.minimumWidth()
    return {"min": minimum, "mid": minimum + 300, "wide": minimum + 900}[which]


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp):
    """실제 전역 QSS를 태운다. ⚠️ `scope="function"` 유지(macOS 종료 크래시 —
    `test_widget_theme.py`의 `_apply_dark_card_qss` 문서 참고)."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))  # 참조 보관 — 이중 해제 우회 (#243, card_helpers.hold_style)
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))


@pytest.fixture
def window(qapp):
    win = VodDownloader()
    yield win
    win.deleteLater()
    QApplication.processEvents()


def _at_width(win, width):
    """창을 `width`("min"/"mid"/"wide" 또는 px)로 놓고, 실제 폭이 요청과 같은지 단언한다.

    resize()는 요청이지 결과가 아니다 — 최소폭 미만은 조용히 클램프되어 여러
    폭이 한 폭으로 붕괴한다(card_helpers.resize_to와 같은 이유).
    """
    if isinstance(width, str):
        width = _resolve_width(win, width)
    win.resize(width, 700)
    win.show()
    QApplication.processEvents()
    assert win.width() == width, f"요청 폭 {width}px인데 실제 {win.width()}px — 최소폭({win.minimumWidth()})에 클램프됐다"


def _right(w) -> int:
    return w.x() + w.width()


def _line_widgets(win) -> list:
    """헤더 1줄의 위젯 — 화면 차례(왼쪽부터)."""
    return [win.urlInput, win.downloadPathInput, win.downloadPathButton, win.fetchButton]


def _centre_y(win, w) -> float:
    """헤더 프레임 좌표계의 세로 중심."""
    top = w.mapTo(win.headerFrame, w.rect().topLeft()).y()
    return top + w.height() / 2


class TestStructure:
    def test_header_line_holds_url_path_find_and_add_in_that_order(self, window):
        """헤더 1줄 레이아웃의 항목은 URL 입력 · 저장 위치 입력 · 경로 찾기 · VOD 추가 차례여야 한다."""
        line = window.urlRowLayout
        items = [line.itemAt(i).widget() for i in range(line.count())]
        assert items == _line_widgets(window)

    def test_status_line_sits_under_the_header_line(self, window):
        """헤더 프레임 레이아웃은 1줄 다음에 조회 상태 글 한 줄이어야 한다."""
        frame = window.headerFrameLayout
        assert frame.count() == 2
        assert frame.itemAt(0).layout() is window.urlRowLayout
        assert frame.itemAt(1).widget() is window.linkStatusLabel

    def test_tab_order_follows_the_line_from_the_menu_to_the_list(self, window):
        """탭 순서는 메뉴 둘 → URL 입력 → 저장 위치 입력 → 경로 찾기 → VOD 추가 → 목록이어야 한다.

        포커스 체인에는 포커스를 안 받는 위젯(입력창의 지우기 버튼 등)도 끼어 있어,
        포커스를 받는 다음 위젯까지 건너 뛰어 잰다.
        """

        def next_focusable(widget):
            cursor = widget.nextInFocusChain()
            while cursor is not None and cursor is not widget:
                if cursor.focusPolicy() != Qt.FocusPolicy.NoFocus and cursor.isVisibleTo(window):
                    return cursor
                cursor = cursor.nextInFocusChain()
            return None

        window.show()
        QApplication.processEvents()
        chain = [window.menuDownloadButton, window.menuSettingsButton, *_line_widgets(window), window.listView]
        got = [next_focusable(w) for w in chain[:-1]]
        assert got == chain[1:]


class TestOneLine:
    @pytest.mark.parametrize("width", WIDTHS)
    def test_the_four_sit_on_one_line_left_to_right(self, window, width):
        """창 폭 min · mid · wide에서 헤더 네 위젯은 세로 중심이 같고(±1) 왼쪽부터 겹침 없이 놓여야 한다."""
        _at_width(window, width)
        widgets = _line_widgets(window)
        centres = [_centre_y(window, w) for w in widgets]
        assert max(centres) - min(centres) <= 1, f"폭 {width}: 세로 중심이 다르다 {centres}"
        for left, right in zip(widgets, widgets[1:]):
            assert _right(left) <= right.x(), f"폭 {width}: {left.objectName()}와 {right.objectName()}가 겹친다"


class TestTextButtonsShareOneWidth:
    """[경로 찾기]와 [VOD 추가] — 같은 종류(텍스트 버튼)끼리 폭이 같다(#245에서 이어 온 규칙)."""

    @pytest.mark.parametrize("width", WIDTHS)
    def test_widths_are_identical(self, window, width):
        """창 폭 min · mid · wide에서 두 텍스트 버튼의 폭이 같아야 한다."""
        _at_width(window, width)
        assert window.fetchButton.width() == window.downloadPathButton.width()


class TestButtonsStayEqualInEveryLanguage:
    """번역 로드 상태(ko / en)마다 두 텍스트 버튼의 폭이 같아야 한다.

    폭은 런타임 sizeHint로 맞추므로 텍스트 길이가 다른 언어에서도 같은 폭이
    나와야 한다. 번역기는 `CVDv2.set_language`가 **시작 시 한 번** 로드한다(런타임 전환
    경로 없음, #245 확인) — 그래서 각 언어를 새 창으로 만들어 잰다(재시작과 같은 조건).
    """

    @pytest.fixture
    def korean(self, qapp):
        from PySide6.QtCore import QTranslator

        translator = QTranslator()
        assert translator.load(main_module.resource_path("translations/ko_KR.qm")), "ko_KR.qm 로드 실패"
        qapp.installTranslator(translator)
        yield translator
        qapp.removeTranslator(translator)

    def _assert_equal_widths(self, win, width):
        _at_width(win, width)
        a, b = win.fetchButton, win.downloadPathButton
        assert a.width() == b.width(), f"폭 {width}: [{a.text()}] {a.width()} ≠ [{b.text()}] {b.width()}"

    @pytest.mark.parametrize("width", WIDTHS)
    def test_korean(self, qapp, korean, width):
        """한국어 번역에서 창 폭 min · mid · wide일 때 두 텍스트 버튼의 폭이 같아야 한다."""
        win = VodDownloader()
        try:
            assert win.fetchButton.text() == "VOD 추가", "ko 번역이 적용되지 않았다 — 전제 확인"
            self._assert_equal_widths(win, width)
        finally:
            win.deleteLater()
            QApplication.processEvents()

    @pytest.mark.parametrize("width", WIDTHS)
    def test_english(self, qapp, width):
        """원문(영어)에서 창 폭 min · mid · wide일 때 두 텍스트 버튼의 폭이 같아야 한다."""
        win = VodDownloader()
        try:
            assert win.fetchButton.text() == "Add VOD", "en 원문이 아니다 — 다른 번역기가 남아 있다"
            self._assert_equal_widths(win, width)
        finally:
            win.deleteLater()
            QApplication.processEvents()


class TestFreeSpaceGoesToTheInputsOnly:
    BUTTONS = ("fetchButton", "downloadPathButton")

    def test_buttons_keep_their_width_as_the_window_widens(self, window):
        """창이 min → mid → wide로 넓어져도 두 텍스트 버튼의 폭이 그대로여야 한다."""
        widths = {}
        for width in WIDTHS:
            _at_width(window, width)
            widths[width] = {name: getattr(window, name).width() for name in self.BUTTONS}
        assert widths[WIDTHS[0]] == widths[WIDTHS[1]] == widths[WIDTHS[2]], (
            f"버튼 폭이 창 폭을 따라 변한다: {widths} — 남는 공간은 입력창만 흡수해야 한다"
        )

    def test_inputs_grow_with_the_window_and_the_url_gets_more(self, window):
        """창이 넓어지면 두 입력창이 늘고, 늘어난 폭은 URL 입력이 저장 위치 입력보다 많이 받아야 한다(2 : 1)."""
        url, path = [], []
        for width in WIDTHS:
            _at_width(window, width)
            url.append(window.urlInput.width())
            path.append(window.downloadPathInput.width())
        assert url[0] < url[1] < url[2], f"URL 입력창이 폭을 따라 늘지 않는다: {url}"
        assert path[0] < path[1] < path[2], f"경로 입력창이 폭을 따라 늘지 않는다: {path}"
        assert url[2] - url[0] > path[2] - path[0], f"URL 입력이 남는 폭을 더 받지 않는다: {url} / {path}"
