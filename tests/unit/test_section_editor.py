"""구간 편집 창 — 카드에서 열기 · 시간 입력 · 검증 · 카드에 쓰기 · 편집 중 건너뛰기 · 해상도 변경 (#309).

실제 배선을 탄다: 카드의 구간 요약(재생 시간 자리) 클릭 → 위젯 시그널 → 뷰 릴레이 →
`VodDownloader.onCardSectionEdit` → 편집 창 ↔ 뷰모델 → `ContentItem.selections` → 카드 표시.
전역 다운로드는 버튼 클릭 → `onDownloadPause` → `downloadItem` → `startDownload`를 지난다.

네트워크는 타지 않는다. 프레임률 · 영상 길이 조회(`app.section_basis.probe_section_basis`)는
대역으로 바꾸고, 엔진 자리는 `DownloadViewModel.start` 기록 대역 또는 서비스 대역이 맡는다.
config는 conftest가 격리한다.

기대값은 손으로 계산한 값이다 — 제품의 해석 함수로 만들지 않는다.
"""

import os
import threading
from fractions import Fraction

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

import app.section_basis as section_basis
import app.theme as theme
import main as main_module
from app.download_logger import DownloadLogger
from app.section_basis import SectionBasis
from app.viewmodels.data import ContentItem
from app.views import mainWindow as mw_mod
from app.views.mainWindow import VodDownloader
from core.api.representations import StreamEntry
from core.models.download_state import DownloadState
from core.models.plan import TimeRange
from core.utils.paths import release_output_paths
from app.viewmodels.section_edit_viewmodel import refit_selections
from tests.unit.section_input import leave_time, type_clock, type_frame, type_time
from tests.unit.card_helpers import drop_new_top_levels, hold_style, shown, snapshot_top_levels

HOUR = 3600.0  # 대역이 돌려주는 영상 길이(초) — 60fps · 30fps 모두에서 프레임 경계다


@pytest.fixture(autouse=True)
def _apply_production_qss(qapp):
    """실제 전역 QSS·스타일을 태운다. ⚠️ function scope 유지."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))  # 참조 보관 — 이중 해제 우회 (#243)
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))


@pytest.fixture(autouse=True)
def _destroy_windows():
    """테스트가 만든 최상위 창은 숨긴 채 두지 않고 파괴한다."""
    before = snapshot_top_levels()
    yield
    drop_new_top_levels(before)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """카드의 크기 조회가 네트워크를 타지 않게 막고, 다운로드 로그 파일을 만들지 않는다."""

    class _FailingSession:
        def head(self, *a, **k):
            raise RuntimeError("network disabled in tests")

        def get(self, *a, **k):
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())
    monkeypatch.setattr(DownloadLogger, "_setup_logging", lambda self: None)


@pytest.fixture
def basis(monkeypatch):
    """프레임률 · 영상 길이 조회 대역 — 기본 60fps · 1시간.

    해상도마다 다른 값을 돌려주거나(by_resolution) 실패시키고(fail_resolutions), 조회가 끝나는
    때를 테스트가 정할 수 있다(gate — 모든 조회, gates — 해상도별).
    """

    class _Basis:
        fps = Fraction(60)
        duration = HOUR
        fail = False
        calls: list = []  # 조회에 넘어온 것(편집 창은 카드 데이터, 다시 맞추기는 그 순간의 사본)
        returned: list = []  # 조회가 끝난 해상도 — 끝난 순서대로
        by_resolution: dict = {}  # 해상도 → (프레임률, 길이)
        fail_resolutions: set = set()
        gate = threading.Event()  # 내려 두면 모든 조회가 기다린다
        gates: dict = {}  # 해상도 → Event. 내려 두면 그 해상도의 조회가 기다린다

    def probe(item):
        _Basis.calls.append(item)
        resolution = getattr(item, "resolution", None)
        assert _Basis.gate.wait(5), "조회 대역의 문이 열리지 않았다"
        if resolution in _Basis.gates:
            assert _Basis.gates[resolution].wait(5), "조회 대역의 해상도별 문이 열리지 않았다"
        try:
            if _Basis.fail or resolution in _Basis.fail_resolutions:
                raise RuntimeError("조회 실패(대역)")
            fps, duration = _Basis.by_resolution.get(resolution, (_Basis.fps, _Basis.duration))
            return SectionBasis(fps=fps, duration=duration)
        finally:
            _Basis.returned.append(resolution)

    _Basis.calls, _Basis.returned = [], []
    _Basis.by_resolution, _Basis.fail_resolutions, _Basis.gates = {}, set(), {}
    _Basis.gate.set()
    monkeypatch.setattr(section_basis, "probe_section_basis", probe)
    return _Basis


@pytest.fixture
def started(monkeypatch):
    """`DownloadViewModel.start` 기록 대역 — 시작된 카드를 순서대로 남긴다."""
    calls: list = []
    monkeypatch.setattr(mw_mod.DownloadViewModel, "start", lambda self, item: calls.append(item))
    monkeypatch.setattr(mw_mod.QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(mw_mod.QMessageBox, "information", lambda *a, **k: None)
    return calls


def _pump():
    for _ in range(3):
        QApplication.processEvents()


def _make_item(download_path: str, title: str = "제목", content_type: str = "video") -> ContentItem:
    """대기 카드 하나 — 1080p 60fps와 480p 30fps, 저장 경로는 실존하는 임시 폴더."""
    item = ContentItem(
        f"https://chzzk.naver.com/video/{title}",
        {
            "title": title,
            "category": "",
            "channelName": "채널",
            "createdDate": "",
            "duration": int(HOUR),
        },
        [StreamEntry(1080, "u1", frame_rate=60.0), StreamEntry(480, "u2", frame_rate=30.0)],
        None,
        "",
        download_path,
        content_type,
        None,
    )
    item.total_size = "595.34 MB"
    return item


def open_window(tmp_path, *items: ContentItem) -> VodDownloader:
    """실제 메인 창에 카드를 넣는다."""
    win = VodDownloader()
    win.resize(1000, win.height())
    win.show()
    QTest.qWaitForWindowExposed(win)
    for item in items:
        win.contentManager.model.addItem(item)
    _pump()
    return win


def click_summary(win: VodDownloader, item: ContentItem) -> None:
    """카드의 구간 요약 자리(재생 시간 · 파일 크기)를 실제로 누른다."""
    widget = win.listView.widgetFor(item)
    shown(widget.fileSizeLabel)
    QTest.mouseClick(widget.fileSizeLabel, Qt.MouseButton.LeftButton)
    _pump()


def open_editor(qtbot, win: VodDownloader, item: ContentItem):
    """카드를 눌러 편집 창을 열고 조회가 끝나기를 기다린다."""
    click_summary(win, item)
    dialog = win._sectionDialog
    assert dialog is not None, "구간 요약을 눌렀는데 편집 창이 열리지 않았다"
    qtbot.waitUntil(lambda: dialog.viewModel().state != "loading", timeout=3000)
    _pump()
    return dialog


def type_into(edit, text: str) -> None:
    """시각(시분초 칸 + 프레임 칸)을 비우고 키 입력으로 값을 넣는다 — 칸을 떠나지 않는다.

    text는 ``HH:MM:SS:FF`` 꼴이다(콜론은 읽기 좋으라고 적은 것 — 칸은 숫자만 받는다). 끝 두
    자리를 프레임 칸에, 그 앞을 시분초 칸에 친다(tests/unit/section_input.py).
    """
    type_time(edit, text)


def leave(edit) -> None:
    """그 시각의 편집을 끝낸다 — 두 칸을 모두 떠날 때 스스로 하는 일(`commit`)이다."""
    leave_time(edit)


def set_rows(dialog, rows: list[tuple[str, str]]) -> None:
    """편집 창의 행을 주어진 (시작, 끝) 타임코드로 만든다 — 추가 버튼과 키 입력으로, 칸마다 떠난다."""
    while len(dialog._rows) < len(rows):
        dialog.addButton.click()
        _pump()
    for index, (start, end) in enumerate(rows):
        for edit, text in (
            (dialog._rows[index].startEdit, start),
            (dialog._rows[index].endEdit, end),
        ):
            type_into(edit, text)
            leave(edit)


def press_ok(dialog) -> None:
    dialog.okButton.click()
    _pump()


def press_cancel(dialog) -> None:
    dialog.cancelButton.click()
    _pump()


def summary_text(win: VodDownloader, item: ContentItem) -> str:
    return shown(win.listView.widgetFor(item).fileSizeLabel)


def notice_of(win: VodDownloader, item: ContentItem) -> str:
    """카드가 구간 요약 뒤에 붙이는 알림 — 폭이 모자라 떼인 것과 무관한 전문.

    알림은 3행에 들어갈 때만 요약 뒤에 붙는다. 들어가는 폭은 폰트마다 달라(로컬과 CI의
    offscreen 폰트가 다르다) 라벨의 글자로 재면 폰트에 기댄다. 카드가 만든 알림 전문을 읽고,
    라벨에는 요약이 보이고 그 뒤가 전문의 일부(전부 · 경고만 · 없음)인지만 본다.
    """
    widget = win.listView.widgetFor(item)
    notice = widget._sectionNotice()
    base = widget._sectionSummary(with_notice=False)
    text = summary_text(win, item)
    allowed = {
        base,
        f"{base} · {notice}",
        f"{base} · {widget._sectionNotice(warnings_only=True)}".removesuffix(" · "),
    }
    assert text in allowed, f"요약 자리의 글이 요약 · 알림의 조합이 아니다: {text!r}"
    return notice


# ================================================================ 열기


def test_clicking_the_summary_of_a_waiting_card_opens_the_editor_with_the_whole_video(
    qtbot, tmp_path, basis
):
    """대기 카드의 구간 요약을 누르면 편집 창이 열리고 빈 행 하나가 보여야 한다.

    60fps · 길이 3600초, 구간 없는 카드
    -> 조회 중에는 안내가 보이고 확인이 꺼져 있다
    -> 조회 뒤 행 1개 ("", ""), 확인 켜짐
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)

    basis.gate.clear()  # 조회를 붙잡아 둔다 — 조회 중 화면을 본다
    click_summary(win, item)
    dialog = win._sectionDialog
    assert dialog is not None and dialog.isVisible() and dialog.isModal()
    assert shown(dialog.statusLabel) == dialog.viewModel().loadingText()
    assert not dialog.okButton.isEnabled() and not dialog.scrollArea.isVisible()

    basis.gate.set()
    qtbot.waitUntil(lambda: dialog.viewModel().state == "ready", timeout=3000)
    _pump()
    assert basis.calls == [item]
    assert not dialog.statusLabel.isVisible()
    assert [(row.startEdit.text(), row.endEdit.text()) for row in dialog._rows] == [("", "")]
    assert dialog.okButton.isEnabled()


@pytest.mark.parametrize(
    "state", [DownloadState.RUNNING, DownloadState.PAUSED, DownloadState.FINISHED]
)
def test_clicking_the_summary_of_a_card_that_is_not_waiting_opens_nothing(tmp_path, basis, state):
    """대기가 아닌 카드의 구간 요약을 누르면 편집 창이 열리지 않아야 한다.

    상태가 RUNNING · PAUSED · FINISHED인 카드
    -> 편집 창 없음, 조회 0건, 손가락 커서 아님
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    item.downloadState = state
    win.contentManager.model.notifyChanged(item)
    _pump()

    click_summary(win, item)

    assert win._sectionDialog is None
    assert basis.calls == []
    label = win.listView.widgetFor(item).fileSizeLabel
    assert label.cursor().shape() == Qt.CursorShape.ArrowCursor
    assert label.property("editable") is False


def test_waiting_card_summary_looks_clickable(tmp_path, basis):
    """대기 카드의 구간 요약 자리는 누를 수 있는 모양이어야 한다.

    대기 상태의 VOD 카드
    -> 손가락 커서, editable 속성 참, 툴팁에 "Click to edit sections"
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    label = win.listView.widgetFor(item).fileSizeLabel

    shown(label)
    assert label.cursor().shape() == Qt.CursorShape.PointingHandCursor
    assert label.property("editable") is True
    assert "Click to edit sections" in label.toolTip()


def test_clicking_the_summary_of_a_clip_card_opens_nothing(tmp_path, basis):
    """클립 카드는 대기 상태여도 편집 창이 열리지 않아야 한다.

    content_type="clip"인 대기 카드
    -> 편집 창 없음, 조회 0건
    """
    item = _make_item(str(tmp_path), content_type="clip")
    win = open_window(tmp_path, item)

    click_summary(win, item)

    assert win._sectionDialog is None
    assert basis.calls == []


def test_failed_lookup_shows_the_reason_and_blocks_editing(qtbot, tmp_path, basis):
    """조회가 실패하면 실패 안내가 보이고 확인할 수 없어야 한다.

    조회 대역이 예외를 던짐
    -> 안내 문구 표시, 행 0개, 확인 꺼짐, 취소로 닫으면 구간은 그대로 빈 튜플
    """
    basis.fail = True
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)

    dialog = open_editor(qtbot, win, item)

    assert dialog.viewModel().state == "failed"
    assert shown(dialog.statusLabel) == dialog.viewModel().failureText()
    assert "조회 실패(대역)" not in dialog.statusLabel.text()  # 원시 예외 문자열을 올리지 않는다
    assert dialog._rows == [] and not dialog.scrollArea.isVisible()
    assert not dialog.okButton.isEnabled()
    press_cancel(dialog)
    assert win._sectionDialog is None and item.selections == ()


# ================================================================ 쓰기


def test_confirming_three_sections_writes_them_to_the_card_in_row_order(qtbot, tmp_path, basis):
    """구간 셋을 넣고 확인하면 카드에 행 순서대로 쓰이고 요약이 바뀌어야 한다.

    60fps. 행: 00:10:00:00~00:20:00:00, 00:30:00:30~00:31:00:00, 00:00:05:00~00:00:06:00
    -> selections == ((600, 1200), (1800.5, 1860), (5, 6)), 요약 "Sections 3 · 20:00"
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)

    set_rows(
        dialog,
        [
            ("00:10:00:00", "00:20:00:00"),
            ("00:30:00:30", "00:31:00:00"),
            ("00:00:05:00", "00:00:06:00"),
        ],
    )
    press_ok(dialog)

    assert win._sectionDialog is None
    assert item.selections == (
        TimeRange(600.0, 1200.0),
        TimeRange(1800.5, 1860.0),  # FF 30 ÷ 60fps = 0.5초
        TimeRange(5.0, 6.0),
    )
    assert item.section_frame_rate == Fraction(60)
    # 길이의 합 600 + 59.5 + 1 = 660.5초 → "11:00"(초 아래 버림)
    assert summary_text(win, item) == "Sections 3 · 11:00"


def test_confirming_the_single_whole_row_keeps_the_card_a_whole_download(qtbot, tmp_path, basis):
    """영상 전체 한 행뿐이면 확인해도 카드의 구간은 빈 튜플이고 표시가 그대로여야 한다.

    구간 없는 카드에서 편집 창을 열고 그대로 확인
    -> selections == (), 요약 자리의 글자는 열기 전과 같다
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    before = summary_text(win, item)
    dialog = open_editor(qtbot, win, item)

    press_ok(dialog)

    assert item.selections == ()
    assert item.section_frame_rate is None
    assert summary_text(win, item) == before


def test_cancel_leaves_the_card_untouched(qtbot, tmp_path, basis):
    """취소로 닫으면 넣은 구간이 카드에 쓰이지 않아야 한다.

    행을 00:10:00:00~00:20:00:00으로 고친 뒤 취소
    -> selections == ()
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    set_rows(dialog, [("00:10:00:00", "00:20:00:00")])

    press_cancel(dialog)

    assert item.selections == ()


def test_reopening_shows_the_sections_already_on_the_card(qtbot, tmp_path, basis):
    """구간이 있는 카드에서 다시 열면 그 구간이 행으로 보여야 한다.

    selections = ((600, 1200), (1800.5, 1860)), 60fps
    -> 행 ("00:10:00:00", "00:20:00:00"), ("00:30:00:30", "00:31:00:00")
    """
    item = _make_item(str(tmp_path))
    item.selections = (TimeRange(600.0, 1200.0), TimeRange(1800.5, 1860.0))
    win = open_window(tmp_path, item)

    dialog = open_editor(qtbot, win, item)

    assert [(row.startEdit.text(), row.endEdit.text()) for row in dialog._rows] == [
        ("00:10:00:00", "00:20:00:00"),
        ("00:30:00:30", "00:31:00:00"),
    ]


# ================================================================ 검증


ALL_FOUR = {"start clock", "start frame", "end clock", "end frame"}


def flagged_fields(row) -> set[str]:
    """붉게 칠해진 칸의 이름 — "start clock" · "start frame" · "end clock" · "end frame"."""
    fields = {
        "start clock": row.startEdit.clockEdit,
        "start frame": row.startEdit.frameEdit,
        "end clock": row.endEdit.clockEdit,
        "end frame": row.endEdit.frameEdit,
    }
    return {name for name, widget in fields.items() if widget.property("invalid") is True}


@pytest.mark.parametrize(
    "start, end, message, flagged",
    [
        # 시각 전체의 오류 — 시작과 끝의 두 칸 모두
        ("00:20:00:00", "00:10:00:00", "Start must be before end", ALL_FOUR),  # 역방향
        ("00:10:00:00", "00:10:00:00", "Start must be before end", ALL_FOUR),  # 길이 0
        # 길이보다 1프레임 뒤 — 영상의 끝을 넘은 시각(끝)의 두 칸
        (
            "00:10:00:00",
            "01:00:00:01",
            "Selection is outside the video",
            {"end clock", "end frame"},
        ),
        # 초 · 분 넘침 — 그 시각의 시분초 칸만
        ("00:10:60:00", "00:20:00:00", "Minutes and seconds must be below 60", {"start clock"}),
        ("00:60:00:00", "00:20:00:00", "Minutes and seconds must be below 60", {"start clock"}),
        ("00:00:75:00", "00:20:00:00", "Minutes and seconds must be below 60", {"start clock"}),
        # 프레임 넘침(60fps의 FF는 59까지) — 그 시각의 프레임 칸만
        (
            "00:10:00:60",
            "00:20:00:00",
            "Frame number must be below the frame rate",
            {"start frame"},
        ),
        (
            "00:10:00:00",
            "00:20:00:75",
            "Frame number must be below the frame rate",
            {"end frame"},
        ),
    ],
)
def test_invalid_input_is_shown_on_the_row_and_cannot_be_confirmed(
    qtbot, tmp_path, basis, start, end, message, flagged
):
    """틀린 입력은 그 행에 오류로 보이고, 틀린 칸만 붉게 칠해지며, 확인으로 카드에 쓰이지 않아야 한다.

    60fps · 길이 3600초에서 위 표의 (시작, 끝)
    -> 행의 문구 == message, 붉게 칠해진 칸 == flagged, 확인 꺼짐, 확인을 눌러도 selections == ()
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)

    set_rows(dialog, [(start, end)])

    row = dialog._rows[0]
    assert shown(row.errorLabel) == message
    assert flagged_fields(row) == flagged
    assert not dialog.okButton.isEnabled()
    dialog.accept()  # 꺼진 버튼을 건너뛰어 직접 확인을 청해도
    _pump()
    assert win._sectionDialog is dialog, "오류가 있는데 창이 닫혔다"
    assert item.selections == ()


def test_frame_field_and_range_follow_the_looked_up_frame_rate_and_length(qtbot, tmp_path, basis):
    """FF의 범위와 구간의 끝은 조회한 프레임률 · 길이로 판정해야 한다.

    29.97fps(2997/100) · 길이 10.02초
    -> 끝 "00:00:10:00" 통과 / "00:00:09:30" FF 초과 / "00:00:10:01"(10.0334초) 길이 초과
    -> 통과한 "00:00:01:15"~"00:00:10:00"의 시작 == 1 + 15 × 100 ÷ 2997초
    """
    basis.fps = Fraction(2997, 100)
    basis.duration = 10.02
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    row = dialog._rows[0]

    set_rows(dialog, [("00:00:01:15", "00:00:09:30")])
    assert shown(row.errorLabel) == "Frame number must be below the frame rate"
    set_rows(dialog, [("00:00:01:15", "00:00:10:01")])
    assert shown(row.errorLabel) == "Selection is outside the video"
    set_rows(dialog, [("00:00:01:15", "00:00:10:00")])
    assert not row.errorLabel.isVisible()
    press_ok(dialog)

    assert len(item.selections) == 1
    assert item.selections[0].start == pytest.approx(1 + 1500 / 2997, abs=1e-12)
    assert item.selections[0].end == 10.0


def test_a_section_one_frame_long_is_accepted_and_the_same_frame_twice_is_not(
    qtbot, tmp_path, basis
):
    """한 프레임짜리 구간은 통과하고, 시작과 끝이 둘 다 같은 구간 둘은 막혀야 한다.

    60fps. 00:10:00:00~00:10:00:01 한 행 -> 오류 없음
    같은 행을 하나 더 -> 두 행 모두 "Duplicate selection", 확인 꺼짐
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)

    set_rows(dialog, [("00:10:00:00", "00:10:00:01")])
    assert not dialog._rows[0].errorLabel.isVisible() and dialog.okButton.isEnabled()

    set_rows(dialog, [("00:10:00:00", "00:10:00:01"), ("00:10:00:00", "00:10:00:01")])
    assert [shown(row.errorLabel) for row in dialog._rows] == ["Duplicate selection"] * 2
    assert not dialog.okButton.isEnabled()


def test_overlapping_sections_are_accepted(qtbot, tmp_path, basis):
    """겹치는 구간은 통과해야 한다.

    60fps. 00:10:00:00~00:20:00:00, 00:15:00:00~00:25:00:00
    -> 오류 없음, 확인하면 두 구간이 쓰인다
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)

    set_rows(dialog, [("00:10:00:00", "00:20:00:00"), ("00:15:00:00", "00:25:00:00")])
    press_ok(dialog)

    assert item.selections == (TimeRange(600.0, 1200.0), TimeRange(900.0, 1500.0))


def test_the_twenty_first_section_cannot_be_added(qtbot, tmp_path, basis):
    """구간이 20개면 추가 버튼이 꺼지고 21번째 행이 생기지 않아야 한다.

    추가 버튼을 19번 눌러 20행(모두 빈 행 — 구간으로는 맨 위 한 행만 센다)
    -> 추가 버튼 꺼짐, 뷰모델에 직접 추가를 청해도 20행, 머리줄의 구간 수는 1
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)

    for _ in range(19):
        assert dialog.addButton.isEnabled()
        dialog.addButton.click()
        _pump()

    assert len(dialog._rows) == 20
    assert not dialog.addButton.isEnabled()
    dialog.viewModel().addRow()
    _pump()
    assert len(dialog._rows) == 20 and len(dialog.viewModel().rows) == 20
    assert shown(dialog.headerLabel) == "Sections 1 / 20 · 60fps · video ends at 01:00:00:00"


def test_out_of_range_digits_are_flagged_and_left_as_typed(qtbot, tmp_path, basis):
    """초 · 분이 60 이상이거나 프레임이 프레임률 이상이면 올림하지 않고 친 그대로 두고 오류로 강조해야 한다.

    60fps. 시작 칸에 숫자 7500(초 75)을 치고 칸을 떠남
    -> 칸의 값 == "00:00:75:00"(00:01:15:00으로 바뀌지 않는다), invalid, 확인 꺼짐
    시작 칸에 60(프레임 60)을 치고 칸을 떠남 -> 값 == "00:00:00:60", invalid
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    row = dialog._rows[0]

    type_into(row.startEdit, "7500")
    leave(row.startEdit)
    assert row.startEdit.text() == "00:00:75:00"
    assert row.startEdit.property("invalid") is True and not dialog.okButton.isEnabled()
    assert shown(row.errorLabel) == "Minutes and seconds must be below 60"

    type_into(row.startEdit, "60")
    leave(row.startEdit)
    assert row.startEdit.text() == "00:00:00:60"
    assert row.startEdit.property("invalid") is True
    assert shown(row.errorLabel) == "Frame number must be below the frame rate"


def type_more(edit, digits: str) -> None:
    """시분초 칸을 비우지 않고 숫자를 이어 친다 — 한 글자씩 치며 화면을 보는 테스트용."""
    type_clock(edit, digits)


def error_shown(row) -> str:
    """행 아래에 지금 보이는 오류 문구. 보이지 않으면 빈 문자열."""
    return shown(row.errorLabel) if row.errorLabel.isVisible() else ""


def test_start_past_the_end_is_flagged_while_typing(qtbot, tmp_path, basis):
    """시작 칸을 치는 중 시작이 끝과 같거나 넘으면 칸을 떠나기 전에 바로 오류를 띄워야 한다.

    60fps · 3600초. 끝을 00:10:00:00으로 둔 행의 시작 시분초 칸에 2 · 0 · 0 · 0을 차례로 침
    -> 00:02:00(숫자 셋)까지는 오류 없음, 00:20:00(숫자 넷)이 되는 순간
       "Start must be before end"와 두 칸 강조, 확인 꺼짐 — 칸을 떠나지 않았다
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    row = dialog._rows[0]
    type_into(row.endEdit, "100000")
    leave(row.endEdit)

    type_into(row.startEdit, "20000")
    assert row.startEdit.text() == "00:02:00:00" and error_shown(row) == ""

    type_more(row.startEdit, "0")

    assert row.startEdit.text() == "00:20:00:00"
    assert error_shown(row) == "Start must be before end"
    assert row.startEdit.property("invalid") is True and row.endEdit.property("invalid") is True
    assert not dialog.okButton.isEnabled()


def test_a_value_past_the_video_is_flagged_while_typing(qtbot, tmp_path, basis):
    """치는 중 끝이 영상 길이를 넘으면 칸을 떠나기 전에 바로 오류를 띄워야 한다.

    60fps · 3600초. 끝의 시분초 칸에 10000(01:00:00 — 영상 끝)까지는 오류 없음, 1을 더 쳐
    10:00:01이 되는 순간 "Selection is outside the video"
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    row = dialog._rows[0]

    type_into(row.endEdit, "1000000")
    assert row.endEdit.text() == "01:00:00:00" and error_shown(row) == ""

    type_more(row.endEdit, "1")

    assert row.endEdit.text() == "10:00:01:00"
    assert error_shown(row) == "Selection is outside the video"
    assert row.endEdit.property("invalid") is True


def test_an_end_still_below_the_start_waits_until_the_field_is_left(qtbot, tmp_path, basis):
    """끝 칸을 치는 중 끝이 아직 시작에 못 미치는 것은 칸을 떠날 때까지 띄우지 않되, 확인은 꺼 두어야 한다.

    60fps · 3600초. 시작 00:10:00:00인 행의 끝 시분초 칸을 비우고 2 · 0 · 0 · 0을 차례로 침
    -> 00:02:00(숫자 셋)까지 매 단계: 오류 문구 없음, 칸 강조 없음, 확인 꺼짐
    -> 00:20:00(숫자 넷): 오류 없음, 확인 켜짐
    끝을 프레임 5로 바꾸고 칸을 떠남 -> "Start must be before end", 시작 칸도 함께 강조
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    row = dialog._rows[0]
    type_into(row.startEdit, "00:10:00:00")
    leave(row.startEdit)

    type_into(row.endEdit, "")
    for digit in "200":
        type_more(row.endEdit, digit)
        assert error_shown(row) == "", f"{row.endEdit.text()}: 치는 도중에 오류가 떴다"
        assert row.endEdit.property("invalid") is False
        assert not dialog.okButton.isEnabled(), "띄우지 않은 오류가 확인을 막지 않는다"
    type_more(row.endEdit, "0")
    assert row.endEdit.text() == "00:20:00:00"
    assert error_shown(row) == "" and dialog.okButton.isEnabled()

    type_into(row.endEdit, "00:00:00:05")
    assert error_shown(row) == ""
    leave(row.endEdit)

    assert error_shown(row) == "Start must be before end"
    assert row.startEdit.property("invalid") is True and row.endEdit.property("invalid") is True


def test_a_field_overflow_on_the_way_to_a_valid_value_is_not_flagged(qtbot, tmp_path, basis):
    """치는 도중 잠깐 생기는 자리 넘침(초 ≥ 60)은 띄우지 않고, 다 친 값이 유효하면 오류가 없어야 한다.

    30fps · 3600초. 끝 시분초 칸에 1 · 9 · 0 · 0을 차례로 침
    -> "190"(00:01:90 — 초 90은 넘친다)에서 오류 문구 없음, 확인 꺼짐
    -> "1900"(00:19:00)을 치고 칸을 떠나도 오류 없음, 확인 켜짐
    """
    basis.fps = Fraction(30)
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    row = dialog._rows[0]

    type_into(row.endEdit, "")  # 두 칸을 비운다
    type_more(row.endEdit, "190")
    assert row.endEdit.text() == "00:01:90:00"
    assert error_shown(row) == "" and not dialog.okButton.isEnabled()

    type_more(row.endEdit, "0")
    leave(row.endEdit)

    assert row.endEdit.text() == "00:19:00:00"
    assert error_shown(row) == "" and dialog.okButton.isEnabled()


def test_a_frame_past_the_frame_rate_is_flagged_as_soon_as_both_digits_are_typed(
    qtbot, tmp_path, basis
):
    """프레임 칸은 한 자리일 때는 오류를 띄우지 않고, 두 자리를 다 쳐 프레임률 이상이 되면 칸을 떠나기 전에 바로 띄워야 한다.

    60fps. 끝 프레임 칸에 6 -> 오류 문구 없음(00:00:00:06). 0을 더 침(60)
    -> 칸을 떠나지 않았는데 "Frame number must be below the frame rate", 프레임 칸만 강조
    세 자리째 5를 침 -> 받지 않는다(값은 그대로 60)
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    row = dialog._rows[0]
    type_into(row.endEdit, "00:30:00:00")

    type_frame(row.endEdit, "6")
    assert row.endEdit.text() == "00:30:00:06" and error_shown(row) == ""

    type_frame(row.endEdit, "0")

    assert row.endEdit.text() == "00:30:00:60"
    assert error_shown(row) == "Frame number must be below the frame rate"
    assert row.endEdit.frameEdit.property("invalid") is True
    assert row.endEdit.clockEdit.property("invalid") is False
    assert row.startEdit.property("invalid") is False

    type_frame(row.endEdit, "5")
    assert row.endEdit.text() == "00:30:00:60"


def test_an_error_disappears_at_once_when_the_value_becomes_valid(qtbot, tmp_path, basis):
    """오류는 값이 유효해지는 순간 칸을 떠나지 않아도 바로 사라져야 한다.

    60fps · 3600초. 끝 시분초 칸에 100001(10:00:01 — 길이 초과)을 쳐 오류가 뜬 뒤 Backspace
    -> 01:00:00(영상 끝)이 되는 순간 오류 문구 없음, 칸 강조 없음, 확인 켜짐
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    row = dialog._rows[0]
    type_into(row.endEdit, "10:00:01:00")
    assert error_shown(row) == "Selection is outside the video"

    QTest.keyClick(row.endEdit.clockEdit, Qt.Key.Key_Backspace)
    _pump()

    assert row.endEdit.text() == "01:00:00:00"
    assert error_shown(row) == "" and row.endEdit.property("invalid") is False
    assert dialog.okButton.isEnabled()


def test_an_error_caused_in_another_row_shows_at_once(qtbot, tmp_path, basis):
    """치고 있는 칸 때문에 다른 행에 생긴 오류는 그 행에 바로 띄우고, 치고 있는 행의 것은 떠날 때 띄워야 한다.

    60fps. 첫째 행 00:10:00:00~00:20:00:00, 둘째 행 00:10:00:00~00:30:00:00.
    둘째 행의 끝 칸에 200000을 쳐 첫째 행과 같게 만듦(칸을 떠나지 않는다)
    -> 첫째 행: "Duplicate selection" 바로 표시. 둘째 행(치는 중): 오류 문구 없음. 확인 꺼짐
    둘째 행의 끝 칸을 떠남 -> 둘째 행에도 "Duplicate selection"
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    set_rows(dialog, [("00:10:00:00", "00:20:00:00"), ("00:10:00:00", "00:30:00:00")])
    first, second = dialog._rows

    type_into(second.endEdit, "200000")

    assert error_shown(first) == "Duplicate selection", "다른 행에 생긴 오류가 늦게 뜬다"
    assert error_shown(second) == ""
    assert not dialog.okButton.isEnabled()

    leave(second.endEdit)
    assert error_shown(second) == "Duplicate selection"


def test_every_error_is_shown_at_once_when_the_window_opens(qtbot, tmp_path, basis):
    """창을 열 때는 치고 있는 칸이 없으므로 떠날 때 띄우는 종류의 오류도 바로 띄워야 한다.

    카드의 구간이 600~1200초 둘(같은 구간 두 개 — 중복)인 채로 편집 창을 엶
    -> 두 행 모두 "Duplicate selection", 확인 꺼짐
    """
    item = _make_item(str(tmp_path))
    item.selections = (TimeRange(600.0, 1200.0), TimeRange(600.0, 1200.0))
    win = open_window(tmp_path, item)

    dialog = open_editor(qtbot, win, item)

    assert [error_shown(row) for row in dialog._rows] == ["Duplicate selection"] * 2
    assert not dialog.okButton.isEnabled()


def test_typed_digits_reach_the_card_and_milliseconds_are_only_shown(qtbot, tmp_path, basis):
    """숫자만 쳐서 넣은 값이 카드에 쓰이고, 밀리초는 툴팁에만 나와야 한다.

    60fps. 시작의 시분초 칸에 503 · 프레임 칸에 30(00:05:03:30), 끝의 시분초 칸에 1000(00:10:00:00)을 치고 확인
    -> 시작의 값 "00:05:03:30", 두 칸의 툴팁 "00:05:03.500", selections == ((303.5, 600),)
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    row = dialog._rows[0]

    type_into(row.startEdit, "50330")
    leave(row.startEdit)
    type_into(row.endEdit, "100000")
    leave(row.endEdit)

    assert row.startEdit.text() == "00:05:03:30"
    assert row.startEdit.clockEdit.toolTip() == "00:05:03.500"
    assert row.startEdit.frameEdit.toolTip() == "00:05:03.500"
    press_ok(dialog)
    assert item.selections == (TimeRange(303.5, 600.0),)


def test_ok_takes_the_digits_of_the_field_still_being_typed(qtbot, tmp_path, basis):
    """칸을 떠나지 않고 확인을 눌러도 그 칸에 친 숫자가 카드에 쓰여야 한다.

    60fps. 끝 칸에 100000(00:10:00:00)을 치고 칸을 떠나지 않은 채 확인을 누름
    -> selections == ((0, 600),)
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)

    type_into(dialog._rows[0].endEdit, "100000")
    press_ok(dialog)

    assert item.selections == (TimeRange(0.0, 600.0),)


@pytest.mark.parametrize("text", ["00:10:00", "5:03", "00:10:00.500", "00:10:00:0x"])
def test_viewmodel_still_refuses_text_that_is_not_four_fields(qtbot, tmp_path, basis, text):
    """뷰모델은 네 칸이 아니거나 숫자가 아닌 글을 형식 오류로 봐야 한다 — 입력 칸이 막아 주는 것에 기대지 않는다.

    60fps. 뷰모델의 첫 행 시작 칸에 세 칸 · 두 칸 · 밀리초 · 숫자 아닌 글자를 직접 넣음
    (빈 글은 빈 시각이다 — 오류가 아니다)
    -> 그 행의 오류 키 == "Invalid timecode format", 확인할 수 없다
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    viewmodel = dialog.viewModel()

    viewmodel.setText(0, 0, text)

    assert viewmodel.errorKey(0) == "Invalid timecode format"
    assert not viewmodel.canCommit()


# ================================================================ 순서 · 삭제


def test_moved_rows_reach_the_engine_in_the_new_order_with_matching_file_numbers(
    qtbot, tmp_path, basis, monkeypatch
):
    """위 · 아래로 바꾼 행의 순서가 엔진에 넘어가는 구간 순서이고 파일 이름의 _N이어야 한다.

    행 A(600~1200) · B(1800~1860) · C(5~6)에서 C의 ▲를 두 번, A(이제 2번째)의 ▼를 한 번
    -> 순서 C · B · A
    -> 제출된 content.selections == (C, B, A), selection_paths의 이름 == "제목 1080p_1.mp4" ~ "_3.mp4"
    """
    submissions: list = []

    class _Handle:
        def elapsed_seconds(self) -> float:
            return 1.0

        def wait(self, timeout=None) -> bool:
            return True

    class _Service:
        def submit(self, content, **kwargs):
            submissions.append(content)
            return _Handle()

    monkeypatch.setattr(mw_mod.QMessageBox, "warning", lambda *a, **k: None)
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    win.downloadViewModel._service = _Service()
    dialog = open_editor(qtbot, win, item)
    set_rows(
        dialog,
        [
            ("00:10:00:00", "00:20:00:00"),
            ("00:30:00:00", "00:31:00:00"),
            ("00:00:05:00", "00:00:06:00"),
        ],
    )

    dialog._rows[2].upButton.click()
    _pump()
    dialog._rows[1].upButton.click()
    _pump()
    dialog._rows[1].downButton.click()
    _pump()
    assert not dialog._rows[0].upButton.isEnabled() and not dialog._rows[2].downButton.isEnabled()
    press_ok(dialog)

    expected = (TimeRange(5.0, 6.0), TimeRange(1800.0, 1860.0), TimeRange(600.0, 1200.0))
    assert item.selections == expected
    try:
        win.downloadButton.click()
        _pump()
        assert len(submissions) == 1, "전역 다운로드가 카드를 엔진에 넘기지 않았다"
        content = submissions[0]
        assert content.selections == expected
        assert [os.path.basename(path) for path in content.selection_paths] == [
            f"제목 1080p_{number}.mp4" for number in (1, 2, 3)
        ]
    finally:
        if submissions:
            release_output_paths(submissions[0].selection_paths)


def test_deleting_a_row_removes_that_section_and_the_last_row_stays(qtbot, tmp_path, basis):
    """삭제는 그 행만 지우고, 하나 남은 행은 지워지지 않아야 한다.

    행 A(600~1200) · B(1800~1860)에서 A의 ✕ -> 행 B만 남는다
    B의 ✕(꺼져 있다) -> 행 B 그대로, 확인하면 selections == (B,)
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    set_rows(dialog, [("00:10:00:00", "00:20:00:00"), ("00:30:00:00", "00:31:00:00")])

    dialog._rows[0].deleteButton.click()
    _pump()
    assert [(row.startEdit.text(), row.endEdit.text()) for row in dialog._rows] == [
        ("00:30:00:00", "00:31:00:00")
    ]

    assert not dialog._rows[0].deleteButton.isEnabled()
    dialog._rows[0].deleteButton.click()
    _pump()
    assert [(row.startEdit.text(), row.endEdit.text()) for row in dialog._rows] == [
        ("00:30:00:00", "00:31:00:00")
    ]
    press_ok(dialog)
    assert item.selections == (TimeRange(1800.0, 1860.0),)


# ================================================================ 편집 중 건너뛰기


def test_the_card_being_edited_is_skipped_and_the_next_card_starts(qtbot, tmp_path, basis, started):
    """편집 창이 열린 카드는 시작되지 않고 다음 카드가 시작되어야 한다.

    카드 A(첫 줄) · B(둘째 줄) 모두 대기. A의 편집 창을 연 채 전역 다운로드를 청함
    -> 시작된 카드 == [B]. A의 상태는 WAITING 그대로
    """
    first, second = _make_item(str(tmp_path), "A"), _make_item(str(tmp_path), "B")
    win = open_window(tmp_path, first, second)
    open_editor(qtbot, win, first)

    win.contentManager.downloadItem()  # 창이 모달이라 버튼 대신 버튼이 부르는 것을 부른다
    _pump()

    assert started == [second], f"편집 중인 카드가 건너뛰어지지 않았다: {started}"
    assert first.downloadState == DownloadState.WAITING


def test_closing_the_editor_makes_the_card_a_target_again(qtbot, tmp_path, basis, started):
    """편집 창을 닫으면 그 카드가 다시 다운로드 대상이 되어야 한다.

    A 편집 중에 B가 시작됨 → A의 창을 취소로 닫음 → B가 끝남
    -> 시작된 카드 == [B, A]
    """
    first, second = _make_item(str(tmp_path), "A"), _make_item(str(tmp_path), "B")
    win = open_window(tmp_path, first, second)
    dialog = open_editor(qtbot, win, first)
    win.contentManager.downloadItem()
    _pump()
    assert started == [second]

    press_cancel(dialog)
    assert started == [second], "창을 닫는 것만으로 다운로드가 시작됐다"
    second.downloadState = DownloadState.FINISHED
    win.contentManager.emitFinishedRequest(second)
    _pump()

    assert started == [second, first]


def test_batch_waits_instead_of_finishing_while_the_only_card_left_is_being_edited(
    qtbot, tmp_path, basis, started
):
    """남은 대상이 편집 중인 카드뿐이면 배치를 끝내지 않고, 창을 닫으면 그 카드를 시작해야 한다.

    A 편집 중에 B가 시작되고 끝남(남은 대상은 A뿐)
    -> 전체 완료 신호 0건, 시작된 카드 == [B]
    A의 창을 닫음 -> 시작된 카드 == [B, A], 전체 완료 신호 0건
    """
    first, second = _make_item(str(tmp_path), "A"), _make_item(str(tmp_path), "B")
    win = open_window(tmp_path, first, second)
    finished_all = QSignalSpy(win.contentManager.finishedAllRequested)
    dialog = open_editor(qtbot, win, first)
    win.contentManager.downloadItem()
    _pump()

    second.downloadState = DownloadState.FINISHED
    win.contentManager.emitFinishedRequest(second)
    _pump()
    assert started == [second]
    assert finished_all.count() == 0, "편집 중인 카드를 두고 배치가 끝났다"

    press_cancel(dialog)

    assert started == [second, first]
    assert finished_all.count() == 0


def test_closing_the_editor_starts_nothing_when_no_download_was_waiting_on_it(
    qtbot, tmp_path, basis, started
):
    """멈춰 둔 배치가 없으면 편집 창을 닫아도 아무것도 시작하지 않아야 한다.

    다운로드를 청한 적 없이 A의 편집 창을 열고 확인으로 닫음
    -> 시작된 카드 == [], 전체 완료 신호 0건
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    finished_all = QSignalSpy(win.contentManager.finishedAllRequested)
    dialog = open_editor(qtbot, win, item)
    set_rows(dialog, [("00:10:00:00", "00:20:00:00")])

    press_ok(dialog)

    assert started == [] and finished_all.count() == 0
    assert item.downloadState == DownloadState.WAITING


def test_sections_are_not_written_when_the_card_left_waiting_while_the_editor_was_open(
    qtbot, tmp_path, basis
):
    """편집 창이 열린 사이 카드가 대기가 아니게 됐으면 확인해도 구간을 쓰지 않아야 한다.

    편집 창을 연 뒤 카드 상태를 RUNNING으로 바꾸고 00:10:00:00~00:20:00:00을 확인
    -> selections == (), 창은 닫힌다
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    set_rows(dialog, [("00:10:00:00", "00:20:00:00")])
    item.downloadState = DownloadState.RUNNING

    press_ok(dialog)

    assert item.selections == ()
    assert win._sectionDialog is None


# ================================================================ 배치 중 편집


def test_a_waiting_card_can_be_edited_while_another_card_is_downloading(
    qtbot, tmp_path, basis, started
):
    """다른 카드가 받는 중이어도 대기 카드의 편집 창이 열리고 구간이 쓰여야 한다.

    카드 A가 RUNNING, 카드 B가 대기. B의 구간 요약을 누르고 00:10:00:00~00:20:00:00을 확인
    -> 편집 창이 열린다, B의 selections == ((600, 1200),), 새로 시작된 카드 없음
    """
    first, second = _make_item(str(tmp_path), "A"), _make_item(str(tmp_path), "B")
    win = open_window(tmp_path, first, second)
    first.downloadState = DownloadState.RUNNING
    win.contentManager.model.notifyChanged(first)
    _pump()

    dialog = open_editor(qtbot, win, second)
    set_rows(dialog, [("00:10:00:00", "00:20:00:00")])
    press_ok(dialog)

    assert second.selections == (TimeRange(600.0, 1200.0),)
    assert started == []


# ================================================================ 해상도 변경


def _pick(win: VodDownloader, item: ContentItem, resolution: int) -> None:
    """카드에서 그 해상도의 버튼을 실제로 눌러 고른다 — 접혀 있으면 먼저 펼친다."""
    widget = win.listView.widgetFor(item)
    index = next(i for i, rep in enumerate(item.unique_reps) if rep[0] == resolution)
    widget.buttons[index].click()
    _pump()
    if widget.isExpanded():
        widget.buttons[index].click()
        _pump()
    assert item.resolution == resolution, "전제: 해상도가 바뀌어야 한다"


def _refitter(win: VodDownloader):
    return win.contentManager._sectionRefitter


def settle(qtbot, win: VodDownloader, pending: int = 0) -> None:
    """결과를 기다리는 조회가 pending건이 될 때까지 기다린다."""
    qtbot.waitUntil(lambda: _refitter(win).pendingCount() == pending, timeout=5000)
    _pump()


def give_sections(qtbot, win: VodDownloader, item: ContentItem, rows) -> tuple:
    """편집 창으로 구간을 넣는다 — 60fps · 1시간으로 확인된 구간이 된다."""
    dialog = open_editor(qtbot, win, item)
    set_rows(dialog, rows)
    press_ok(dialog)
    assert len(item.selections) == len(rows), "전제: 구간이 쓰여야 한다"
    return item.selections


def _on_grid(seconds: float, fps: int) -> bool:
    return abs(seconds * fps - round(seconds * fps)) < 1e-9


ODD = [("00:00:10:31", "00:00:20:01")]  # 60fps의 홀수 프레임 — 30fps에는 없는 자리다


def test_changing_resolution_refits_by_the_declared_rate_at_once_and_marks_the_length_unchecked(
    qtbot, tmp_path, basis
):
    """해상도를 바꾸면 조회가 끝나기 전에도 선언 프레임률로 맞추고 길이를 확인하는 중이라고 알려야 한다.

    60fps에서 정한 10.5167~20.0167초, 480p의 선언값 30fps, 480p의 조회는 붙잡아 둠
    -> 곧바로: 시작 · 끝이 30fps의 프레임 경계, section_check == "pending",
       알림 == "refit to 30fps · checking length"
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    give_sections(qtbot, win, item, ODD)
    basis.gates[480] = threading.Event()
    try:
        _pick(win, item, 480)

        after = item.selections[0]
        assert _on_grid(after.start, 30) and _on_grid(after.end, 30)
        assert item.section_check == "pending"
        assert notice_of(win, item) == "refit to 30fps · checking length"
    finally:
        basis.gates[480].set()
        settle(qtbot, win)


def test_the_looked_up_frame_rate_and_length_settle_the_sections(qtbot, tmp_path, basis):
    """조회가 끝나면 조회한 프레임률로 다시 맞추고 길이 확인 표시를 지워야 한다.

    60fps에서 정한 10.5167~20.0167초, 480p의 조회값 30fps · 3600초
    -> 시작 · 끝이 30fps의 프레임 경계이고 원래 시각에서 반 프레임(1/60초) 이내,
       section_frame_rate == 30, section_check == "", 알림 == "refit to 30fps"
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    before = give_sections(qtbot, win, item, ODD)[0]
    basis.by_resolution[480] = (Fraction(30), HOUR)

    _pick(win, item, 480)
    settle(qtbot, win)

    after = item.selections[0]
    for moved, original in ((after.start, before.start), (after.end, before.end)):
        assert _on_grid(moved, 30), "30fps의 프레임 경계가 아니다"
        assert abs(moved - original) <= 1 / 60 + 1e-9
    assert (after.start, after.end) != (before.start, before.end)
    assert item.section_frame_rate == Fraction(30) and item.section_check == ""
    assert notice_of(win, item) == "refit to 30fps"
    assert [call.resolution for call in basis.calls[1:]] == [480], "새 해상도로 조회해야 한다"


def test_the_looked_up_rate_wins_over_the_declared_rate(qtbot, tmp_path, basis):
    """선언값과 조회값이 다르면 조회값으로 맞춰야 한다.

    480p의 선언값 30fps, 조회값 25fps · 3600초. 60fps에서 정한 10.5167~20.0167초
    -> 시작 · 끝이 25fps의 프레임 경계이고 원래 시각에서 반 프레임(1/50초) 이내,
       section_frame_rate == 25, 알림 == "refit to 25fps"
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    before = give_sections(qtbot, win, item, ODD)[0]
    basis.by_resolution[480] = (Fraction(25), HOUR)

    _pick(win, item, 480)
    settle(qtbot, win)

    after = item.selections[0]
    for moved, original in ((after.start, before.start), (after.end, before.end)):
        assert _on_grid(moved, 25), "25fps의 프레임 경계가 아니다"
        assert abs(moved - original) <= 1 / 50 + 1e-9
    assert item.section_frame_rate == Fraction(25)
    assert notice_of(win, item) == "refit to 25fps"


def test_same_frame_rate_and_length_change_nothing(qtbot, tmp_path, basis):
    """프레임률 · 길이가 같은 해상도로 바꾸면 구간이 그대로이고 알림이 남지 않아야 한다.

    1080p · 720p 모두 60fps · 3600초인 카드에서 10.5167~20.0167초를 정한 뒤 720p를 고름
    -> 조회가 끝난 뒤 selections가 같은 객체, 요약 == "Sections 1 · 0:09"
    """
    item = _make_item(str(tmp_path))
    item.unique_reps = [
        StreamEntry(1080, "u1", frame_rate=60.0),
        StreamEntry(720, "u2", frame_rate=60.0),
    ]
    win = open_window(tmp_path, item)
    before = give_sections(qtbot, win, item, ODD)

    _pick(win, item, 720)
    settle(qtbot, win)

    assert item.selections is before
    assert item.section_refit_fps is None and item.section_check == ""
    assert summary_text(win, item) == "Sections 1 · 0:09"


def tooltip_of(win: VodDownloader, item: ContentItem) -> str:
    """구간 요약 자리의 툴팁 — 알림의 전문이 폭과 무관하게 여기에 있다."""
    return win.listView.widgetFor(item).fileSizeLabel.toolTip()


PULLED = "This resolution is shorter. Sections now end at the end of the video."
EXTENDED = "This resolution is longer. Sections that reached the end now reach it."
UNFIT = "Some sections start after the end of this resolution."


def test_a_section_that_reached_the_end_is_pulled_to_the_shorter_end(qtbot, tmp_path, basis):
    """옛 영상의 끝에 닿아 있던 구간은 더 짧은 새 영상의 끝으로 당겨지고 카드에 적혀야 한다.

    60fps · 3600초에서 3540~3600초(끝 = 영상 끝)를 정함. 480p의 조회값 30fps · 3590초
    -> 구간 == (3540, 3590), section_end_pulled, 늘림 · 받을 수 없는 구간 없음, 툴팁에 당겼다는 문장
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    give_sections(qtbot, win, item, [("00:59:00:00", "01:00:00:00")])
    basis.by_resolution[480] = (Fraction(30), 3590.0)

    _pick(win, item, 480)
    settle(qtbot, win)

    assert item.selections == (TimeRange(3540.0, 3590.0),)
    assert item.section_end_pulled and not item.section_end_extended
    assert item.section_unfit == frozenset()
    assert PULLED in tooltip_of(win, item)


def test_a_section_past_the_new_length_is_pulled_and_sections_inside_stay(qtbot, tmp_path, basis):
    """끝이 새 길이를 넘는 구간은 새 영상의 끝으로 당겨지고, 길이 안의 구간은 그대로여야 한다.

    60fps · 3600초에서 600~1200초와 3500~3595초를 정함. 480p의 조회값 30fps · 3590초
    -> 구간 == ((600, 1200), (3500, 3590)), section_end_pulled, 툴팁에 당겼다는 문장
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    give_sections(
        qtbot, win, item, [("00:10:00:00", "00:20:00:00"), ("00:58:20:00", "00:59:55:00")]
    )
    basis.by_resolution[480] = (Fraction(30), 3590.0)

    _pick(win, item, 480)
    settle(qtbot, win)

    assert item.selections == (TimeRange(600.0, 1200.0), TimeRange(3500.0, 3590.0))
    assert item.section_end_pulled and not item.section_end_extended
    assert item.section_unfit == frozenset()
    assert PULLED in tooltip_of(win, item)


def test_only_the_section_that_reached_the_end_grows_with_a_longer_video(qtbot, tmp_path, basis):
    """새 영상이 더 길면 옛 영상의 끝에 닿아 있던 구간만 늘어나고 그 밖의 구간은 그대로여야 한다.

    60fps · 3600초에서 600~1200초와 3540~3600초(끝 = 영상 끝)를 정함. 480p의 조회값 30fps · 3700초
    -> 구간 == ((600, 1200), (3540, 3700)), section_end_extended, 당김 없음, 툴팁에 늘렸다는 문장
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    give_sections(
        qtbot, win, item, [("00:10:00:00", "00:20:00:00"), ("00:59:00:00", "01:00:00:00")]
    )
    basis.by_resolution[480] = (Fraction(30), 3700.0)

    _pick(win, item, 480)
    settle(qtbot, win)

    assert item.selections == (TimeRange(600.0, 1200.0), TimeRange(3540.0, 3700.0))
    assert item.section_end_extended and not item.section_end_pulled
    assert EXTENDED in tooltip_of(win, item)


def test_a_section_that_starts_after_the_new_end_is_kept_and_flagged(qtbot, tmp_path, basis):
    """새 영상의 끝 이후에서 시작하는 구간은 지우거나 고치지 않고 받을 수 없다고 경고해야 한다.

    60fps · 3600초에서 600~1200초와 3595~3599초를 정함. 480p의 조회값 30fps · 3590초
    -> 구간 == ((600, 1200), (3595, 3599)), section_unfit == {1}, 당김 없음,
       경고 알림 == "1 outside the video", 툴팁에 받을 수 없다는 문장
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    give_sections(
        qtbot, win, item, [("00:10:00:00", "00:20:00:00"), ("00:59:55:00", "00:59:59:00")]
    )
    basis.by_resolution[480] = (Fraction(30), 3590.0)

    _pick(win, item, 480)
    settle(qtbot, win)

    assert item.selections == (TimeRange(600.0, 1200.0), TimeRange(3595.0, 3599.0))
    assert item.section_unfit == frozenset({1})
    assert not item.section_end_pulled and not item.section_end_extended
    assert win.listView.widgetFor(item)._sectionNotice(warnings_only=True) == "1 outside the video"
    assert notice_of(win, item) == "refit to 30fps · 1 outside the video"
    assert UNFIT in tooltip_of(win, item)


def test_a_failed_lookup_keeps_the_declared_refit_and_says_the_length_is_unchecked(
    qtbot, tmp_path, basis
):
    """조회가 실패하면 선언값으로 맞춘 구간을 두고 길이를 확인하지 못했다고 알려야 한다.

    60fps에서 정한 10.5167~20.0167초, 480p의 선언값 30fps, 480p의 조회는 예외
    -> 시작 · 끝이 30fps의 프레임 경계, section_check == "unverified",
       알림 == "refit to 30fps · length not checked"
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    give_sections(qtbot, win, item, ODD)
    basis.fail_resolutions.add(480)

    _pick(win, item, 480)
    settle(qtbot, win)

    after = item.selections[0]
    assert _on_grid(after.start, 30) and _on_grid(after.end, 30)
    assert item.section_check == "unverified"
    assert notice_of(win, item) == "refit to 30fps · length not checked"


def test_a_late_result_for_an_earlier_resolution_is_dropped(qtbot, tmp_path, basis):
    """조회가 끝나기 전에 해상도를 또 바꾸면 앞 해상도의 늦은 결과를 버려야 한다.

    1080p(60) · 720p(60) · 480p(선언 30) 카드. 480p를 고르고(조회 붙잡음) 곧 720p를 고름.
    720p의 조회(60fps · 3600초)를 먼저 끝내고, 그 뒤에 480p의 조회(25fps · 3000초)를 끝냄
    -> 끝까지 selections == 처음 정한 구간, section_frame_rate == 60, 요약 == "Sections 1 · 0:09"
    """
    item = _make_item(str(tmp_path))
    item.unique_reps = [
        StreamEntry(1080, "u1", frame_rate=60.0),
        StreamEntry(720, "u2", frame_rate=60.0),
        StreamEntry(480, "u3", frame_rate=30.0),
    ]
    win = open_window(tmp_path, item)
    before = give_sections(qtbot, win, item, ODD)
    basis.by_resolution[480] = (Fraction(25), 3000.0)
    basis.gates[480], basis.gates[720] = threading.Event(), threading.Event()
    try:
        _pick(win, item, 480)
        _pick(win, item, 720)
        assert _refitter(win).pendingCount() == 2

        basis.gates[720].set()  # 새 해상도의 결과가 먼저 온다
        settle(qtbot, win, pending=1)
        assert item.selections == before and item.section_check == ""

        basis.gates[480].set()  # 앞 해상도의 결과가 늦게 온다
        settle(qtbot, win)
    finally:
        basis.gates[480].set()
        basis.gates[720].set()

    assert basis.returned[-2:] == [720, 480], "전제: 480p의 결과가 나중에 와야 한다"
    assert item.selections == before
    assert item.section_frame_rate == Fraction(60)
    assert summary_text(win, item) == "Sections 1 · 0:09"


def test_an_earlier_result_that_arrives_first_is_dropped_and_the_newer_one_settles(
    qtbot, tmp_path, basis
):
    """해상도를 또 바꾼 뒤 앞 해상도의 결과가 먼저 와도 쓰지 않고, 새 해상도의 결과로 정해야 한다.

    1080p(60) · 720p(60) · 480p(선언 30) 카드. 480p를 고르고(조회 붙잡음) 곧 720p를 고름.
    480p의 조회(25fps · 3000초)를 먼저 끝내고, 그 뒤에 720p의 조회(60fps · 3600초)를 끝냄
    -> 480p의 결과가 온 뒤에도 selections == 처음 정한 구간이고 section_check == "pending"
    -> 720p의 결과가 온 뒤 selections == 처음 정한 구간, section_frame_rate == 60, 알림 없음
    """
    item = _make_item(str(tmp_path))
    item.unique_reps = [
        StreamEntry(1080, "u1", frame_rate=60.0),
        StreamEntry(720, "u2", frame_rate=60.0),
        StreamEntry(480, "u3", frame_rate=30.0),
    ]
    win = open_window(tmp_path, item)
    before = give_sections(qtbot, win, item, ODD)
    basis.by_resolution[480] = (Fraction(25), 3000.0)
    basis.gates[480], basis.gates[720] = threading.Event(), threading.Event()
    try:
        _pick(win, item, 480)
        _pick(win, item, 720)

        basis.gates[480].set()  # 앞 해상도의 결과가 먼저 온다
        settle(qtbot, win, pending=1)
        assert basis.returned[-1] == 480, "전제: 480p의 조회가 먼저 끝나야 한다"
        assert item.selections == before, "앞 해상도의 결과가 쓰였다"
        assert item.section_check == "pending", "새 해상도의 조회를 아직 기다려야 한다"

        basis.gates[720].set()
        settle(qtbot, win)
    finally:
        basis.gates[480].set()
        basis.gates[720].set()

    assert item.selections == before
    assert item.section_frame_rate == Fraction(60) and item.section_check == ""
    assert summary_text(win, item) == "Sections 1 · 0:09"


def test_a_result_that_arrives_after_the_download_started_is_dropped(qtbot, tmp_path, basis):
    """조회가 끝나기 전에 받기 시작한 카드에는 늦은 결과를 쓰지 않아야 한다.

    480p를 고르고(조회 붙잡음, 조회값 25fps · 3000초) 카드 상태를 RUNNING으로 바꾼 뒤 조회를 끝냄
    -> selections가 받기 시작할 때의 객체 그대로, section_frame_rate == 30(선언값)
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    give_sections(qtbot, win, item, ODD)
    basis.by_resolution[480] = (Fraction(25), 3000.0)
    basis.gates[480] = threading.Event()
    try:
        _pick(win, item, 480)
        at_start = item.selections
        item.downloadState = DownloadState.RUNNING
        win.contentManager.model.notifyChanged(item)
    finally:
        basis.gates[480].set()
    settle(qtbot, win)

    assert basis.returned[-1] == 480, "전제: 조회가 끝나야 한다"
    assert item.selections is at_start
    assert item.section_frame_rate == Fraction(30)


def test_a_result_that_arrives_after_the_card_was_deleted_is_dropped(qtbot, tmp_path, basis):
    """조회가 끝나기 전에 지운 카드에는 늦은 결과를 쓰지 않아야 한다.

    480p를 고르고(조회 붙잡음, 조회값 25fps · 3000초) 카드를 지운 뒤 조회를 끝냄
    -> selections가 지울 때의 객체 그대로, section_frame_rate == 30(선언값)
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    give_sections(qtbot, win, item, ODD)
    basis.by_resolution[480] = (Fraction(25), 3000.0)
    basis.gates[480] = threading.Event()
    try:
        _pick(win, item, 480)
        at_delete = item.selections
        win.contentManager.removeItem(item)
        _pump()
    finally:
        basis.gates[480].set()
    settle(qtbot, win)

    assert basis.returned[-1] == 480, "전제: 조회가 끝나야 한다"
    assert item.selections is at_delete
    assert item.section_frame_rate == Fraction(30)


def test_editing_again_clears_the_refit_notice(qtbot, tmp_path, basis):
    """다시 맞춘 알림은 구간을 다시 편집해 확인하면 사라져야 한다.

    60fps에서 정한 구간을 480p(30fps)로 옮겨 알림이 뜬 뒤, 편집 창을 열어 확인
    -> 요약 == "Sections 1 · 0:09"
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    give_sections(qtbot, win, item, ODD)
    basis.by_resolution[480] = (Fraction(30), HOUR)
    _pick(win, item, 480)
    settle(qtbot, win)
    assert notice_of(win, item) == "refit to 30fps"

    dialog = open_editor(qtbot, win, item)
    press_ok(dialog)

    assert summary_text(win, item) == "Sections 1 · 0:09"
    assert len(item.selections) == 1


def test_whole_download_card_changes_resolution_without_a_lookup(qtbot, tmp_path, basis):
    """구간 없는 카드는 해상도를 바꿔도 조회를 돌리지 않고 요약 자리에 구간 요약이 나오지 않아야 한다.

    구간 없는 mp4 카드에서 480p를 고름
    -> 조회 0건, selections == (), 요약 자리의 글자에 "Sections" · "refit" · "length" 없음
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)

    _pick(win, item, 480)
    settle(qtbot, win)

    assert basis.calls == []
    assert item.selections == () and item.section_check == ""
    text = summary_text(win, item)
    assert not any(word in text for word in ("Sections", "refit", "length"))


def test_picking_the_resolution_already_picked_starts_no_lookup(qtbot, tmp_path, basis):
    """이미 고른 해상도를 다시 고르면 조회를 돌리지 않아야 한다.

    구간이 있는 카드(1080p)에서 1080p 버튼을 다시 누름
    -> 편집 창의 조회 1건뿐, section_check == ""
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    give_sections(qtbot, win, item, ODD)

    _pick(win, item, 1080)
    settle(qtbot, win)

    assert len(basis.calls) == 1
    assert item.section_check == ""


def test_the_default_pick_of_a_new_card_starts_no_lookup(qtbot, tmp_path, basis):
    """카드가 만들어질 때 스스로 고르는 기본 해상도는 조회를 돌리지 않아야 한다.

    구간과 확인된 기준값(60fps · 3600초)을 이미 든 아이템(고른 해상도 없음)을 목록에 넣음
    -> 카드가 1080p를 기본으로 고른다, 조회 0건, section_verified가 넣을 때의 객체 그대로
    """
    item = _make_item(str(tmp_path))
    item.selections = (TimeRange(600.0, 1200.0),)
    item.section_frame_rate = Fraction(60)
    verified = (item.selections, SectionBasis(fps=Fraction(60), duration=HOUR))
    item.section_verified = verified

    win = open_window(tmp_path, item)
    settle(qtbot, win)

    assert item.resolution == 1080, "전제: 카드가 기본 해상도를 골라야 한다"
    assert basis.calls == []
    assert item.section_verified is verified and item.section_check == ""


def test_notice_gives_way_when_it_does_not_fit_in_the_row(qtbot, tmp_path, basis):
    """알림까지 붙인 요약이 3행에 안 들어가면 알림을 떼고 요약만 적어야 한다.

    구간 하나인 대기 카드의 알림을 어떤 창 폭보다도 긴 글(프레임률 숫자 400자리)로 둠
    -> 요약 자리의 글 == "Sections 1 · 0:09"(알림 없음), 툴팁에는 알림 문장이 남는다
    -> 고른 해상도 버튼이 제 폭보다 좁게 눌리지 않고, 카드가 목록 폭을 넘지 않는다
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    give_sections(qtbot, win, item, ODD)
    widget = win.listView.widgetFor(item)

    item.section_refit_fps = Fraction(10**400)  # 폭을 폰트에서 유도하지 않아도 어떤 행보다 길다
    win.contentManager.model.notifyChanged(item)
    _pump()

    assert summary_text(win, item) == "Sections 1 · 0:09"
    tooltip = widget.fileSizeLabel.toolTip()
    assert "Sections were moved to the frames of the new frame rate." in tooltip
    selected = widget._selectedButton
    assert selected.isVisible() and selected.width() >= selected.minimumSizeHint().width()
    assert widget.width() <= win.listView.viewport().width()


# ================================================================ 받을 수 없는 구간 빼고 받기


class _Handle:
    """DownloadHandle 대역 — 뷰모델이 쓰는 것만."""

    def elapsed_seconds(self) -> float:
        return 72.0

    def wait(self, timeout=None) -> bool:
        return True


class _Service:
    """DownloadService 대역 — 제출된 것을 적어 두고 테스트가 엔진처럼 콜백을 부르게 한다."""

    def __init__(self):
        self.submissions: list[dict] = []

    def submit(self, content, **kwargs):
        self.submissions.append({"content": content, **kwargs})
        return _Handle()

    def finish(self, index: int = -1) -> None:
        """엔진이 넘겨받은 구간을 모두 잘라 끝냈다고 알린다 — 구간 파일도 만든다."""
        submission = self.submissions[index]
        content, data = submission["content"], submission["data"]
        for path in content.selection_paths:
            with open(path, "wb") as file:
                file.write(b"section")
        data.sections_total = data.sections_done = len(content.selections)
        submission["on_finished"]()


@pytest.fixture
def service(monkeypatch):
    """엔진 자리의 서비스 대역 — 창을 만든 뒤 `use(win)`으로 끼운다. 끝나면 파일명 예약을 푼다."""
    monkeypatch.setattr(mw_mod.QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(mw_mod.QMessageBox, "information", lambda *a, **k: None)
    fake = _Service()

    def use(win: VodDownloader) -> _Service:
        win.downloadViewModel._service = fake
        return fake

    yield use
    for submission in fake.submissions:
        release_output_paths(submission["content"].selection_paths)


THREE = [
    ("00:10:00:00", "00:20:00:00"),
    ("00:59:55:00", "00:59:59:00"),  # 480p(3590초)에서는 영상 끝 이후다
    ("00:30:00:00", "00:40:00:00"),
]
FIRST, SECOND, THIRD = (
    TimeRange(600.0, 1200.0),
    TimeRange(3595.0, 3599.0),
    TimeRange(1800.0, 2400.0),
)


def card_with_an_unfit_section(qtbot, tmp_path, basis, service, rows=THREE):
    """구간을 정한 뒤 480p(30fps · 3590초)로 바꿔 둘째 구간이 받을 수 없게 된 대기 카드."""
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    engine = service(win)
    give_sections(qtbot, win, item, rows)
    basis.by_resolution[480] = (Fraction(30), 3590.0)
    _pick(win, item, 480)
    settle(qtbot, win)
    return win, item, engine


def start_batch(win: VodDownloader) -> None:
    win.downloadButton.click()
    _pump()


def names(paths) -> list[str]:
    return [os.path.basename(path) for path in paths]


def test_only_the_unfit_section_is_left_out_and_its_file_number_stays_empty(
    qtbot, tmp_path, basis, service
):
    """받을 수 없는 구간만 엔진에 넘어가지 않고, 파일 번호는 목록 순서대로 비워 두어야 한다.

    구간 셋 가운데 둘째가 받을 수 없는 카드(480p)를 받기 시작
    -> 제출된 selections == (첫째, 셋째), 파일 이름 == "제목 480p_1.mp4" · "제목 480p_3.mp4"
    -> 카드의 selections는 셋 그대로
    """
    win, item, engine = card_with_an_unfit_section(qtbot, tmp_path, basis, service)
    assert item.section_unfit == frozenset({1}), "전제: 둘째 구간이 받을 수 없어야 한다"

    start_batch(win)

    assert len(engine.submissions) == 1, "카드가 엔진에 넘어가지 않았다"
    content = engine.submissions[0]["content"]
    assert content.selections == (FIRST, THIRD)
    assert names(content.selection_paths) == ["제목 480p_1.mp4", "제목 480p_3.mp4"]
    assert item.selections == (FIRST, SECOND, THIRD)


def test_the_card_ends_as_a_partial_failure_counting_the_section_left_out(
    qtbot, tmp_path, basis, service
):
    """넘긴 구간을 다 받아도 뺀 구간이 있으면 카드는 일부 실패로 끝나고 뺀 수가 실패 수에 들어야 한다.

    구간 셋 가운데 둘째를 뺀 카드에서 엔진이 넘겨받은 둘을 모두 끝냄
    -> 상태 FAILED, sections_done == 2, sections_failed == 1,
       카드 문구에 "1 failed" · "2/3" · "Section is outside the video"
    """
    win, item, engine = card_with_an_unfit_section(qtbot, tmp_path, basis, service)
    start_batch(win)

    engine.finish()
    qtbot.waitUntil(lambda: item.downloadState == DownloadState.FAILED, timeout=3000)
    _pump()

    assert (item.sections_done, item.sections_failed) == (2, 1)
    status = shown(win.listView.widgetFor(item).statusLabel)
    assert "1 failed" in status and "2/3" in status
    assert "Section is outside the video" in status


def test_retry_leaves_the_same_section_out_again_and_does_not_redo_finished_files(
    qtbot, tmp_path, basis, service
):
    """재시도해도 받을 수 없는 구간은 다시 빠지고, 이미 만든 구간 파일은 다시 받지 않아야 한다.

    둘째를 빼고 끝난 카드에서 ↻ → 받기 시작: 구간 파일 둘이 그대로 있다
    -> 새 제출 없음, 카드는 다시 FAILED · "Section is outside the video"
    셋째 구간의 파일을 지우고 ↻ → 받기 시작
    -> 새 제출 1건: selections == (첫째, 셋째)(둘째는 다시 빠진다), 파일 이름 그대로,
       이어받기 기록의 끝낸 구간 == {0}
    """
    win, item, engine = card_with_an_unfit_section(qtbot, tmp_path, basis, service)
    start_batch(win)
    engine.finish()
    qtbot.waitUntil(lambda: item.downloadState == DownloadState.FAILED, timeout=3000)
    _pump()
    widget = win.listView.widgetFor(item)

    widget.retryButton.click()
    _pump()
    start_batch(win)
    qtbot.waitUntil(lambda: item.downloadState == DownloadState.FAILED, timeout=3000)
    _pump()
    assert len(engine.submissions) == 1, "끝낸 구간을 다시 받으려 했다"
    assert "Section is outside the video" in shown(widget.statusLabel)

    os.remove(engine.submissions[0]["content"].selection_paths[1])
    widget.retryButton.click()
    _pump()
    start_batch(win)

    assert len(engine.submissions) == 2
    content = engine.submissions[1]["content"]
    assert content.selections == (FIRST, THIRD)
    assert names(content.selection_paths) == ["제목 480p_1.mp4", "제목 480p_3.mp4"]
    assert content.section_resume.done == frozenset({0})


def test_a_card_whose_sections_are_all_unfit_fails_at_once_and_the_batch_goes_on(
    qtbot, tmp_path, basis, service
):
    """모든 구간이 빠지는 카드는 엔진에 넘기지 않고 곧바로 실패로 끝내고 다음 카드로 가야 한다.

    카드 A의 구간은 하나뿐이고 받을 수 없다(480p). 카드 B는 구간 없는 대기 카드
    -> A는 제출되지 않고 FAILED · "Section is outside the video", sections_failed == 1
    -> B가 제출된다(구간 없음)
    """
    win, first, engine = card_with_an_unfit_section(
        qtbot, tmp_path, basis, service, rows=[("00:59:55:00", "00:59:59:00")]
    )
    second = _make_item(str(tmp_path), "B")
    win.contentManager.model.addItem(second)
    _pump()
    assert first.section_unfit == frozenset({0}), "전제: 하나뿐인 구간이 받을 수 없어야 한다"

    start_batch(win)
    qtbot.waitUntil(lambda: first.downloadState == DownloadState.FAILED, timeout=3000)
    _pump()

    assert first.sections_failed == 1
    status = shown(win.listView.widgetFor(first).statusLabel)
    assert "Section is outside the video" in status
    assert [submission["content"].url for submission in engine.submissions] == [second.vod_url]
    assert engine.submissions[0]["content"].selections == ()


def test_a_card_whose_length_could_not_be_checked_hands_every_section_to_the_engine(
    qtbot, tmp_path, basis, service
):
    """길이를 확인하지 못한 카드는 구간을 빼지 않고 전부 엔진에 넘겨야 한다.

    구간 셋을 정한 뒤 480p로 바꿨는데 조회가 실패함(길이 미확인)
    -> 제출된 selections가 셋, 파일 이름 "_1" ~ "_3"
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    engine = service(win)
    give_sections(qtbot, win, item, THREE)
    basis.fail_resolutions.add(480)
    _pick(win, item, 480)
    settle(qtbot, win)
    assert item.section_check == "unverified"

    start_batch(win)

    content = engine.submissions[0]["content"]
    assert len(content.selections) == 3
    assert names(content.selection_paths) == [f"제목 480p_{n}.mp4" for n in (1, 2, 3)]


# ================================================================ 길이 확인 중 건너뛰기


def card_checking_its_length(qtbot, win: VodDownloader, item: ContentItem, basis) -> None:
    """구간을 정한 뒤 480p로 바꿔, 조회가 붙잡혀 길이를 확인하는 중인 카드로 만든다."""
    give_sections(qtbot, win, item, ODD)
    basis.gates[480] = threading.Event()
    _pick(win, item, 480)
    assert item.section_check == "pending", "전제: 길이를 확인하는 중이어야 한다"


def test_a_card_still_checking_its_length_is_skipped_and_taken_after_the_lookup(
    qtbot, tmp_path, basis, started
):
    """길이를 확인하는 중인 카드는 건너뛰고, 조회가 끝난 뒤에는 다시 대상이 되어야 한다.

    카드 A(첫 줄)는 길이 확인 중, B는 대기. 받기를 청함 -> 시작된 카드 == [B]
    조회를 끝내고 B가 끝남 -> 시작된 카드 == [B, A]
    """
    first, second = _make_item(str(tmp_path), "A"), _make_item(str(tmp_path), "B")
    win = open_window(tmp_path, first, second)
    card_checking_its_length(qtbot, win, first, basis)
    try:
        win.contentManager.downloadItem()
        _pump()
        assert started == [second], f"길이 확인 중인 카드가 건너뛰어지지 않았다: {started}"
    finally:
        basis.gates[480].set()
    settle(qtbot, win)
    assert started == [second], "조회가 끝난 것만으로 받는 중에 또 시작됐다"

    second.downloadState = DownloadState.FINISHED
    win.contentManager.emitFinishedRequest(second)
    _pump()

    assert started == [second, first]


def test_batch_waits_for_the_lookup_when_only_a_checking_card_is_left(
    qtbot, tmp_path, basis, started
):
    """남은 대상이 길이 확인 중인 카드뿐이면 배치를 끝내지 않고, 조회가 끝나면 그 카드를 받아야 한다.

    길이 확인 중인 카드 하나만 있는 목록에서 받기를 청함 -> 시작된 카드 없음, 전체 완료 신호 0건
    조회를 끝냄 -> 시작된 카드 == [그 카드], 전체 완료 신호 0건
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    finished_all = QSignalSpy(win.contentManager.finishedAllRequested)
    card_checking_its_length(qtbot, win, item, basis)
    try:
        win.contentManager.downloadItem()
        _pump()
        assert started == [] and finished_all.count() == 0
    finally:
        basis.gates[480].set()

    qtbot.waitUntil(lambda: started == [item], timeout=5000)
    assert item.section_check == "" and finished_all.count() == 0


def test_a_card_whose_lookup_failed_is_received_as_it_is(qtbot, tmp_path, basis, started):
    """조회가 실패한 카드는 막지 않고 선언값으로 맞춘 그대로 받아야 한다.

    길이 확인 중인 카드 하나에서 받기를 청한 뒤 조회가 예외로 끝남
    -> 시작된 카드 == [그 카드], section_check == "unverified"
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    basis.fail_resolutions.add(480)
    card_checking_its_length(qtbot, win, item, basis)
    try:
        win.contentManager.downloadItem()
        _pump()
        assert started == []
    finally:
        basis.gates[480].set()

    qtbot.waitUntil(lambda: started == [item], timeout=5000)
    assert item.section_check == "unverified"


# ================================================================ 편집 창 — 받을 수 없는 구간 · 기다리는 안내


def test_opening_the_editor_marks_the_unfit_section_and_fixing_it_clears_the_warning(
    qtbot, tmp_path, basis, service
):
    """받을 수 없는 구간이 든 카드를 열면 그 행이 강조되고 확인이 막히며, 고치면 풀려야 한다.

    둘째 구간(3595~3599초)이 받을 수 없는 480p(30fps · 3590초) 카드의 편집 창을 엶
    -> 둘째 행의 두 칸 invalid, 오류 문구 "Selection is outside the video", 첫째 · 셋째 행은 정상, 확인 꺼짐
    둘째 행을 00:45:00:00~00:50:00:00으로 고침 -> invalid 풀림, 확인 켜짐
    확인 -> section_unfit == 빈 집합, 알림 없음
    """
    win, item, _engine = card_with_an_unfit_section(qtbot, tmp_path, basis, service)

    dialog = open_editor(qtbot, win, item)

    rows = dialog._rows
    assert rows[1].startEdit.property("invalid") is True
    assert rows[1].endEdit.property("invalid") is True
    assert shown(rows[1].errorLabel) == "Selection is outside the video"
    for row in (rows[0], rows[2]):
        assert row.startEdit.property("invalid") is False and not row.errorLabel.isVisible()
    assert not dialog.okButton.isEnabled()

    type_into(rows[1].startEdit, "00:45:00:00")
    type_into(rows[1].endEdit, "00:50:00:00")
    assert rows[1].startEdit.property("invalid") is False
    assert rows[1].endEdit.property("invalid") is False
    assert not rows[1].errorLabel.isVisible() and dialog.okButton.isEnabled()
    press_ok(dialog)

    assert item.section_unfit == frozenset()
    assert notice_of(win, item) == ""


def test_the_waiting_hint_shows_only_while_the_batch_waits_for_the_edited_card(
    qtbot, tmp_path, basis, started
):
    """편집 창 아래쪽 안내는 배치가 그 카드를 기다리는 동안에만 보여야 한다.

    A 편집 중, B 대기. 창을 열었을 때 -> 안내 숨김
    받기를 청해 B가 받는 중 -> 안내 숨김
    B가 끝나 남은 대상이 A뿐 -> 안내 보임
    """
    first, second = _make_item(str(tmp_path), "A"), _make_item(str(tmp_path), "B")
    win = open_window(tmp_path, first, second)
    dialog = open_editor(qtbot, win, first)
    assert not dialog.waitHintLabel.isVisible()

    win.contentManager.downloadItem()
    _pump()
    assert started == [second]
    assert not dialog.waitHintLabel.isVisible(), "다른 카드를 받는 중인데 안내가 보인다"

    second.downloadState = DownloadState.FINISHED
    win.contentManager.emitFinishedRequest(second)
    _pump()

    assert win.contentManager.isWaitingOnEdit()
    assert "A download is waiting for this card" in shown(dialog.waitHintLabel)


def test_values_from_the_card_are_shown_bright_even_when_they_are_zero(qtbot, tmp_path, basis):
    """목록에서 온 값은 0이어도 친 값처럼 전부 밝게 보여야 한다 — 아무것도 치지 않은 칸과 구분된다.

    구간 (0, 3600) · (600, 1200)이 있는 카드를 열면 첫 행은 00:00:00:00 ~ 01:00:00:00
    -> 시작의 시분초 칸은 밝은 부분 "00:00:00" · 흐린 부분 "", 프레임 칸은 밝은 부분 "00" · 흐린 부분 ""
    두 칸을 Delete로 비움 -> 시분초 칸은 흐린 부분 "00:00:00", 프레임 칸은 흐린 부분 "00"
    """
    item = _make_item(str(tmp_path))
    item.selections = (TimeRange(0.0, HOUR), TimeRange(600.0, 1200.0))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    edit = dialog._rows[0].startEdit

    assert (edit.clockEdit.dimText(), edit.clockEdit.brightText()) == ("", "00:00:00")
    assert (edit.frameEdit.dimText(), edit.frameEdit.brightText()) == ("", "00")

    QTest.keyClick(edit.clockEdit, Qt.Key.Key_Delete)
    QTest.keyClick(edit.frameEdit, Qt.Key.Key_Delete)
    assert (edit.clockEdit.dimText(), edit.clockEdit.brightText()) == ("00:00:00", "")
    assert (edit.frameEdit.dimText(), edit.frameEdit.brightText()) == ("00", "")


# ================================================================ 영상의 끝 타임코드


def test_header_shows_the_end_of_the_video_only_after_the_lookup(qtbot, tmp_path, basis):
    """머리줄은 조회가 끝난 뒤에만 영상의 끝 타임코드를 보여야 한다.

    조회를 붙잡아 둔 동안 -> 끝 타임코드 "" (모르는 값을 보이지 않는다), 머리줄 숨김
    60fps · 3600초로 조회가 끝남 -> 머리줄 "Sections 1 / 20 · 60fps · video ends at 01:00:00:00",
    머리줄 툴팁(밀리초) "01:00:00.000"
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    basis.gate.clear()
    click_summary(win, item)
    dialog = win._sectionDialog
    assert dialog.viewModel().endTimecodeText() == ""
    assert not dialog.headerLabel.isVisible()

    basis.gate.set()
    qtbot.waitUntil(lambda: dialog.viewModel().state == "ready", timeout=3000)
    _pump()

    assert shown(dialog.headerLabel) == "Sections 1 / 20 · 60fps · video ends at 01:00:00:00"
    assert dialog.headerLabel.toolTip() == "01:00:00.000"


def test_no_end_is_shown_when_the_lookup_failed(qtbot, tmp_path, basis):
    """조회가 실패하면 영상의 끝 타임코드를 보이지 않아야 한다.

    조회 대역이 예외를 던짐 -> 끝 타임코드 "", 머리줄 숨김
    """
    basis.fail = True
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)

    dialog = open_editor(qtbot, win, item)

    assert dialog.viewModel().endTimecodeText() == ""
    assert not dialog.headerLabel.isVisible()


@pytest.mark.parametrize(
    "fps, duration, end",
    [
        (Fraction(60), 3600.0, "01:00:00:00"),  # 길이가 프레임 경계다
        (Fraction(60), 11524.5, "03:12:04:30"),  # 0.5초 = 30프레임
        (Fraction(60), 10.99, "00:00:10:59"),  # 659.4프레임 → 659번째 경계
        (Fraction(30), 3600.5, "01:00:00:15"),
        (Fraction(2997, 100), 10.02, "00:00:10:00"),  # 300.3프레임 → 300번째 = 10초 + 0.3프레임
        (Fraction(30), 360000.0, "100:00:00:00"),  # 100시간 — 시가 세 자리여도 깨지지 않는다
    ],
)
def test_the_shown_end_is_the_last_frame_boundary_of_the_looked_up_length(
    qtbot, tmp_path, basis, fps, duration, end
):
    """영상의 끝 타임코드는 조회한 프레임률 · 길이에서 길이를 넘지 않는 마지막 프레임 경계여야 한다.

    위 표의 프레임률 · 길이 -> 끝 타임코드가 표와 같고, 머리줄에 그 값이 들어 있다
    """
    basis.fps, basis.duration = fps, duration
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)

    dialog = open_editor(qtbot, win, item)

    assert dialog.viewModel().endTimecodeText() == end
    assert shown(dialog.headerLabel).endswith(f"video ends at {end}")


@pytest.mark.parametrize(
    "fps, duration, end_digits, one_more",
    [
        (Fraction(60), 10.99, "1059", "1100"),
        (Fraction(30), 3600.5, "01000015", "01000016"),
        (Fraction(2997, 100), 10.02, "1000", "1001"),
    ],
)
def test_typing_the_shown_end_reaches_the_end_and_one_frame_more_is_outside(
    qtbot, tmp_path, basis, fps, duration, end_digits, one_more
):
    """보인 끝 타임코드를 끝 칸에 치면 통과하고 전체 다운로드로 판정되며, 한 프레임 더하면 길이 초과여야 한다.

    위 표의 프레임률 · 길이에서 끝 칸에 끝 타임코드의 숫자를 침 -> 오류 없음, 확인하면 selections == ()
    한 프레임 뒤의 숫자를 침 -> "Selection is outside the video"
    """
    basis.fps, basis.duration = fps, duration
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    row = dialog._rows[0]

    type_into(row.endEdit, one_more)
    leave(row.endEdit)
    assert shown(row.errorLabel) == "Selection is outside the video"

    type_into(row.endEdit, end_digits)
    leave(row.endEdit)
    assert row.endEdit.text() == dialog.viewModel().endTimecodeText()
    assert not row.errorLabel.isVisible() and dialog.okButton.isEnabled()
    press_ok(dialog)
    assert item.selections == ()


def test_the_end_follows_the_looked_up_rate_not_the_declared_one(qtbot, tmp_path, basis):
    """영상의 끝 타임코드는 목록의 선언 프레임률이 아니라 조회한 프레임률로 정해야 한다.

    카드의 1080p 선언값 60fps, 조회값 30fps · 10.5초
    -> 끝 타임코드 "00:00:10:15"(30fps의 15프레임. 60fps로 계산하면 "00:00:10:30"이다)
    """
    basis.fps, basis.duration = Fraction(30), 10.5
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)

    dialog = open_editor(qtbot, win, item)

    assert dialog.viewModel().endTimecodeText() == "00:00:10:15"


# ================================================================ Enter


def test_enter_confirms_the_field_and_moves_on_without_closing_the_window(qtbot, tmp_path, basis):
    """시분초 칸에서 Enter를 치면 값을 확정하고 프레임 칸을 건너뛰어 다음 시각의 시분초 칸으로 가야 하며 창은 닫히지 않아야 한다.

    60fps. 행 둘. 첫째 행 시작 시분초 칸에 1000(10분)을 치고 Enter -> 포커스가 첫째 행 끝의 시분초 칸, 창 열림
    끝에서 Enter -> 둘째 행 시작의 시분초 칸. 둘째 행 시작 → 끝 → Enter -> 확인 버튼. 창은 계속 열려 있다
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    dialog.addButton.click()
    _pump()
    first, second = dialog._rows

    type_into(first.startEdit, "00:10:00:00")
    QTest.keyClick(first.startEdit.clockEdit, Qt.Key.Key_Return)
    _pump()
    assert dialog.focusWidget() is first.endEdit.clockEdit
    assert dialog.viewModel().rows[0][0] == "00:10:00:00", "Enter가 칸의 값을 확정하지 않았다"

    QTest.keyClick(first.endEdit.clockEdit, Qt.Key.Key_Enter)  # 숫자 키패드의 Enter
    _pump()
    assert dialog.focusWidget() is second.startEdit.clockEdit
    QTest.keyClick(second.startEdit.clockEdit, Qt.Key.Key_Return)
    _pump()
    assert dialog.focusWidget() is second.endEdit.clockEdit
    QTest.keyClick(second.endEdit.clockEdit, Qt.Key.Key_Return)
    _pump()

    assert dialog.focusWidget() is dialog.okButton
    assert win._sectionDialog is dialog and dialog.isVisible(), "Enter가 창을 닫았다"
    assert item.selections == ()


def test_enter_shows_the_errors_that_wait_for_the_field_to_be_left(qtbot, tmp_path, basis):
    """Enter는 칸을 떠날 때 띄우는 오류를 띄워야 한다.

    30fps. 끝 시분초 칸에 75(초 75)를 치고 Enter
    -> "Minutes and seconds must be below 60", 창은 열려 있다
    """
    basis.fps = Fraction(30)
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    row = dialog._rows[0]

    type_into(row.endEdit, "00:00:75:00")
    assert error_shown(row) == "", "전제: 치는 동안에는 띄우지 않는다"
    QTest.keyClick(row.endEdit.clockEdit, Qt.Key.Key_Return)
    _pump()

    assert shown(row.errorLabel) == "Minutes and seconds must be below 60"
    assert dialog.isVisible()


def test_the_window_closes_only_through_the_ok_button(qtbot, tmp_path, basis):
    """창은 확인 버튼으로만 닫혀야 한다 — 창이나 다른 버튼에 간 Enter는 닫지 않고, 확인 버튼에 포커스가 있을 때의 Enter는 닫는다.

    값이 유효한 창에서 창 자신과 구간 추가 버튼에 Enter를 보냄 -> 창 열림, 구간 그대로
    확인 버튼에 포커스를 두고 Enter를 보냄 -> 창 닫힘, 구간이 쓰인다
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    set_rows(dialog, [("00:10:00:00", "00:20:00:00")])
    assert not dialog.okButton.isDefault(), "기본 버튼이면 창 어디서든 Enter가 확인을 누른다"

    QTest.keyClick(dialog, Qt.Key.Key_Return)
    QTest.keyClick(dialog.addButton, Qt.Key.Key_Return)
    _pump()
    assert win._sectionDialog is dialog and dialog.isVisible()
    assert item.selections == ()

    dialog.okButton.setFocus()
    QTest.keyClick(dialog.okButton, Qt.Key.Key_Return)
    _pump()

    assert win._sectionDialog is None
    assert item.selections == (TimeRange(600.0, 1200.0),)


def test_escape_cancels_the_window(qtbot, tmp_path, basis):
    """Esc는 취소와 같이 창을 닫고 구간을 쓰지 않아야 한다.

    구간을 고친 창의 입력 칸에서 Esc -> 창 닫힘, selections == ()
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    set_rows(dialog, [("00:10:00:00", "00:20:00:00")])

    QTest.keyClick(dialog._rows[0].endEdit.clockEdit, Qt.Key.Key_Escape)
    _pump()

    assert win._sectionDialog is None and item.selections == ()


# ================================================================ 완료 카드의 폴더 열기


@pytest.fixture
def opened(monkeypatch):
    """폴더 열기가 OS에 넘기는 것을 적어 둔다 — 탐색기 · 파일 관리자를 실제로 띄우지 않는다."""
    import app.widgets.widget as widget_module

    calls: list = []

    def detached(program, arguments):
        calls.append(("select", arguments[-1]))
        return True

    def open_url(url):
        calls.append(("open", url.toLocalFile()))
        return True

    monkeypatch.setattr(widget_module.QProcess, "startDetached", staticmethod(detached))
    monkeypatch.setattr(widget_module.QDesktopServices, "openUrl", staticmethod(open_url))
    monkeypatch.setattr(
        widget_module.QMessageBox, "warning", lambda *a, **k: calls.append("warning")
    )
    return calls


def finished_section_card(qtbot, tmp_path, basis, service):
    """구간 셋을 모두 받아 완료된 카드 — 구간 파일 `_1` · `_2` · `_3`이 디스크에 있다."""
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    engine = service(win)
    give_sections(
        qtbot,
        win,
        item,
        [
            ("00:10:00:00", "00:20:00:00"),
            ("00:30:00:00", "00:40:00:00"),
            ("00:50:00:00", "00:55:00:00"),
        ],
    )
    start_batch(win)
    # 완료 상태는 실제로는 서비스가 완료를 알리기 전에 태스크 모델을 거쳐 카드에 옮긴다 —
    # 대역에서는 직접 둔다. 알린 뒤에 두면 배치가 같은 카드를 한 번 더 집어 간다
    item.downloadState = DownloadState.FINISHED
    engine.finish()
    _pump()
    win.contentManager.model.notifyChanged(item)
    _pump()
    return win, item, engine.submissions[0]["content"].selection_paths


def _same_file(first: str, second: str) -> bool:
    return os.path.normcase(os.path.normpath(first)) == os.path.normcase(os.path.normpath(second))


def test_folder_button_of_a_section_card_selects_the_lowest_numbered_file_that_exists(
    qtbot, tmp_path, basis, service, opened
):
    """구간 카드의 폴더 버튼은 실제로 있는 구간 파일 가운데 번호가 가장 작은 것을 선택한 채 폴더를 열어야 한다.

    구간 파일 `_1` · `_2` · `_3`이 있는 완료 카드에서 폴더 버튼 -> `_1`을 선택
    `_1`을 지우고 다시 누름 -> `_2`를 선택. 전체 다운로드 이름("제목 1080p.mp4")은 쓰지 않는다
    """
    win, item, paths = finished_section_card(qtbot, tmp_path, basis, service)
    button = win.listView.widgetFor(item).openDirectoryButton
    shown_button = button.isVisible()
    assert shown_button, "전제: 완료 카드에 폴더 버튼이 보여야 한다"

    button.click()
    os.remove(paths[0])
    button.click()

    assert [kind for kind, _ in opened] == ["select", "select"], opened
    assert _same_file(opened[0][1], paths[0]) and _same_file(opened[1][1], paths[1])
    assert not any(_same_file(path, item.output_path) for _, path in opened)


def test_folder_button_opens_the_download_folder_when_no_section_file_is_left(
    qtbot, tmp_path, basis, service, opened
):
    """구간 파일이 하나도 없으면 폴더 버튼은 경고 없이 저장 폴더를 열어야 한다.

    완료 카드의 구간 파일 셋을 모두 지우고 폴더 버튼 -> 저장 폴더를 연다, 경고 없음
    """
    win, item, paths = finished_section_card(qtbot, tmp_path, basis, service)
    for path in paths:
        os.remove(path)

    win.listView.widgetFor(item).openDirectoryButton.click()

    assert len(opened) == 1 and opened[0][0] == "open"
    assert _same_file(opened[0][1], str(tmp_path))


def test_folder_button_of_a_whole_download_card_still_selects_its_output_file(
    qtbot, tmp_path, basis, opened
):
    """구간 없는 카드의 폴더 버튼은 전과 같이 산출물 파일을 선택한 채 폴더를 열어야 한다.

    산출물 파일("제목 1080p.mp4")이 있는 완료 카드에서 폴더 버튼 -> 그 파일을 선택
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    item.output_path = os.path.join(str(tmp_path), "제목 1080p.mp4")
    with open(item.output_path, "wb") as file:
        file.write(b"whole")
    item.downloadState = DownloadState.FINISHED
    win.contentManager.model.notifyChanged(item)
    _pump()

    win.listView.widgetFor(item).openDirectoryButton.click()

    assert len(opened) == 1 and opened[0][0] == "select"
    assert _same_file(opened[0][1], item.output_path)


# ================================================================ 시분초 칸 + 프레임 칸 (#309)


def test_a_row_has_a_clock_and_a_frame_field_for_each_time_with_a_tilde_between(
    qtbot, tmp_path, basis
):
    """한 행은 [시작 시분초][시작 프레임] ~ [끝 시분초][끝 프레임] 순서로 놓여야 한다.

    편집 창의 첫 행 -> 네 칸과 "~"가 보이고, 왼쪽 끝의 x가 그 순서로 커진다
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    row = dialog._rows[0]

    widgets = [
        row.startEdit.clockEdit,
        row.startEdit.frameEdit,
        row.rangeLabel,
        row.endEdit.clockEdit,
        row.endEdit.frameEdit,
    ]
    assert all(widget.isVisible() for widget in widgets)
    lefts = [widget.mapTo(row, widget.rect().topLeft()).x() for widget in widgets]
    assert lefts == sorted(lefts) and len(set(lefts)) == len(lefts)
    assert shown(row.rangeLabel) == "~"


def test_one_minute_typed_as_0100_reaches_the_card_as_sixty_seconds(qtbot, tmp_path, basis):
    """시분초 칸에 0100을 치면 1분이어야 한다 — 카드에 60초로 쓰인다.

    60fps. 시작 시분초 칸에 0100, 끝 시분초 칸에 012345 · 프레임 칸에 30을 치고 확인
    -> 시작 "00:01:00:00", 끝 "01:23:45:30", selections == ((60.0, 5025.5),)
    """
    basis.duration = 7200.0
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    row = dialog._rows[0]

    type_into(row.startEdit, "")
    type_clock(row.startEdit, "0100")
    leave(row.startEdit)
    type_into(row.endEdit, "")
    type_clock(row.endEdit, "012345")
    type_frame(row.endEdit, "30")
    leave(row.endEdit)

    assert (row.startEdit.text(), row.endEdit.text()) == ("00:01:00:00", "01:23:45:30")
    press_ok(dialog)
    assert item.selections == (TimeRange(60.0, 5025.5),)


def test_tab_walks_the_four_fields_of_a_row_in_order(qtbot, tmp_path, basis):
    """Tab은 시작 시분초 → 시작 프레임 → 끝 시분초 → 끝 프레임 순서로 가고 Shift+Tab은 거꾸로 가야 한다.

    첫 행의 시작 시분초 칸에서 Tab을 세 번, 이어 Shift+Tab을 세 번
    -> 포커스: 시작 프레임 → 끝 시분초 → 끝 프레임 → 끝 시분초 → 시작 프레임 → 시작 시분초
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    row = dialog._rows[0]
    dialog.activateWindow()
    row.startEdit.clockEdit.setFocus()
    _pump()
    seen = []

    for _ in range(3):
        QTest.keyClick(dialog.focusWidget(), Qt.Key.Key_Tab)
        _pump()
        seen.append(dialog.focusWidget())
    for _ in range(3):
        QTest.keyClick(dialog.focusWidget(), Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
        _pump()
        seen.append(dialog.focusWidget())

    assert seen == [
        row.startEdit.frameEdit,
        row.endEdit.clockEdit,
        row.endEdit.frameEdit,
        row.endEdit.clockEdit,
        row.startEdit.frameEdit,
        row.startEdit.clockEdit,
    ]


def test_enter_in_a_frame_field_goes_to_the_next_clock_field(qtbot, tmp_path, basis):
    """프레임 칸에서 Enter를 치면 다음 시각의 시분초 칸으로 가고, 마지막 끝의 프레임 칸에서는 확인 버튼으로 가야 한다.

    행 하나. 시작 프레임 칸에서 Enter -> 끝 시분초 칸. 끝 프레임 칸에서 Enter -> 확인 버튼. 창은 열려 있다
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    row = dialog._rows[0]
    dialog.activateWindow()
    row.startEdit.frameEdit.setFocus()
    _pump()

    QTest.keyClick(row.startEdit.frameEdit, Qt.Key.Key_Return)
    _pump()
    first = dialog.focusWidget()
    row.endEdit.frameEdit.setFocus()
    _pump()
    QTest.keyClick(row.endEdit.frameEdit, Qt.Key.Key_Return)
    _pump()

    assert first is row.endEdit.clockEdit
    assert dialog.focusWidget() is dialog.okButton
    assert dialog.isVisible()


def test_period_moves_from_the_clock_field_to_the_frame_field_of_the_same_time(
    qtbot, tmp_path, basis
):
    """시분초 칸에서 "."을 누르면 같은 시각의 프레임 칸으로 가고, 이어 친 숫자가 프레임이 되어야 한다.

    끝 시분초 칸에 1000 · "." · 30
    -> "."을 누른 뒤 포커스가 끝 프레임 칸, 끝의 값 "00:10:00:30"
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    row = dialog._rows[0]
    dialog.activateWindow()
    row.endEdit.clockEdit.setFocus()
    _pump()

    QTest.keyClicks(row.endEdit.clockEdit, "1000")
    QTest.keyClick(row.endEdit.clockEdit, Qt.Key.Key_Period)
    _pump()
    moved_to = dialog.focusWidget()
    QTest.keyClicks(row.endEdit.frameEdit, "30")
    _pump()

    assert moved_to is row.endEdit.frameEdit
    assert row.endEdit.text() == "00:10:00:30"


def test_end_timecode_of_the_header_typed_into_both_fields_reaches_the_end(qtbot, tmp_path, basis):
    """머리줄의 끝 타임코드를 시분초 칸과 프레임 칸에 나눠 치면 영상 끝까지 받는 구간이어야 한다.

    60fps · 길이 100.25초(끝 타임코드 00:01:40:15). 끝 시분초 칸에 140, 프레임 칸에 15를 치고 확인
    -> 오류 없음, 카드의 구간 끝 == 100.25(영상 길이)
    """
    basis.duration = 100.25
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    row = dialog._rows[0]
    assert dialog.viewModel().endTimecodeText() == "00:01:40:15", "전제: 머리줄의 끝 타임코드"
    type_into(row.startEdit, "00:00:10:00")
    leave(row.startEdit)

    type_into(row.endEdit, "")
    type_clock(row.endEdit, "140")
    type_frame(row.endEdit, "15")
    leave(row.endEdit)

    assert error_shown(row) == "" and dialog.okButton.isEnabled()
    press_ok(dialog)
    assert item.selections == (TimeRange(10.0, 100.25),)


def test_frame_change_that_breaks_the_whole_time_follows_the_timing_table(qtbot, tmp_path, basis):
    """프레임 칸의 값이 바뀌어 생긴 시각 전체의 오류도 표를 따라야 한다 — 끝이 시작에 못 미치는 것은 떠날 때 띄운다.

    60fps. 시작 00:10:00:30, 끝 시분초 00:10:00인 행의 끝 프레임 칸에 2 · 0을 침(00:10:00:20 < 시작)
    -> 치는 동안 오류 문구 없음, 확인 꺼짐. 끝을 떠나면 "Start must be before end", 네 칸 모두 강조
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    row = dialog._rows[0]
    type_into(row.startEdit, "00:10:00:30")
    leave(row.startEdit)
    type_into(row.endEdit, "00:10:00:00")

    type_frame(row.endEdit, "20")
    assert row.endEdit.text() == "00:10:00:20"
    assert error_shown(row) == "" and not dialog.okButton.isEnabled()

    leave(row.endEdit)

    assert error_shown(row) == "Start must be before end"
    assert flagged_fields(row) == ALL_FOUR


# ================================================================ 가운데 값의 방향 · 원래 값에서 다시 맞추기 (#309)


def _range(first: int, last: int, rate: Fraction) -> TimeRange:
    """그 프레임률의 프레임 번호 둘로 만든 구간 — 시각은 번호 ÷ 프레임률을 float로 바꾼 값."""
    return TimeRange(float(Fraction(first) / rate), float(Fraction(last) / rate))


@pytest.mark.parametrize(
    ("old", "new"),
    [(Fraction(60), Fraction(30)), (Fraction(60000, 1001), Fraction(30000, 1001))],
    ids=["60-30", "59.94-29.97"],
)
@pytest.mark.parametrize(
    ("first", "last", "expected"),
    [
        (31, 91, (15, 46)),  # 시작 31 → 15(앞), 끝 91 → 46(뒤)
        (33, 93, (16, 47)),  # 시작 33 → 16(앞), 끝 93 → 47(뒤)
        (10, 31, (5, 16)),  # 끝 31 → 16
        (10, 33, (5, 17)),  # 끝 33 → 17
        (30, 90, (15, 45)),  # 가운데가 아니면 그 프레임 그대로
    ],
)
def test_refit_sends_an_exact_midpoint_outwards(old, new, first, last, expected):
    """새 프레임률의 두 프레임 정확히 가운데에 오는 시각은 시작이면 앞 프레임, 끝이면 뒤 프레임으로 가야 한다.

    60fps(또는 59.94fps)의 프레임 번호 (first, last)로 만든 구간을 절반의 프레임률에 맞춤
    -> 새 프레임 번호 == expected (홀수 번호는 시작이면 내림, 끝이면 올림)
    """
    (refit,) = refit_selections([_range(first, last, old)], new)

    assert (round(Fraction(refit.start) * new), round(Fraction(refit.end) * new)) == expected
    assert refit == _range(*expected, new)


def test_refit_to_a_doubled_frame_rate_is_exactly_twice_the_frame_number():
    """30fps에서 60fps로 맞추면 프레임 번호가 정확히 두 배여야 한다 — 가운데가 생기지 않는다.

    30fps의 프레임 15~46 -> 60fps의 프레임 30~92
    """
    (refit,) = refit_selections([_range(15, 46, Fraction(30))], Fraction(60))

    assert refit == _range(30, 92, Fraction(60))


def test_refit_picks_the_nearest_frame_when_not_at_a_midpoint():
    """가운데가 아닌 시각은 시작이든 끝이든 가장 가까운 프레임으로 가야 한다.

    60fps의 프레임 7~101을 24fps에 맞춤(7/60초 = 24fps의 2.8번째, 101/60초 = 40.4번째)
    -> 24fps의 프레임 3~40
    """
    (refit,) = refit_selections([_range(7, 101, Fraction(60))], Fraction(24))

    assert refit == _range(3, 40, Fraction(24))


def test_going_back_to_the_original_frame_rate_restores_the_original_sections(
    qtbot, tmp_path, basis
):
    """해상도를 바꿨다가 원래 프레임률로 돌아오면 사용자가 확정한 원래 구간이 그대로 나와야 한다.

    60fps에서 홀수 프레임의 구간(00:00:10:31 ~ 00:00:20:01)을 확인 → 480p(30fps) → 1080p(60fps)
    -> 30fps에서는 30fps의 프레임 경계, 돌아온 뒤 selections == 처음 확정한 구간(31번 프레임은 31번)
    """
    basis.by_resolution[480] = (Fraction(30), HOUR)
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    original = give_sections(qtbot, win, item, ODD)

    _pick(win, item, 480)
    settle(qtbot, win)
    at_thirty = item.selections
    _pick(win, item, 1080)
    settle(qtbot, win)

    assert at_thirty != original and _on_grid(at_thirty[0].start, 30)
    assert item.selections == original
    assert notice_of(win, item) == ""


def test_changing_twice_refits_from_the_original_not_from_the_last_result(qtbot, tmp_path, basis):
    """해상도를 연달아 바꿔도 결과는 원래 구간을 지금의 프레임률에 한 번 맞춘 것이어야 한다.

    60fps에서 00:00:10:31 ~ 00:00:20:01을 확인 → 480p(30fps) → 1080p(60fps) → 480p(30fps)
    -> 마지막 selections == 첫 480p에서의 selections == (10.5초, 601/30초)
       (시작 631번은 가운데라 앞 프레임 315, 끝 1201번은 가운데라 뒤 프레임 601)
    """
    basis.by_resolution[480] = (Fraction(30), HOUR)
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    give_sections(qtbot, win, item, ODD)

    seen = []
    for resolution in (480, 1080, 480):
        _pick(win, item, resolution)
        settle(qtbot, win)
        seen.append(item.selections)

    expected = (TimeRange(10.5, float(Fraction(601, 30))),)
    assert seen[0] == expected
    assert seen[2] == expected


def test_editing_after_a_change_makes_the_edit_the_new_original(qtbot, tmp_path, basis):
    """해상도를 바꾼 뒤 구간을 다시 확정하면 그 값이 새 원래 값이 되어야 한다.

    60fps에서 구간을 확인 → 480p(30fps)에서 편집 창을 열어 00:00:05:07 ~ 00:00:09:11로 확정
    → 1080p(60fps) → 480p(30fps)
    -> 60fps에서는 프레임 번호가 정확히 두 배(5초 14프레임 ~ 9초 22프레임),
       30fps로 돌아오면 selections == 480p에서 확정한 구간
    """
    basis.by_resolution[480] = (Fraction(30), HOUR)
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    give_sections(qtbot, win, item, ODD)
    _pick(win, item, 480)
    settle(qtbot, win)
    edited = give_sections(qtbot, win, item, [("00:00:05:07", "00:00:09:11")])
    assert edited == (_range(157, 281, Fraction(30)),), "전제: 30fps의 프레임으로 확정됐다"

    _pick(win, item, 1080)
    settle(qtbot, win)
    at_sixty = item.selections
    _pick(win, item, 480)
    settle(qtbot, win)

    assert at_sixty == (_range(314, 562, Fraction(60)),)
    assert item.selections == edited


def test_a_section_pulled_to_a_shorter_end_gets_its_end_back(qtbot, tmp_path, basis):
    """더 짧은 해상도에서 끝이 당겨진 구간도 원래 해상도로 돌아오면 원래 끝으로 돌아가야 한다.

    60fps · 3600초에서 00:10:00:00 ~ 01:00:00:00(영상 끝)을 확인 → 480p(30fps · 3590초) → 1080p
    -> 480p에서는 끝이 3590초로 당겨지고 알림이 붙는다, 돌아온 뒤 끝 == 3600초 · 알림 없음
    """
    basis.by_resolution[480] = (Fraction(30), 3590.0)
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    original = give_sections(qtbot, win, item, [("00:10:00:00", "01:00:00:00")])

    _pick(win, item, 480)
    settle(qtbot, win)
    pulled = item.selections[0].end
    was_marked = item.section_end_pulled
    _pick(win, item, 1080)
    settle(qtbot, win)

    assert (pulled, was_marked) == (3590.0, True)
    assert item.selections == original
    assert not item.section_end_pulled and not item.section_end_extended


# ================================================================ 빈 행 · 빈 시각 (#309)


def texts(dialog) -> list[tuple[str, str]]:
    return [(row.startEdit.text(), row.endEdit.text()) for row in dialog._rows]


def notes(dialog) -> list[str]:
    """행마다 보이는 무시 안내 — 보이지 않으면 빈 글."""
    return [shown(row.noteLabel) if row.noteLabel.isVisible() else "" for row in dialog._rows]


def numbers(dialog) -> list[str]:
    return [row.numberLabel.text() for row in dialog._rows]


def delete_enabled(dialog) -> list[bool]:
    return [row.deleteButton.isEnabled() for row in dialog._rows]


IGNORED = "Empty — this row is ignored"


def test_a_card_without_sections_opens_with_one_empty_row_that_confirms_as_a_whole_download(
    qtbot, tmp_path, basis
):
    """구간 없는 카드를 열면 빈 행 하나가 보이고, 그대로 확인하면 빈 튜플이어야 한다.

    60fps · 3600초, 구간 없는 카드
    -> 행 1개 ("", ""), 네 칸 모두 친 숫자 없음, 안내 없음, 번호 "1", 확인 켜짐
    확인 -> selections == ()
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)

    row = dialog._rows[0]
    assert texts(dialog) == [("", "")]
    assert [
        part.digits()
        for edit in (row.startEdit, row.endEdit)
        for part in (edit.clockEdit, edit.frameEdit)
    ] == ["", "", "", ""]
    assert notes(dialog) == [""] and numbers(dialog) == ["1"]
    assert dialog.okButton.isEnabled()
    press_ok(dialog)

    assert item.selections == ()


def test_the_only_row_cannot_be_deleted_and_no_action_leaves_zero_rows(qtbot, tmp_path, basis):
    """행이 하나면 삭제 버튼이 꺼져 있고, 어떤 조작으로도 행이 0개가 되지 않아야 한다.

    행 1개 -> 삭제 꺼짐. 뷰모델에 직접 삭제를 청해도 1행
    구간 추가 -> 2행, 둘 다 삭제 켜짐. 첫 행 삭제 -> 1행, 삭제 꺼짐. 다시 삭제를 청해도 1행
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    viewmodel = dialog.viewModel()

    assert delete_enabled(dialog) == [False]
    viewmodel.removeRow(0)
    _pump()
    assert len(dialog._rows) == 1 and len(viewmodel.rows) == 1

    dialog.addButton.click()
    _pump()
    assert delete_enabled(dialog) == [True, True]

    dialog._rows[0].deleteButton.click()
    _pump()
    assert delete_enabled(dialog) == [False]
    dialog._rows[0].deleteButton.click()
    viewmodel.removeRow(0)
    _pump()
    assert len(dialog._rows) == 1 and len(viewmodel.rows) == 1


def test_adding_a_section_gives_an_empty_row_and_focuses_its_start_clock_field(
    qtbot, tmp_path, basis
):
    """구간 추가는 빈 행을 끝에 넣고 그 행의 시작 시분초 칸에 포커스를 줘야 한다.

    첫 행에 10분~20분을 넣고 구간 추가
    -> 2행, 둘째 행 ("", ""), 포커스 == 둘째 행의 시작 시분초 칸
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    set_rows(dialog, [("00:10:00:00", "00:20:00:00")])

    dialog.addButton.click()
    _pump()

    assert texts(dialog) == [("00:10:00:00", "00:20:00:00"), ("", "")]
    assert QApplication.focusWidget() is dialog._rows[1].startEdit.clockEdit


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        ("00:10:00:00", "", TimeRange(600.0, HOUR)),  # 시작만 — 그 시각부터 끝까지
        ("", "00:20:00:00", TimeRange(0.0, 1200.0)),  # 끝만 — 처음부터 그 시각까지
    ],
)
def test_an_empty_start_means_the_beginning_and_an_empty_end_means_the_end(
    qtbot, tmp_path, basis, start, end, expected
):
    """빈 시작은 영상 맨 처음, 빈 끝은 영상 맨 끝이어야 한다.

    60fps · 3600초. 한 행의 시작만 00:10:00:00 / 끝만 00:20:00:00
    -> selections == ((600, 3600),) / ((0, 1200),), 오류 없음
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    row = dialog._rows[0]

    for edit, text in ((row.startEdit, start), (row.endEdit, end)):
        if text:
            type_into(edit, text)
            leave(edit)
    assert not row.errorLabel.isVisible()
    press_ok(dialog)

    assert item.selections == (expected,)


def test_a_frame_typed_under_an_empty_clock_field_counts_from_zero(qtbot, tmp_path, basis):
    """시분초 칸이 빈 채 프레임 칸만 치면 그 시각은 00:00:00에 그 프레임이어야 한다.

    60fps. 끝의 프레임 칸에만 30을 침(시분초 칸은 치지 않음)
    -> 끝의 값 "00:00:00:30", 끝 시분초 칸의 흐린 글 "00:00:00"(영상 끝이 아니다)
    확인 -> selections == ((0, 0.5),)
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    end = dialog._rows[0].endEdit

    type_frame(end, "30")

    assert end.text() == "00:00:00:30"
    assert end.clockEdit.dimText() == "00:00:00"
    leave(end)
    press_ok(dialog)
    assert item.selections == (TimeRange(0.0, 0.5),)


def test_three_empty_rows_keep_the_top_one_and_confirm_as_a_whole_download(qtbot, tmp_path, basis):
    """모든 행이 비어 있으면 맨 위 행만 남고 나머지는 안내와 함께 걸러져야 한다.

    빈 행 셋
    -> 안내는 둘째 · 셋째 행에만, 번호 "1" · "" · "", 오류 없음, 머리줄 "Sections 1 / 20 …", 확인 켜짐
    확인 -> selections == (). 다시 열면 빈 행 하나
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    for _ in range(2):
        dialog.addButton.click()
        _pump()

    assert notes(dialog) == ["", IGNORED, IGNORED]
    assert numbers(dialog) == ["1", "", ""]
    assert not any(row.errorLabel.isVisible() for row in dialog._rows)
    assert shown(dialog.headerLabel).startswith("Sections 1 / 20 ")
    assert dialog.okButton.isEnabled()
    press_ok(dialog)
    assert item.selections == ()

    dialog = open_editor(qtbot, win, item)
    assert texts(dialog) == [("", "")]


def test_an_empty_row_beside_a_filled_row_is_left_out(qtbot, tmp_path, basis):
    """빈 행과 입력한 행이 함께 있으면 입력한 행만 구간이 되고 안내는 빈 행에만 보여야 한다.

    첫 행은 빈 채, 둘째 행에 10분~20분
    -> 안내 [IGNORED, ""], 번호 ["", "1"], 머리줄 "Sections 1 / 20 …"
    확인 -> selections == ((600, 1200),). 다시 열면 그 한 행뿐
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    dialog.addButton.click()
    _pump()
    for edit, text in (
        (dialog._rows[1].startEdit, "00:10:00:00"),
        (dialog._rows[1].endEdit, "00:20:00:00"),
    ):
        type_into(edit, text)
        leave(edit)

    assert notes(dialog) == [IGNORED, ""]
    assert numbers(dialog) == ["", "1"]
    assert shown(dialog.headerLabel).startswith("Sections 1 / 20 ")
    press_ok(dialog)
    assert item.selections == (TimeRange(600.0, 1200.0),)

    dialog = open_editor(qtbot, win, item)
    assert texts(dialog) == [("00:10:00:00", "00:20:00:00")]


def test_rows_around_an_empty_row_are_numbered_one_and_two(qtbot, tmp_path, basis, service):
    """입력한 행 사이의 빈 행은 번호를 받지 않고, 남은 행의 파일 번호가 _1 · _2로 이어져야 한다.

    행: 10분~20분 · 빈 행 · 30분~31분. 확인하고 받기 시작
    -> 번호 ["1", "", "2"], selections == ((600, 1200), (1800, 1860)),
       파일 이름 == "제목 1080p_1.mp4" · "제목 1080p_2.mp4"
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    engine = service(win)
    dialog = open_editor(qtbot, win, item)
    for _ in range(2):
        dialog.addButton.click()
        _pump()
    for index, (start, end) in (
        (0, ("00:10:00:00", "00:20:00:00")),
        (2, ("00:30:00:00", "00:31:00:00")),
    ):
        for edit, text in (
            (dialog._rows[index].startEdit, start),
            (dialog._rows[index].endEdit, end),
        ):
            type_into(edit, text)
            leave(edit)

    assert numbers(dialog) == ["1", "", "2"]
    press_ok(dialog)
    assert item.selections == (TimeRange(600.0, 1200.0), TimeRange(1800.0, 1860.0))
    start_batch(win)

    assert names(engine.submissions[0]["content"].selection_paths) == [
        "제목 1080p_1.mp4",
        "제목 1080p_2.mp4",
    ]


def test_two_rows_with_only_the_same_start_are_duplicates(qtbot, tmp_path, basis):
    """일부만 빈 행은 걸러지지 않아, 시작만 같은 값인 두 행은 중복 오류여야 한다.

    두 행 모두 시작만 00:10:00:00(끝은 빈 채)
    -> 두 행 모두 "Duplicate selection", 안내 없음, 확인 꺼짐
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    dialog.addButton.click()
    _pump()
    for row in dialog._rows:
        type_into(row.startEdit, "00:10:00:00")
        leave(row.startEdit)

    assert [shown(row.errorLabel) for row in dialog._rows] == ["Duplicate selection"] * 2
    assert notes(dialog) == ["", ""]
    assert not dialog.okButton.isEnabled()


def test_a_typed_whole_video_row_beside_another_row_is_a_section(qtbot, tmp_path, basis):
    """직접 00:00:00:00 ~ 끝을 친 행은 빈 행이 아니어서, 다른 행과 함께 있으면 구간으로 들어가야 한다.

    행: 직접 친 00:00:00:00~01:00:00:00 · 10분~20분
    -> 안내 없음, selections == ((0, 3600), (600, 1200))
    다시 열면 첫 행이 값 그대로 보인다(비워 보이지 않는다) — 다시 확인해도 두 구간이 남는다
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    set_rows(dialog, [("00:00:00:00", "01:00:00:00"), ("00:10:00:00", "00:20:00:00")])

    assert notes(dialog) == ["", ""]
    press_ok(dialog)
    expected = (TimeRange(0.0, HOUR), TimeRange(600.0, 1200.0))
    assert item.selections == expected

    dialog = open_editor(qtbot, win, item)
    assert texts(dialog) == [("00:00:00:00", "01:00:00:00"), ("00:10:00:00", "00:20:00:00")]
    press_ok(dialog)
    assert item.selections == expected


def test_the_empty_end_field_shows_the_end_of_the_video_dimmed(qtbot, tmp_path, basis):
    """빈 끝 칸에는 영상의 끝 타임코드가, 빈 시작 칸에는 00:00:00이 흐리게 보여야 한다.

    60fps · 3599.5초(끝 타임코드 00:59:59:30)인 카드의 빈 행
    -> 끝의 흐린 글: 시분초 "00:59:59" · 프레임 "30", 시작의 흐린 글: "00:00:00" · "00", 밝은 부분 없음
    """
    basis.duration = 3599.5
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    row = dialog._rows[0]

    assert dialog.viewModel().endTimecodeText() == "00:59:59:30", "전제: 끝 타임코드"
    assert (row.endEdit.clockEdit.dimText(), row.endEdit.frameEdit.dimText()) == ("00:59:59", "30")
    assert (row.startEdit.clockEdit.dimText(), row.startEdit.frameEdit.dimText()) == (
        "00:00:00",
        "00",
    )
    assert [
        part.brightText()
        for edit in (row.startEdit, row.endEdit)
        for part in (edit.clockEdit, edit.frameEdit)
    ] == ["", "", "", ""]


def test_copying_from_an_empty_field_gives_the_value_it_stands_for(
    qtbot, tmp_path, basis, monkeypatch
):
    """빈 칸에서 복사하면 그 칸이 뜻하는 값이 클립보드에 들어가야 한다.

    60fps · 3600초의 빈 행. 시작 칸 · 끝 칸에서 차례로 복사
    -> "00:00:00:00", "01:00:00:00"
    """
    copied: list[str] = []

    class _Clipboard:
        def setText(self, text: str) -> None:
            copied.append(text)

        def text(self) -> str:
            return ""

    monkeypatch.setattr(
        "app.widgets.timecode_edit.QGuiApplication.clipboard", staticmethod(lambda: _Clipboard())
    )
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    row = dialog._rows[0]

    row.startEdit.clockEdit.copy()
    row.endEdit.frameEdit.copy()

    assert copied == ["00:00:00:00", "01:00:00:00"]


def test_times_at_the_beginning_and_the_end_are_shown_as_empty_fields(qtbot, tmp_path, basis):
    """구간이 있는 카드를 열 때 영상 맨 처음과 같은 시작 · 맨 끝과 같은 끝은 빈 칸으로 보여야 한다.

    60fps · 3600초, selections = ((0, 1200), (1800, 3600))
    -> 행 ("", "00:20:00:00"), ("00:30:00:00", "")
    그대로 확인 -> selections 그대로
    """
    item = _make_item(str(tmp_path))
    sections = (TimeRange(0.0, 1200.0), TimeRange(1800.0, HOUR))
    item.selections = sections
    win = open_window(tmp_path, item)

    dialog = open_editor(qtbot, win, item)

    assert texts(dialog) == [("", "00:20:00:00"), ("00:30:00:00", "")]
    press_ok(dialog)
    assert item.selections == sections


def test_a_section_with_an_empty_end_follows_the_new_end_after_a_resolution_change(
    qtbot, tmp_path, basis
):
    """빈 끝으로 정한 구간은 해상도를 바꾼 뒤 새 영상의 끝을 따라가야 한다.

    1080p(60fps · 3600초)에서 시작만 00:10:00:00으로 확인 → 480p(30fps · 3700초)를 고름
    -> 조회 뒤 selections == ((600, 3700),)
    """
    basis.by_resolution[480] = (Fraction(30), 3700.0)
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    type_into(dialog._rows[0].startEdit, "00:10:00:00")
    leave(dialog._rows[0].startEdit)
    press_ok(dialog)
    assert item.selections == (TimeRange(600.0, HOUR),), "전제: 빈 끝이 영상 끝으로 쓰였다"

    _pick(win, item, 480)
    settle(qtbot, win)

    assert item.selections == (TimeRange(600.0, 3700.0),)


def test_moving_a_row_while_a_field_has_focus_does_not_overwrite_the_row_that_took_its_place(
    qtbot, tmp_path, basis
):
    """칸에 포커스가 있는 채로 행의 순서를 바꿔도 그 자리에 온 행의 값이 덮이지 않아야 한다.

    행 A(10분~20분) · B(30분~31분). B의 시작 시분초 칸에 포커스를 두고 B의 ▲
    -> 행 B · A, 값 그대로
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    set_rows(dialog, [("00:10:00:00", "00:20:00:00"), ("00:30:00:00", "00:31:00:00")])
    dialog._rows[1].startEdit.clockEdit.setFocus()
    _pump()
    assert QApplication.focusWidget() is dialog._rows[1].startEdit.clockEdit, "전제: 포커스"

    dialog._rows[1].upButton.click()
    _pump()

    assert dialog.viewModel().rows == [
        ["00:30:00:00", "00:31:00:00"],
        ["00:10:00:00", "00:20:00:00"],
    ]


# ================================================================ 레이아웃 고정 — 스크롤바 · 안내 줄 (#309)


def _spots(dialog) -> list[tuple[int, int]]:
    """첫 행과 그 칸 · 삭제 버튼의 (창 기준 x, 폭)."""
    row = dialog._rows[0]
    widgets = (
        row,
        row.startEdit.clockEdit,
        row.startEdit.frameEdit,
        row.endEdit.clockEdit,
        row.endEdit.frameEdit,
        row.deleteButton,
    )
    return [(widget.mapTo(dialog, QPoint(0, 0)).x(), widget.width()) for widget in widgets]


def test_rows_keep_their_place_when_the_scroll_bar_appears_and_disappears(qtbot, tmp_path, basis):
    """구간 목록의 스크롤바가 생기고 사라져도 행의 폭과 칸의 자리가 같아야 한다.

    창 560x420. 행 1개(스크롤바 없음) → 스크롤바가 보일 때까지 구간 추가 → 사라질 때까지 삭제
    -> 세 시점의 첫 행 · 네 칸 · 삭제 버튼의 (x, 폭)이 모두 같다
    -> 스크롤바가 보이는 동안 행 컨테이너의 오른쪽 여백 == 없을 때의 여백 − 스크롤바 폭
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    dialog.resize(560, 420)
    _pump()
    bar = dialog.scrollArea.verticalScrollBar()
    assert not bar.isVisible(), "전제: 행 하나는 스크롤 없이 보인다"
    without = _spots(dialog)
    margin_without = dialog._rowLayout.contentsMargins().right()

    while not bar.isVisible() and dialog.addButton.isEnabled():
        dialog.addButton.click()
        _pump()
    assert bar.isVisible(), "전제: 행이 늘어 스크롤바가 생겼다"
    with_bar = _spots(dialog)
    margin_with = dialog._rowLayout.contentsMargins().right()

    while bar.isVisible():
        dialog._rows[-1].deleteButton.click()
        _pump()
    again = _spots(dialog)

    assert with_bar == without
    assert again == without
    assert bar.width() > 0 and margin_with == margin_without - bar.width()
    assert dialog._rowLayout.contentsMargins().right() == margin_without


def test_row_height_is_the_same_with_and_without_a_message(qtbot, tmp_path, basis):
    """안내 줄은 글이 없어도 한 줄을 차지해, 오류 · 무시 안내가 생기고 사라져도 행의 높이가 같아야 한다.

    행 A(10분~20분) · B(30분~31분). 오류 없음 → A의 시작을 50분으로(시작이 끝보다 뒤) → 되돌림 →
    빈 행을 하나 더함(무시 안내)
    -> A의 높이가 네 시점에 같고, 빈 행의 높이도 같다. 글이 없을 때 안내 줄의 높이 > 0
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    set_rows(dialog, [("00:10:00:00", "00:20:00:00"), ("00:30:00:00", "00:31:00:00")])
    _pump()
    row = dialog._rows[0]
    plain = row.height()
    assert not row.errorLabel.isVisible() and row.messageSlot.height() > 0

    type_into(row.startEdit, "00:50:00:00")
    leave(row.startEdit)
    _pump()
    assert shown(row.errorLabel) == "Start must be before end", "전제: 오류가 보인다"
    with_error = row.height()

    type_into(row.startEdit, "00:10:00:00")
    leave(row.startEdit)
    _pump()
    cleared = row.height()

    dialog.addButton.click()
    _pump()
    blank = dialog._rows[2]
    assert shown(blank.noteLabel) == IGNORED, "전제: 무시 안내가 보인다"

    assert with_error == plain and cleared == plain
    assert dialog._rows[0].height() == plain and blank.height() == plain


def test_a_message_too_long_for_the_row_is_elided_on_one_line(qtbot, tmp_path, basis):
    """안내 줄은 줄바꿈하지 않고, 넘치는 글은 말줄임하며 전문을 툴팁에 둬야 한다. 행의 높이는 그대로다.

    창을 가장 좁게 줄이고 첫 행의 안내 줄에 행보다 긴 글(같은 글자 200개)을 오류로 보임
    -> 줄바꿈 꺼짐, 보이는 글이 "…"로 끝나고 전문보다 짧다, 툴팁 == 전문, 행의 높이 == 글이 없을 때
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    dialog.resize(dialog.minimumSizeHint().width(), 300)
    _pump()
    row = dialog._rows[0]
    plain = row.height()
    long_text = "가" * 200

    row.showMessage({"error": long_text})
    _pump()

    assert not row.errorLabel.wordWrap() and not row.noteLabel.wordWrap()
    visible = shown(row.errorLabel)
    assert visible.endswith("…") and len(visible) < len(long_text)
    assert row.errorLabel.toolTip() == long_text
    assert row.height() == plain


def test_only_the_error_shows_when_an_error_and_a_note_fall_on_the_same_row(qtbot, tmp_path, basis):
    """안내 줄에 오류와 무시 안내가 함께 걸리면 오류만 보여야 한다.

    첫 행의 안내 줄에 오류 "E"와 무시 안내 "N"을 함께 넘김 -> 오류 라벨만 보이고 글 == "E"
    무시 안내만 넘김 -> 무시 안내 라벨만 보이고 글 == "N"
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    row = dialog._rows[0]

    row.showMessage({"error": "E", "note": "N"})
    _pump()
    assert shown(row.errorLabel) == "E" and not row.noteLabel.isVisible()

    row.showMessage({"note": "N"})
    _pump()
    assert shown(row.noteLabel) == "N" and not row.errorLabel.isVisible()
