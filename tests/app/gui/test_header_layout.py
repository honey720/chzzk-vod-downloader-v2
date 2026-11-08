"""헤더바 1줄 가로 · 세로 배치 게이트 (#492 — 셸 S0, #495 — S1).

    ┌ [치지직 URL 입력                    ][📁 Downloads ▾][VOD 추가] ┐
    │ (등록 오류 한 줄 — 평소 빈 줄)                                     │
    └ [전체 3][대기 1] … [정렬 ▾]                         [완료 지우기] ┘

- 한 줄에 왼쪽부터 URL 입력 · 저장 위치 버튼 · 등록(VOD 추가). 그 아래 등록 오류 줄, 그 아래 2줄
- 남는 폭은 URL 입력만 받는다. 저장 위치 버튼은 기준 글(`WIDTH_SAMPLE`)이 들어가는 고정 폭
  (#495 — 고른 위치에 따라 폭이 바뀌면 헤더바가 움직인다)
- 저장 위치 입력 · [경로 찾기](#245의 같은 폭 짝)는 저장 위치 버튼으로 바뀌었다(#495)

폰트에 의존하지 않는 기하 관계만 잰다(차례 · 같은 줄 · 폭 동일 · 증가 방향).
폭 3점은 절대 px가 아니라 창 최소폭에서 유도한다(#249).
"""

from collections.abc import Iterator

import pytest
from PySide6.QtCore import Qt, QTranslator
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QApplication, QWidget

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
def _apply_production_qss(qapp) -> None:
    """실제 전역 QSS를 태운다. ⚠️ `scope="function"` 유지(macOS 종료 크래시 —
    `test_widget_theme.py`의 `_apply_dark_card_qss` 문서 참고)."""
    theme.set_color_scheme("dark")
    qapp.setStyle(
        hold_style(theme.build_style())
    )  # 참조 보관 — 이중 해제 우회 (#243, card_helpers.hold_style)
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))


@pytest.fixture
def window(qapp) -> Iterator[VodDownloader]:
    win = VodDownloader()
    yield win
    win.deleteLater()
    QApplication.processEvents()


def _at_width(win, width) -> None:
    """창을 `width`("min"/"mid"/"wide" 또는 px)로 놓고, 실제 폭이 요청과 같은지 단언한다.

    resize()는 요청이지 결과가 아니다 — 최소폭 미만은 조용히 클램프되어 여러
    폭이 한 폭으로 붕괴한다(card_helpers.resize_to와 같은 이유).
    """
    if isinstance(width, str):
        width = _resolve_width(win, width)
    win.resize(width, 700)
    win.show()
    QApplication.processEvents()
    assert win.width() == width, (
        f"요청 폭 {width}px인데 실제 {win.width()}px — 최소폭({win.minimumWidth()})에 클램프됐다"
    )


def _font_whose_rounded_width_elides(base: QFont, text: str) -> QFont:
    """`base`에 글자 사이 간격(소수 px)을 더해, 정수 글 폭(`horizontalAdvance`) 칸에서 `text`가 말줄임되는 글꼴.

    간격이 소수면 글 폭이 소수가 된다 — 글자 폭이 소수인 글꼴 엔진(Windows 실제 화면)과 같은 조건이다.
    반올림한 정수 폭이 소수 폭보다 좁아지는 간격을 0.01 ~ 0.99px에서 찾는다. 글꼴마다 그 값이 달라
    박지 않는다(#525).
    """
    for hundredths in range(1, 100):
        font = QFont(base)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, hundredths / 100)
        metrics = QFontMetrics(font)
        rounded = metrics.horizontalAdvance(text)
        if metrics.elidedText(text, Qt.TextElideMode.ElideMiddle, rounded) != text:
            return font
    pytest.fail(f"전제: 0.01 ~ 0.99px 간격 가운데 정수 폭 칸에서 {text!r}가 말줄임되는 값이 없다")


def _right(w) -> int:
    return w.x() + w.width()


def _line_widgets(win) -> list:
    """헤더 1줄의 위젯 — 화면 차례(왼쪽부터)."""
    return [win.urlInput, win.saveLocationButton, win.fetchButton]


def _centre_y(win, w) -> float:
    """헤더 프레임 좌표계의 세로 중심."""
    top = w.mapTo(win.headerFrame, w.rect().topLeft()).y()
    return top + w.height() / 2


class TestStructure:
    def test_header_line_holds_url_location_and_add_in_that_order(self, window) -> None:
        """헤더 1줄 레이아웃의 항목은 URL 입력 · 저장 위치 버튼 · VOD 추가 차례여야 한다."""
        line = window.urlRowLayout
        items = [line.itemAt(i).widget() for i in range(line.count())]
        assert items == _line_widgets(window)

    def test_error_line_sits_between_the_two_lines(self, window) -> None:
        """헤더 프레임 레이아웃은 1줄 · 등록 오류 줄 · 2줄(칩 · 정렬 · 완료 지우기) 차례여야 한다."""
        frame = window.headerFrameLayout
        assert frame.count() == 3
        assert frame.itemAt(0).layout() is window.urlRowLayout
        assert frame.itemAt(1).widget() is window.registerErrorLabel
        assert frame.itemAt(2).layout() is window.filterRowLayout

    def test_second_line_holds_chips_sort_and_clear_finished(self, window) -> None:
        """헤더 2줄 레이아웃은 칩 줄 · 정렬 버튼 · 빈칸 · 완료 지우기 차례여야 한다."""
        line = window.filterRowLayout
        assert [line.itemAt(i).widget() for i in range(line.count())] == [
            window.filterChipBar,
            window.sortButton,
            None,
            window.clearFinishedButton,
        ]

    def test_tab_order_follows_the_line_from_the_menu_to_the_list(self, window) -> None:
        """탭 순서는 메뉴 둘 → URL 입력 → 저장 위치 버튼 → VOD 추가 → 칩 여섯 → 정렬 → 완료 지우기 → 목록이어야 한다.

        포커스 체인에는 포커스를 안 받는 위젯(입력창의 지우기 버튼 등)도 끼어 있어,
        포커스를 받는 다음 위젯까지 건너 뛰어 잰다.
        """

        def next_focusable(widget) -> QWidget | None:
            cursor = widget.nextInFocusChain()
            while cursor is not None and cursor is not widget:
                if cursor.focusPolicy() != Qt.FocusPolicy.NoFocus and cursor.isVisibleTo(window):
                    return cursor
                cursor = cursor.nextInFocusChain()
            return None

        window.show()
        QApplication.processEvents()
        chain = [
            window.menuDownloadButton,
            window.menuSettingsButton,
            *_line_widgets(window),
            *window.filterChips.values(),
            window.sortButton,
            window.clearFinishedButton,
            window.listView,
        ]
        got = [next_focusable(w) for w in chain[:-1]]
        assert got == chain[1:]


class TestOneLine:
    @pytest.mark.parametrize("width", WIDTHS)
    def test_the_three_sit_on_one_line_left_to_right(self, window, width) -> None:
        """창 폭 min · mid · wide에서 헤더 세 위젯은 세로 중심이 같고(±1) 왼쪽부터 겹침 없이 놓여야 한다."""
        _at_width(window, width)
        widgets = _line_widgets(window)
        centres = [_centre_y(window, w) for w in widgets]
        assert max(centres) - min(centres) <= 1, f"폭 {width}: 세로 중심이 다르다 {centres}"
        for left, right in zip(widgets, widgets[1:]):
            assert _right(left) <= right.x(), (
                f"폭 {width}: {left.objectName()}와 {right.objectName()}가 겹친다"
            )


class TestSaveLocationButtonWidth:
    """저장 위치 버튼은 기준 글이 들어가는 고정 폭이다 — 고른 위치 · 언어와 무관하다(#495)."""

    @pytest.fixture
    def korean(self, qapp) -> Iterator[QTranslator]:
        from PySide6.QtCore import QTranslator

        translator = QTranslator()
        assert translator.load(main_module.resource_path("translations/ko_KR.qm")), (
            "ko_KR.qm 로드 실패"
        )
        qapp.installTranslator(translator)
        yield translator
        qapp.removeTranslator(translator)

    def test_the_sample_name_fits_without_eliding(self, window) -> None:
        """저장 위치 버튼 폭에서 기준 글 Downloads가 줄지 않고 그려져야 한다."""
        from app.widgets.header_controls import WIDTH_SAMPLE

        _at_width(window, "min")
        button = window.saveLocationButton
        button.setFullText(WIDTH_SAMPLE)
        assert button.isVisible()
        assert button.shownText() == WIDTH_SAMPLE

    def test_the_sample_name_fits_when_the_text_width_is_fractional(self) -> None:
        """글 폭이 소수인 글꼴이어도 저장 위치 버튼에 기준 글 Downloads가 줄지 않고 그려져야 한다.

        정수 글 폭 칸에서는 기준 글이 말줄임되는 소수 글자 간격 -> 버튼이 그리는 글 = Downloads (#525)
        """
        from app.widgets.header_controls import WIDTH_SAMPLE, SaveLocationButton

        button = SaveLocationButton()
        button.ensurePolished()
        font = _font_whose_rounded_width_elides(button.font(), WIDTH_SAMPLE)
        button.setFont(font)
        button.ensurePolished()
        assert button.font() == font, "전제: 전역 QSS가 글꼴을 덮지 않는다"
        button.applyWidth()
        button.setFullText(WIDTH_SAMPLE)
        assert button.shownText() == WIDTH_SAMPLE

    def test_a_long_name_is_elided_in_the_middle_without_widening(self, window) -> None:
        """기준 글보다 긴 폴더 이름은 폭을 바꾸지 않고 가운데가 줄어야 한다(앞 · 끝 글자는 남는다)."""
        _at_width(window, "min")
        button = window.saveLocationButton
        before = button.width()
        long_name = "앞쪽" + "가" * 60 + "뒤쪽"
        button.setFullText(long_name)
        QApplication.processEvents()
        shown = button.shownText()
        assert button.width() == before
        assert shown != long_name and "…" in shown
        assert shown.startswith("앞") and shown.endswith("쪽")

    @pytest.mark.parametrize("width", WIDTHS)
    def test_the_width_is_the_same_in_korean_and_english(self, qapp, korean, width) -> None:
        """한국어 번역을 올려도 창 폭 min · mid · wide에서 저장 위치 버튼 폭은 원문과 같아야 한다."""
        qapp.removeTranslator(korean)
        english = VodDownloader()
        qapp.installTranslator(korean)
        hangul = VodDownloader()
        try:
            assert hangul.fetchButton.text() == "VOD 추가", "ko 번역이 적용되지 않았다 — 전제 확인"
            _at_width(english, width)
            _at_width(hangul, width)
            assert hangul.saveLocationButton.width() == english.saveLocationButton.width()
        finally:
            english.deleteLater()
            hangul.deleteLater()
            QApplication.processEvents()


class TestFreeSpaceGoesToTheUrlInputOnly:
    BUTTONS = ("saveLocationButton", "fetchButton")

    def test_buttons_keep_their_width_as_the_window_widens(self, window) -> None:
        """창이 min → mid → wide로 넓어져도 저장 위치 버튼 · VOD 추가의 폭이 그대로여야 한다."""
        widths = {}
        for width in WIDTHS:
            _at_width(window, width)
            widths[width] = {name: getattr(window, name).width() for name in self.BUTTONS}
        assert widths[WIDTHS[0]] == widths[WIDTHS[1]] == widths[WIDTHS[2]], (
            f"버튼 폭이 창 폭을 따라 변한다: {widths} — 남는 공간은 URL 입력만 받아야 한다"
        )

    def test_the_url_input_grows_with_the_window(self, window) -> None:
        """창이 min → mid → wide로 넓어지면 URL 입력이 그만큼 늘어야 한다."""
        url = []
        for width in WIDTHS:
            _at_width(window, width)
            url.append(window.urlInput.width())
        assert url[1] - url[0] == 300 and url[2] - url[0] == 900, (
            f"URL 입력이 남는 폭을 다 받지 않는다: {url}"
        )
