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
from PySide6.QtCore import Qt
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
    """칸의 글자를 지우고 키 입력으로 넣는다 — Enter는 누르지 않는다."""
    edit.selectAll()
    QTest.keyClick(edit, Qt.Key.Key_Delete)
    QTest.keyClicks(edit, text)
    _pump()


def set_rows(dialog, rows: list[tuple[str, str]]) -> None:
    """편집 창의 행을 주어진 (시작, 끝) 글자들로 만든다 — 추가 버튼과 키 입력으로."""
    while len(dialog._rows) < len(rows):
        dialog.addButton.click()
        _pump()
    for index, (start, end) in enumerate(rows):
        type_into(dialog._rows[index].startEdit, start)
        type_into(dialog._rows[index].endEdit, end)


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
    """대기 카드의 구간 요약을 누르면 편집 창이 열리고 영상 전체 한 행이 보여야 한다.

    60fps · 길이 3600초, 구간 없는 카드
    -> 조회 중에는 안내가 보이고 확인이 꺼져 있다
    -> 조회 뒤 행 1개 ("00:00:00:00", "01:00:00:00"), 확인 켜짐
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
    assert [(row.startEdit.text(), row.endEdit.text()) for row in dialog._rows] == [
        ("00:00:00:00", "01:00:00:00")
    ]
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


@pytest.mark.parametrize(
    "start, end, message",
    [
        ("00:20:00:00", "00:10:00:00", "Start must be before end"),  # 역방향
        ("00:10:00:00", "00:10:00:00", "Start must be before end"),  # 길이 0
        ("00:10:00:00", "01:00:00:01", "Selection is outside the video"),  # 길이보다 1프레임 뒤
        ("00:10:00", "00:20:00:00", "Invalid timecode format"),  # 세 칸
        ("00:10:00.500", "00:20:00:00", "Invalid timecode format"),  # 밀리초 입력
        ("00:10:00:0x", "00:20:00:00", "Invalid timecode format"),  # 숫자가 아닌 글자
        ("00:10:60:00", "00:20:00:00", "Minutes and seconds must be below 60"),
        (
            "00:10:00:60",
            "00:20:00:00",
            "Frame number must be below the frame rate",
        ),  # 60fps의 FF는 59까지
    ],
)
def test_invalid_input_is_shown_on_the_row_and_cannot_be_confirmed(
    qtbot, tmp_path, basis, start, end, message
):
    """틀린 입력은 그 행에 오류로 보이고 확인으로 카드에 쓰이지 않아야 한다.

    60fps · 길이 3600초에서 위 표의 (시작, 끝)
    -> 행의 문구 == message, 두 칸의 invalid 속성 참, 확인 꺼짐, 확인을 눌러도 selections == ()
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)

    set_rows(dialog, [(start, end)])

    row = dialog._rows[0]
    assert shown(row.errorLabel) == message
    assert row.startEdit.property("invalid") is True and row.endEdit.property("invalid") is True
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

    추가 버튼을 19번 눌러 20행
    -> 추가 버튼 꺼짐, 뷰모델에 직접 추가를 청해도 20행
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
    assert shown(dialog.headerLabel) == "Sections 20 / 20 · 60fps"


def test_typed_text_is_rewritten_in_four_fields_and_milliseconds_are_only_shown(
    qtbot, tmp_path, basis
):
    """해석된 입력은 편집을 끝내면 네 칸 표기로 고쳐지고, 밀리초는 툴팁에만 나와야 한다.

    60fps. 시작 칸에 "0:5:3:30" 입력 뒤 Enter 없이 끝 칸으로 이동
    -> 시작 칸 "00:05:03:30", 툴팁 "00:05:03.500"
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    dialog = open_editor(qtbot, win, item)
    row = dialog._rows[0]

    type_into(row.startEdit, "0:5:3:30")
    row.startEdit.editingFinished.emit()  # 포커스가 떠날 때 Qt가 내는 신호
    row.startEdit.clearFocus()
    dialog.viewModel().validated.emit()
    _pump()

    assert row.startEdit.text() == "00:05:03:30"
    assert row.startEdit.toolTip() == "00:05:03.500"


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


def test_deleting_a_row_removes_that_section_and_the_last_row_returns_to_the_whole_video(
    qtbot, tmp_path, basis
):
    """삭제는 그 행만 지우고, 마지막 남은 행을 지우면 영상 전체 한 행으로 돌아가야 한다.

    행 A(600~1200) · B(1800~1860)에서 A의 ✕ -> 행 B만 남는다
    B의 ✕ -> 행 ("00:00:00:00", "01:00:00:00"), 확인하면 selections == ()
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

    dialog._rows[0].deleteButton.click()
    _pump()
    assert [(row.startEdit.text(), row.endEdit.text()) for row in dialog._rows] == [
        ("00:00:00:00", "01:00:00:00")
    ]
    press_ok(dialog)
    assert item.selections == ()


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


def test_a_section_that_reached_the_end_follows_the_new_length(qtbot, tmp_path, basis):
    """옛 영상의 끝에 닿아 있던 구간은 새 길이에 맞춰 끝이 옮겨지고 카드에 적혀야 한다.

    60fps · 3600초에서 3540~3600초(끝 = 영상 끝)를 정함. 480p의 조회값 30fps · 3590초
    -> 구간 == (3540, 3590), section_end_fitted, 요약 자리의 툴팁에 끝을 옮겼다는 문장
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    give_sections(qtbot, win, item, [("00:59:00:00", "01:00:00:00")])
    basis.by_resolution[480] = (Fraction(30), 3590.0)

    _pick(win, item, 480)
    settle(qtbot, win)

    assert item.selections == (TimeRange(3540.0, 3590.0),)
    assert item.section_end_fitted and not item.section_out_of_range
    # 요약 뒤의 알림은 폭이 모자라면 떼인다 — 폭과 무관한 툴팁으로 잰다
    tooltip = win.listView.widgetFor(item).fileSizeLabel.toolTip()
    assert "A section that reached the end now ends at the end of this resolution." in tooltip


def test_a_section_past_the_new_length_is_left_alone_and_flagged(qtbot, tmp_path, basis):
    """영상 끝에 닿아 있지 않던 구간이 새 길이를 벗어나면 고치지 않고 벗어났다고 알려야 한다.

    60fps · 3600초에서 600~1200초와 3500~3595초를 정함. 480p의 조회값 30fps · 3590초
    -> 구간 == ((600, 1200), (3500, 3595)), section_out_of_range, 요약 자리의 툴팁에 벗어났다는 문장
    """
    item = _make_item(str(tmp_path))
    win = open_window(tmp_path, item)
    give_sections(
        qtbot, win, item, [("00:10:00:00", "00:20:00:00"), ("00:58:20:00", "00:59:55:00")]
    )
    basis.by_resolution[480] = (Fraction(30), 3590.0)

    _pick(win, item, 480)
    settle(qtbot, win)

    assert item.selections == (TimeRange(600.0, 1200.0), TimeRange(3500.0, 3595.0))
    assert item.section_out_of_range and not item.section_end_fitted
    tooltip = win.listView.widgetFor(item).fileSizeLabel.toolTip()
    assert "Some sections are longer than this resolution. Edit the sections." in tooltip


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
