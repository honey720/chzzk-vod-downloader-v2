"""카드 조작 표면 전수 감사 게이트 (#244 3행 정리 P-5) — 상태 × 표면.

판정 기준은 하나다: **지금 눌러서 실제로 되는가.** 되지 않는데 "조작할 수 있다"고
말하는 표면(호버 강조·커서 모양·열린 입력창·클릭 반응)이 있으면 결함이고, 되는데
표면이 없어도 결함이다. 오너가 실기에서 하나씩 찾은 결함 셋(제목 호버·경로 커서·
편집 중 다운로드 시작)은 같은 뿌리 — 상태가 바뀌었는데 조작 표면이 안 따라간다.

감사 표 (2026-09-03, 대기 / 진행 / 일시정지 / 완료 / 실패):

| 표면                        | 대기 | 진행·일시정지·완료·실패 | 감사 시점 코드                           |
|-----------------------------|------|--------------------------|------------------------------------------|
| 제목 — 호버 강조            | ✅   | ❌                       | ✅ [P-4] `#titleLabel[editable]:hover`    |
| 제목 — 커서(IBeam)          | ✅   | ❌(화살표)               | ✅ [P-4] `_applyTitleEditability`         |
| 제목 — 클릭 → 편집          | ✅   | ❌                       | ✅ `startTitleEditing` 대기 검사          |
| 제목 — 편집 중 상태 전이    | —    | 입력창 닫고 값 되돌림    | ❌ **결함 3**: 입력창 남고 확정 시 라벨만 바뀜(파일은 옛 이름) |
| 제목 — 툴팁(전문)           | 정보 | 정보                     | ✅ (조작 표면 아님)                       |
| 경로 라벨 — 커서(손가락)    | ✅   | ❌(화살표)               | ❌ **결함 2**: ui 파일이 항상 손가락       |
| 경로 라벨 — 호버 강조       | —    | —                        | ✅ QSS에 호버 규칙 없음                   |
| 경로 라벨 — 클릭 → 폴더선택 | ✅   | ❌                       | ✅ `choosePath` 대기 검사                 |
| 경로 아이콘 — 호버 강조     | ✅   | ❌                       | ❌ **미발견 칸**: `[role="icon"]:hover` 배경·도형 밝아짐이 상태 무관 |
| 경로 아이콘 — 클릭 → 폴더선택 | ✅ | ❌                       | ✅ 같은 `choosePath`                      |
| 경로 — 대화상자 중 상태 전이 | —   | 고른 값을 버림           | ❌ **미발견 칸**: 대화상자가 닫힌 뒤 상태를 다시 안 봄 |
| 경로 — 툴팁(전문)           | 정보 | 정보                     | ✅                                        |
| 해상도 pill — 호버/클릭     | ✅   | (없음)                   | ✅ 대기에만 보임(`_layoutRowThree`), 펼침도 풀림 |
| 1행 조작(⏸/▶/📁/↻/✕)        | 상태별 가시성으로 정리됨 (tests/unit/test_card_state_matrix.py) | ✅ (3행 밖 — 이 PR 범위 아님) |

경로 변경은 **대기에서만**(오너 확정 — 진행·일시정지는 이미 그 경로에 쓰고 있고, 완료는
저장됐고, **실패도 닫는다**: 복구 수단은 재시도이고 §7.1 시작 전 쓰기 검사가 경로 문제를
걸러내므로 실패 카드까지 온 것은 대개 네트워크·서버 쪽이다. 예외 없는 규칙이 낫다).
편집 중 상태가 바뀌면 **편집을 취소하고 되돌린다** — 시작 순간에 편집값을 확정하는 안은
쓰지 않는다(파일명은 시작 시점에 이미 결정되므로 늦고, 엔터를 안 눌렀으니 의도도 확정이
아니다).

호버는 실제 커서를 옮기지 않고 `QWindow` 합성 이벤트로 보낸다. 표시 문자열은 `shown()`을
거치고 커서 모양은 `cursor()`로 직접 본다.
"""

import os

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

import main as main_module
import app.theme as theme
from app.viewmodels.data import ContentItem
from app.widgets.widget import ContentItemWidget
from core.models.download_state import DownloadState
from core.utils.paths import build_output_path
from tests.unit.card_helpers import drop_new_top_levels, hold_style, shown, snapshot_top_levels

STATES = (DownloadState.WAITING, DownloadState.RUNNING, DownloadState.PAUSED,
          DownloadState.FINISHED, DownloadState.FAILED)
IDS = [s.name for s in STATES]

OTHER_PATH = "D:/vod/archive/2026/summer/finals/T1-vs-GEN-full-set-highlights-and-interviews"


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp):
    """실제 전역 QSS(호버 규칙 포함)를 태운다 — scope=function 유지(test_widget_theme 참고)."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))


@pytest.fixture(autouse=True)
def _drop_windows(qapp):
    """이 파일의 테스트가 띄운 창을 테스트 끝에 확실히 파괴한다(card_helpers.drop_new_top_levels)."""
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

    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())
    monkeypatch.setattr("app.widgets.widget._global_download_path", "C:/dl")


def _pump():
    for _ in range(3):
        QApplication.processEvents()


def make_card(state: DownloadState, path: str = OTHER_PATH, width: int = 900):
    """상태별 카드를 보이는 최상위 창 안에 놓는다 — 호버는 창(QWindow)이 받아야 자식까지 간다.

    경로는 전역과 다르게 둔다 — 대기 밖 상태에서도 경로 표면(라벨/아이콘)이 보이게.
    """
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {"title": "제목", "category": "", "channelName": "채널", "createdDate": "", "duration": 3600},
        [["1080", "u1"], ["720", "u2"]], None, "", path, "video", None,
    )
    item.downloadState = DownloadState.WAITING
    item.total_size = "595.34 MB"
    widget = ContentItemWidget(item, 0)
    widget.addRepresentationButtons()
    item.downloadState = state
    item.download_progress = 42
    item.download_speed = "8.2 MB/s"
    item.download_remain_time = "00:12:34"
    item.download_size = 624_000_000
    item.stateMessage = "Failed to save file" if state == DownloadState.FAILED else ""
    widget.setData(item, 0)
    window = QWidget()
    column = QVBoxLayout(window)
    column.setContentsMargins(0, 0, 0, 0)
    column.addWidget(widget)
    column.addStretch(1)
    window.resize(width, widget.sizeHint().height() + 40)
    window.show()
    QTest.qWaitForWindowExposed(window)
    _pump()
    return window, widget


def resize_window(window, widget, width):
    window.resize(width, widget.sizeHint().height() + 40)
    _pump()
    assert widget.width() == width, f"요청 폭 {width}px인데 실제 {widget.width()}px — 클램프"


def hover(qtbot, window, target, on: bool) -> None:
    """`target` 위(on) 또는 창의 빈 바닥(off)으로 합성 이동. 두 번 보내 이동량 0 함정을 피한다."""
    if on:
        pos = target.mapTo(window, target.rect().center())
    else:
        pos = QPoint(window.width() - 2, window.height() - 2)
    QTest.mouseMove(window.windowHandle(), pos + QPoint(1, 1))
    QTest.mouseMove(window.windowHandle(), pos)
    qtbot.waitUntil(lambda: target.underMouse() == on, timeout=2000)


def pixels(target):
    shown(target)
    return target.grab().toImage()


def highlights_on_hover(qtbot, window, target) -> bool:
    hover(qtbot, window, target, on=False)
    idle = pixels(target)
    hover(qtbot, window, target, on=True)
    hovered = pixels(target)
    hover(qtbot, window, target, on=False)
    return hovered != idle


def _clickable(state: DownloadState) -> bool:
    return state == DownloadState.WAITING


def _record_dialog(monkeypatch, result="", during=None):
    """폴더 선택 대화상자를 가로챈다 — 호출 여부·인자를 기록하고 `result`를 돌려준다."""
    calls = []

    def fake(parent, caption, start, *a, **k):
        calls.append(start)
        if during is not None:
            during()
        return result

    from app.widgets import widget as widget_mod

    monkeypatch.setattr(widget_mod.QFileDialog, "getExistingDirectory", staticmethod(fake))
    return calls


# ============================ 제목 ============================


class TestTitleSurface:
    @pytest.mark.parametrize("state", STATES, ids=IDS)
    def test_hover_highlight_matches_clickability(self, qapp, qtbot, state):
        window, widget = make_card(state)
        qtbot.addWidget(window)
        assert highlights_on_hover(qtbot, window, widget.titleLabel) == _clickable(state), (
            f"{state.name}: 제목 호버 강조가 클릭 가능 여부와 다르다"
        )

    @pytest.mark.parametrize("state", STATES, ids=IDS)
    def test_cursor_matches_clickability(self, qapp, qtbot, state):
        window, widget = make_card(state)
        qtbot.addWidget(window)
        expected = Qt.CursorShape.IBeamCursor if _clickable(state) else Qt.CursorShape.ArrowCursor
        assert widget.titleLabel.cursor().shape() == expected, f"{state.name}: 제목 커서가 클릭 가능 여부와 다르다"

    @pytest.mark.parametrize("state", STATES, ids=IDS)
    def test_click_opens_the_editor_only_when_clickable(self, qapp, qtbot, state):
        window, widget = make_card(state)
        qtbot.addWidget(window)
        shown(widget.titleLabel)
        QTest.mouseClick(widget.titleLabel, Qt.MouseButton.LeftButton)
        _pump()
        assert widget.titleEdit.isVisible() == _clickable(state), f"{state.name}: 제목 클릭의 편집창 표시가 상태와 다르다"


# ============================ 경로 ============================


class TestPathLabelSurface:
    @pytest.mark.parametrize("state", STATES, ids=IDS)
    def test_cursor_matches_clickability(self, qapp, qtbot, state):
        window, widget = make_card(state)
        qtbot.addWidget(window)
        assert widget.directoryLabel.isVisible(), "전제: 전역과 다른 경로라 라벨이 보인다"
        expected = Qt.CursorShape.PointingHandCursor if _clickable(state) else Qt.CursorShape.ArrowCursor
        assert widget.directoryLabel.cursor().shape() == expected, (
            f"{state.name}: 경로 라벨 커서가 클릭 가능 여부와 다르다 — 결함 2"
        )

    @pytest.mark.parametrize("state", STATES, ids=IDS)
    def test_label_highlights_on_hover_only_when_clickable(self, qapp, qtbot, state):
        """경로 라벨은 누를 수 있는 상태(대기)에서만 호버에 색이 바뀌어야 한다 (#309).

        종전 단언은 "어느 상태에서도 호버에 반응하지 않는다"였다. 대기 카드의 구간 요약 · 제목과
        같이 누를 수 있을 때만 알리기로 정해(오너 결정) 단언을 클릭 가능 여부로 바꿨다.
        """
        window, widget = make_card(state)
        qtbot.addWidget(window)
        assert highlights_on_hover(qtbot, window, widget.directoryLabel) == _clickable(state), (
            f"{state.name}: 경로 라벨의 호버 강조가 클릭 가능 여부와 다르다"
        )

    @pytest.mark.parametrize("state", STATES, ids=IDS)
    def test_click_opens_the_folder_dialog_only_when_clickable(self, qapp, qtbot, monkeypatch, state):
        window, widget = make_card(state)
        qtbot.addWidget(window)
        calls = _record_dialog(monkeypatch)
        shown(widget.directoryLabel)
        QTest.mouseClick(widget.directoryLabel, Qt.MouseButton.LeftButton)
        _pump()
        assert (len(calls) == 1) == _clickable(state), f"{state.name}: 경로 클릭의 폴더 선택 호출이 상태와 다르다"


class TestPathButtonSurface:
    """1행의 저장 위치 버튼 (#309) — 아래 줄에 남던 경로 아이콘의 자리를 이어받았다.

    누를 수 없는 상태에서는 꺼 두지 않고 아예 숨긴다(1행의 다른 조작 버튼과 같다).
    """

    def _card(self, qtbot, state):
        window, widget = make_card(state)
        qtbot.addWidget(window)
        return window, widget

    @pytest.mark.parametrize("state", STATES, ids=IDS)
    def test_button_is_there_only_when_the_path_can_be_changed(self, qapp, qtbot, state):
        """저장 위치 버튼은 저장 위치를 바꿀 수 있는 상태(대기)에서만 보여야 하고, 숨을 때는 꺼 두는 것이 아니라 없어야 한다.

        상태마다 카드 하나 -> 보임 == (대기), 보일 때는 켜져 있다
        """
        _window, widget = self._card(qtbot, state)
        assert widget.pathButton.isVisible() == _clickable(state), (
            f"{state.name}: 저장 위치 버튼의 가시성이 누를 수 있는지와 다르다"
        )
        assert widget.pathButton.isEnabled(), f"{state.name}: 버튼을 숨기지 않고 꺼 두었다"

    def test_hover_highlights_the_button_like_the_other_row_one_buttons(self, qapp, qtbot):
        window, widget = self._card(qtbot, DownloadState.WAITING)
        assert highlights_on_hover(qtbot, window, widget.pathButton), (
            "대기 카드의 저장 위치 버튼이 호버에 반응하지 않는다"
        )

    def test_cursor_is_the_arrow_like_every_other_button(self, qapp, qtbot):
        """버튼의 조작 표면은 호버 배경(위 게이트)이다 — 1행 조작 버튼과 같이 커서는 화살표."""
        _window, widget = self._card(qtbot, DownloadState.WAITING)
        assert widget.pathButton.cursor().shape() == Qt.CursorShape.ArrowCursor

    def test_click_opens_the_folder_dialog(self, qapp, qtbot, monkeypatch):
        """저장 위치 버튼을 누르면 경로 글자를 눌렀을 때와 같은 폴더 선택이 열려야 한다.

        대기 카드에서 버튼을 클릭 -> 폴더 선택 대화상자 호출 1회
        """
        _window, widget = self._card(qtbot, DownloadState.WAITING)
        calls = _record_dialog(monkeypatch)
        QTest.mouseClick(widget.pathButton, Qt.MouseButton.LeftButton)
        _pump()
        assert len(calls) == 1

    @pytest.mark.parametrize("state", STATES, ids=IDS)
    def test_choose_path_does_nothing_outside_waiting(self, qapp, qtbot, monkeypatch, state):
        """폴더 선택은 버튼이 불러도 대기가 아닌 상태에서는 열리지 않아야 한다.

        상태마다 choosePath()를 직접 부름(버튼이 부르는 모양 — 이벤트 없이)
        -> 대화상자 호출 수 == (대기면 1, 아니면 0)
        """
        _window, widget = self._card(qtbot, state)
        calls = _record_dialog(monkeypatch)
        widget.choosePath()
        assert (len(calls) == 1) == _clickable(state)

    def test_tooltip_says_what_the_button_does_and_carries_the_full_path(self, qapp, qtbot):
        """툴팁은 무엇을 하는 버튼인지와 지금의 경로 전문을 준다."""
        _window, widget = self._card(qtbot, DownloadState.WAITING)
        assert widget.pathButton.toolTip() == f"Change save location\n{OTHER_PATH}"


class TestSectionEditButtonSurface:
    """1행의 구간 편집 버튼 (#309) — 아래 줄의 구간 요약 글자를 누르는 것과 같은 일을 한다."""

    def _card(self, qtbot, state, content_type: str = "video"):
        window, widget = make_card(state)
        qtbot.addWidget(window)
        if content_type != "video":
            widget.item.content_type = content_type
            widget.setData(widget.item, 0)
            _pump()
        return window, widget

    @pytest.mark.parametrize("state", STATES, ids=IDS)
    def test_button_is_there_only_when_the_sections_can_be_edited(self, qapp, qtbot, state):
        """구간 편집 버튼은 편집 창을 열 수 있는 상태(대기)에서만 보여야 하고, 숨을 때는 꺼 두는 것이 아니라 없어야 한다.

        상태마다 구간 기능이 있는 타입(video)의 카드 하나 -> 보임 == (대기), 보일 때는 켜져 있다
        """
        _window, widget = self._card(qtbot, state)
        assert widget.sectionEditButton.isVisible() == (state == DownloadState.WAITING), (
            f"{state.name}: 구간 편집 버튼의 가시성이 편집 창을 열 수 있는지와 다르다"
        )
        assert widget.sectionEditButton.isEnabled(), f"{state.name}: 버튼을 숨기지 않고 꺼 두었다"

    def test_button_is_hidden_on_a_waiting_card_whose_type_has_no_sections(self, qapp, qtbot):
        """구간 기능이 없는 타입의 카드에는 대기 상태여도 구간 편집 버튼이 없어야 한다.

        대기 카드의 타입을 구간 기능이 없는 것(clip)으로 둠 -> 구간 편집 버튼 숨김, 저장 위치 버튼은 보임
        """
        _window, widget = self._card(qtbot, DownloadState.WAITING, content_type="clip")
        assert not widget.sectionEditButton.isVisible()
        assert widget.pathButton.isVisible()

    def test_click_asks_for_the_editor_like_a_click_on_the_summary_text(
        self, qapp, qtbot, monkeypatch
    ):
        """구간 편집 버튼을 누르면 구간 요약 글자를 눌렀을 때와 같은 요청(sectionEditRequest)이 한 번 나와야 하고, 폴더 선택은 열리지 않아야 한다.

        대기 카드에서 버튼을 클릭, 이어서 구간 요약 글자를 클릭(폴더 선택 대화상자는 대역)
        -> 요청이 각각 1회씩(모두 2회), 폴더 선택 호출 0회
        """
        _window, widget = self._card(qtbot, DownloadState.WAITING)
        dialogs = _record_dialog(monkeypatch)  # 잘못 이어지면 실제 대화상자가 떠서 끝나지 않는다
        asked = []
        widget.sectionEditRequest.connect(lambda: asked.append(1))

        QTest.mouseClick(widget.sectionEditButton, Qt.MouseButton.LeftButton)
        _pump()
        after_button = len(asked)
        QTest.mouseClick(widget.fileSizeLabel, Qt.MouseButton.LeftButton)
        _pump()

        assert (after_button, len(asked)) == (1, 2)
        assert dialogs == []

    def test_tooltip_says_what_the_button_does(self, qapp, qtbot):
        _window, widget = self._card(qtbot, DownloadState.WAITING)
        assert widget.sectionEditButton.toolTip() == "Edit sections"

    def test_row_three_has_no_path_icon_any_more(self, qapp, qtbot):
        """아래 줄에 남던 경로 아이콘은 없어야 한다 — 그 일은 1행의 저장 위치 버튼이 한다."""
        _window, widget = self._card(qtbot, DownloadState.WAITING)
        assert not hasattr(widget, "pathIconButton")
        assert widget.findChild(QWidget, "pathIconButton") is None


class TestRowOneAtTheNarrowestCard:
    def test_waiting_card_keeps_its_three_buttons_apart_from_the_channel_name(self, qapp, qtbot):
        """1행의 버튼이 가장 많은 대기 카드는 가장 좁은 폭에서도 채널 이름 · 버튼이 겹치거나 잘리지 않아야 한다.

        대기 카드(구간 편집 · 저장 위치 · 삭제 세 버튼)를 카드의 최소 폭으로 줄임
        -> 왼쪽부터 채널 이름 < 구간 편집 < 저장 위치 < 삭제 순으로 서로 겹치지 않고, 모두 카드 안에 있다
        """
        window, widget = make_card(DownloadState.WAITING)
        qtbot.addWidget(window)
        resize_window(window, widget, widget.minimumSizeHint().width())
        row = [widget.channelNameLabel, widget.sectionEditButton, widget.pathButton, widget.deleteButton]
        assert all(part.isVisible() for part in row), "전제: 네 가지가 모두 보인다"

        spans = [(part.mapTo(widget, part.rect().topLeft()).x(), part.width()) for part in row]

        for (left, width), (next_left, _next_width) in zip(spans, spans[1:]):
            assert left + width <= next_left, f"1행에서 겹친다: {spans}"
        assert spans[0][0] >= 0 and spans[-1][0] + spans[-1][1] <= widget.width(), f"카드 밖으로 나갔다: {spans}"
        assert all(width > 0 for _left, width in spans)


class TestPathDialogDuringTransition:
    def test_a_folder_picked_after_the_download_started_is_discarded(self, qapp, qtbot, monkeypatch):
        """대화상자가 열린 사이 다운로드가 시작되면 고른 폴더는 버린다 — 이미 그 경로에 쓰고 있다."""
        window, widget = make_card(DownloadState.WAITING)
        qtbot.addWidget(window)

        def start_download():
            widget.item.downloadState = DownloadState.RUNNING
            widget.setData(widget.item, 0)

        calls = _record_dialog(monkeypatch, result="E:/picked", during=start_download)
        widget.choosePath()
        _pump()
        assert calls == [OTHER_PATH]
        assert widget.item.download_path == OTHER_PATH, "시작된 뒤에 고른 폴더가 적용됐다 — 부분 파일은 옛 경로에 있다"


# ============================ 편집 중 상태 전이 ============================


class TestEditingIsCancelledOnStateChange:
    """편집 중에 상태가 바뀌면 입력창을 닫고 기존 값으로 되돌린다 — 그리고 실제 저장 파일명이 라벨과 같다."""

    def _editing_card(self, qtbot, typed="NewName"):
        window, widget = make_card(DownloadState.WAITING, path="C:/dl")
        qtbot.addWidget(window)
        emitted = []
        widget.textChanged.connect(emitted.append)
        shown(widget.titleLabel)
        QTest.mouseClick(widget.titleLabel, Qt.MouseButton.LeftButton)
        _pump()
        assert widget.titleEdit.isVisible() and widget.isEditing, "전제: 편집 중"
        widget.titleEdit.selectAll()
        # 엔터는 누르지 않는다 — 의도가 확정되지 않았다. 글자는 ASCII — QTest.keyClicks에
        # 한글을 넣으면 offscreen에서 프로세스가 죽는다(실측, 키 매핑 없음)
        QTest.keyClicks(widget.titleEdit, typed)
        assert widget.titleEdit.text() == typed
        return window, widget, emitted

    def _start(self, widget, tmp_path):
        """다운로드 시작 — 파일명은 이 순간의 제목으로 확정된다(app/viewmodels/content_viewmodel.py::onDownload)."""
        item = widget.item
        item.output_path = build_output_path(str(tmp_path), item.title, "1080")
        item.downloadState = DownloadState.RUNNING
        widget.setData(item, 0)
        _pump()

    @pytest.mark.parametrize("state", STATES[1:], ids=IDS[1:])
    def test_editor_closes_and_the_title_reverts(self, qapp, qtbot, state):
        window, widget, emitted = self._editing_card(qtbot)
        widget.item.downloadState = state
        widget.setData(widget.item, 0)
        _pump()
        assert not widget.titleEdit.isVisible() and not widget.isEditing, f"{state.name}: 입력창이 남아 있다 — 결함 3"
        assert shown(widget.titleLabel) == "제목" and widget.item.title == "제목", "편집 중이던 값이 확정됐다"
        assert emitted == [], "확정하지 않은 편집이 모델로 나갔다"

    def test_saved_filename_matches_the_label_after_the_revert(self, qapp, qtbot, tmp_path):
        """결함 3의 핵심 — 라벨만 보는 게이트는 못 잡는다. 시작 시점에 확정된 파일명과 라벨이 같아야 한다."""
        window, widget, emitted = self._editing_card(qtbot)
        self._start(widget, tmp_path)
        # 시작 뒤 포커스가 움직여도(창이 닫히든 다른 곳을 누르든) 편집이 확정되면 안 된다
        widget.titleEdit.clearFocus()
        window.setFocus()
        _pump()
        stem = os.path.splitext(os.path.basename(widget.item.output_path))[0]
        assert stem.startswith(widget.item.title) and shown(widget.titleLabel) == widget.item.title, (
            f"저장 파일명 {stem!r} ≠ 라벨 {shown(widget.titleLabel)!r} — 라벨만 바뀌고 파일은 옛 이름으로 저장된다"
        )
        assert "NewName" not in stem and emitted == []

    def test_finishing_the_edit_after_the_start_does_not_rename(self, qapp, qtbot, tmp_path):
        """시작된 뒤 어떤 경로로든 finishTitleEditing이 불려도 제목은 바뀌지 않는다."""
        window, widget, emitted = self._editing_card(qtbot)
        self._start(widget, tmp_path)
        widget.titleEdit.setText("나중에 바꾼 이름")
        widget.finishTitleEditing()
        _pump()
        assert widget.item.title == "제목" and shown(widget.titleLabel) == "제목" and emitted == []

    def test_editing_still_commits_normally_while_waiting(self, qapp, qtbot):
        """대조군 — 대기에서는 편집이 정상 확정된다(취소 규칙이 정상 경로를 망가뜨리지 않는다)."""
        window, widget, emitted = self._editing_card(qtbot)
        QTest.keyClick(widget.titleEdit, Qt.Key.Key_Return)
        _pump()
        assert not widget.titleEdit.isVisible()
        assert shown(widget.titleLabel) == "NewName" and widget.item.title == "NewName" and emitted == ["NewName"]
