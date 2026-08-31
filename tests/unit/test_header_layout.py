"""상단(헤더) 가로·세로 배치 게이트 (#245 — 설정 버튼 위치 확정).

오너 확정 구조:

    ┌ [치지직 URL 입력          ][VOD 추가 ] ┐
    │                                        │  ⚙
    └ [C:\\...\\cvdv2          ✕][경로 찾기 ] ┘
    해상도 가져오기 성공.

- 입력 블록(두 행)은 완결된 사각형이다 — **텍스트 버튼 둘([VOD 추가]·
  [경로 찾기])은 폭이 같고 좌우 끝이 맞는다**, 두 입력창의 오른쪽 끝도 같다.
- ⚙는 그 블록 **밖** 오른쪽, 두 행 높이의 세로 중앙에 하나다. 설정은 입력과
  성격이 달라 시각적으로도 갈린다. 하단으로 내리지 않는다(쿠키 발견성).
- 남는 공간은 각 행에서 **입력창 하나만** 흡수한다. 버튼들은 고정 폭.

⚠️ 폐기한 게이트 — "1행 [VOD 추가] 끝 == 2행 [⚙] 끝". 텍스트 버튼과
아이콘 버튼, 즉 **다른 종류끼리** 끝을 맞춘 것이라 숫자는 통과했지만 눈에
걸리는 것([VOD 추가]와 [경로 찾기]의 끝이 어긋남)은 전혀 재지 못했다 —
⚙가 2행 끝에 붙으면서 [경로 찾기]를 왼쓸으로 밀었는데 게이트는 통과했다.
**정렬 게이트는 같은 종류끼리 재야 한다.** 텍스트 버튼↔아이콘 버튼, 라벨↔
입력창처럼 성격이 다른 것의 끝을 맞추면 숫자는 통과해도 눈에는 어긋나 보인다.

폰트에 의존하지 않는 기하 관계만 잰다(끝선 일치·폭 동일·중앙·증가 방향).
⚠️ 창 폭 여러 값으로 잰다 — 이 부류 결함은 넓은 폭에서만 드러난다.
"""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import main as main_module
import theme
from application.mainWindow import VodDownloader

#: 창 최소폭(ui/mainWindow.py 기준 640 근처)·기본·아주 넓게.
WIDTHS = (640, 900, 1600)


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp):
    """실제 전역 QSS를 태운다. ⚠️ `scope="function"` 유지(macOS 종료 크래시 —
    `test_widget_theme.py`의 `_apply_dark_card_qss` 문서 참고)."""
    theme.set_color_scheme("dark")
    qapp.setStyle(theme.build_style())
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))


@pytest.fixture
def window(qapp):
    win = VodDownloader()
    yield win
    win.deleteLater()
    QApplication.processEvents()


def _at_width(win, width):
    win.resize(width, 700)
    win.show()
    QApplication.processEvents()


def _right(w) -> int:
    return w.x() + w.width()


def _in_header(win, w):
    """헤더 프레임 좌표계의 (top, bottom) — 서로 다른 부모에 있는 위젯을 한 좌표계로."""
    top = w.mapTo(win.headerFrame, w.rect().topLeft()).y()
    return top, top + w.height()


class TestStructure:
    def test_input_block_has_two_rows_and_settings_sits_outside(self, window):
        rows = window.headerRowsLayout
        assert rows.count() == 2
        assert rows.itemAt(0).layout() is window.inputBlockLayout, "첫 항목은 입력 블록(두 행)이어야 한다"
        assert rows.itemAt(1).widget() is window.settingButton, "⚙는 입력 블록 밖 오른쓸이어야 한다"
        assert rows.itemAt(1).alignment() & Qt.AlignmentFlag.AlignVCenter, "⚙는 세로 중앙 정렬이어야 한다"
        url = [window.urlRowLayout.itemAt(i).widget() for i in range(window.urlRowLayout.count())]
        path = [window.pathRowLayout.itemAt(i).widget() for i in range(window.pathRowLayout.count())]
        assert url == [window.urlInput, window.fetchButton]
        assert path == [window.downloadPathInput, window.downloadPathButton], "2행에 ⚙가 끼어 있으면 [경로 찾기]가 밀린다"

    def test_tab_order_visits_settings_after_find_path(self, window):
        """탭 순서: 경로 찾기 → ⚙ → 목록. 포커스 체인에는 포커스를 안 받는
        위젯(입력창의 지우기 버튼 등)도 끼어 있어, 포커스를 받는 다음
        위젯까지 건너 뛰어 잰다."""

        def next_focusable(widget):
            cursor = widget.nextInFocusChain()
            while cursor is not None and cursor is not widget:
                if cursor.focusPolicy() != Qt.FocusPolicy.NoFocus and cursor.isVisibleTo(window):
                    return cursor
                cursor = cursor.nextInFocusChain()
            return None

        window.show()
        QApplication.processEvents()
        assert next_focusable(window.downloadPathButton) is window.settingButton
        assert next_focusable(window.settingButton) is window.listView


class TestTextButtonsAlignAsOneColumn:
    """★ [VOD 추가]와 [경로 찾기] — 같은 종류(텍스트 버튼)끼리 왼쓸 끝·오른쪽
    끝·폭이 전부 같다. 하나라도 다르면 입력 블록이 사각형으로 읽히지 않는다."""

    @pytest.mark.parametrize("width", WIDTHS)
    def test_left_right_and_width_are_identical(self, window, width):
        _at_width(window, width)
        a, b = window.fetchButton, window.downloadPathButton
        assert (a.x(), _right(a), a.width()) == (b.x(), _right(b), b.width()), (
            f"폭 {width}px에서 [VOD 추가] (x={a.x()}, right={_right(a)}, w={a.width()}) ≠ "
            f"[경로 찾기] (x={b.x()}, right={_right(b)}, w={b.width()})"
        )

    @pytest.mark.parametrize("width", WIDTHS)
    def test_inputs_share_left_and_right_edges(self, window, width):
        _at_width(window, width)
        assert window.urlInput.x() == window.downloadPathInput.x()
        assert _right(window.urlInput) == _right(window.downloadPathInput)

    @pytest.mark.parametrize("width", WIDTHS)
    def test_settings_is_to_the_right_of_the_block(self, window, width):
        _at_width(window, width)
        assert window.settingButton.x() > _right(window.fetchButton)
        assert window.settingButton.x() > _right(window.downloadPathButton)


class TestSettingsIsVerticallyCentredOnTheTwoRows:
    @pytest.mark.parametrize("width", WIDTHS)
    def test_settings_centre_matches_the_block_centre(self, window, width):
        _at_width(window, width)
        top, _ = _in_header(window, window.urlInput)
        _, bottom = _in_header(window, window.downloadPathInput)
        block_centre = (top + bottom) / 2
        s_top, s_bottom = _in_header(window, window.settingButton)
        settings_centre = (s_top + s_bottom) / 2
        assert abs(settings_centre - block_centre) <= 1, (
            f"폭 {width}px에서 ⚙ 중심 {settings_centre} ≠ 두 행 중심 {block_centre}"
        )


class TestFreeSpaceGoesToTheInputsOnly:
    BUTTONS = ("fetchButton", "downloadPathButton", "settingButton")

    def test_buttons_keep_their_width_as_the_window_widens(self, window):
        widths = {}
        for width in WIDTHS:
            _at_width(window, width)
            widths[width] = {name: getattr(window, name).width() for name in self.BUTTONS}
        assert widths[WIDTHS[0]] == widths[WIDTHS[1]] == widths[WIDTHS[2]], (
            f"버튼 폭이 창 폭을 따라 변한다: {widths} — 남는 공간은 입력창만 흡수해야 한다"
        )

    def test_inputs_grow_with_the_window(self, window):
        url, path = [], []
        for width in WIDTHS:
            _at_width(window, width)
            url.append(window.urlInput.width())
            path.append(window.downloadPathInput.width())
        assert url[0] < url[1] < url[2], f"URL 입력창이 폭을 따라 늘지 않는다: {url}"
        assert path[0] < path[1] < path[2], f"경로 입력창이 폭을 따라 늘지 않는다: {path}"
