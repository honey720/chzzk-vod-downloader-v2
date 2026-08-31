"""상단(헤더) 두 행의 가로 배치 게이트 (#245 — 설정 버튼 위치 확정).

오너 확정 구조:

    [치지직 URL 입력                  ][VOD 추가 ]
    [C:\\Users\\...\\cvdv2            ✕][경로 찾기][⚙]
    해상도 가져오기 성공.

- 1행 [VOD 추가]의 오른쓸 끝 == 2행 [⚙]의 오른쓸 끝 — 우측 끝선은 하나다.
  ⚙가 1행에 붙어 있던 동안은 [VOD 추가]가 안쪽으로 밀려 두 행의 끝선이
  어긋났다.
- 남는 공간은 각 행에서 **입력창 하나만** 흡수한다. 버튼들은 창 폭이
  바뀌어도 자기 폭을 지킨다.
- ⚙는 하단으로 내리지 않는다(설정 안의 쿠키 발견성) — 2행에 있어야 한다.

폰트에 의존하지 않는 기하 관계만 잰다(끝선 일치·폭 불변·증가 방향).
⚠️ 창 폭 여러 값으로 잰다 — 이 부류 결함은 넓은 폭에서만 드러난다.
"""

import pytest
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


class TestSettingsButtonLivesOnThePathRow:
    def test_settings_button_is_on_the_path_row_after_find_path(self, window):
        row = window.pathRowLayout
        widgets = [row.itemAt(i).widget() for i in range(row.count())]
        assert widgets == [window.downloadPathInput, window.downloadPathButton, window.settingButton], (
            f"2행 구성이 [경로 입력][경로 찾기][⚙]이 아니다: {[w.objectName() for w in widgets]}"
        )

    def test_url_row_ends_with_add_vod(self, window):
        row = window.urlRowLayout
        widgets = [row.itemAt(i).widget() for i in range(row.count())]
        assert widgets == [window.urlInput, window.fetchButton], (
            f"1행 구성이 [URL 입력][VOD 추가]가 아니다: {[w.objectName() for w in widgets]}"
        )

    def test_tab_order_visits_settings_after_find_path(self, window):
        """탭 순서: 경로 찾기 → ⚙ → 목록. 포커스 체인에는 포커스를 안 받는
        위젯(입력창의 지우기 버튼 등)도 끼어 있어, 포커스를 받는 다음
        위젯까지 건너 뛰어 잰다."""
        from PySide6.QtCore import Qt

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


class TestOneRightEdge:
    @pytest.mark.parametrize("width", WIDTHS)
    def test_add_vod_and_settings_share_the_right_edge(self, window, width):
        _at_width(window, width)
        assert _right(window.fetchButton) == _right(window.settingButton), (
            f"폭 {width}px에서 [VOD 추가] 오른쓸 끝({_right(window.fetchButton)})과 "
            f"[⚙] 오른쓸 끝({_right(window.settingButton)})이 다르다 — 우측 끝선은 하나여야 한다"
        )

    @pytest.mark.parametrize("width", WIDTHS)
    def test_both_inputs_start_at_the_same_left_edge(self, window, width):
        _at_width(window, width)
        assert window.urlInput.x() == window.downloadPathInput.x()


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
