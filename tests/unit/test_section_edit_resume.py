"""일부 실패 뒤 재시도한 카드의 구간을 편집했을 때의 이어받기 (#309).

실제 창에서 카드 클릭 → 편집 창 → 확인 → 재시도 → 전역 다운로드까지 지난다. 엔진 자리는
서비스 대역이 맡는다 — 테스트가 엔진처럼 구간 파일을 만들고 실패 · 이어받기 기록을 남긴다.

같은 번호에 같은 값(시작 · 끝 프레임)인 끝낸 구간만 다시 받지 않는다.
"""

import os
from fractions import Fraction

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
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
from core.downloaders.base import PostprocessError
from core.models.download_state import DownloadState
from core.models.plan import TimeRange
from core.models.section_resume import SectionResume
from core.utils.hybrid_cut import CUT_FAILED, CutError
from core.utils.paths import release_output_paths
from tests.unit.section_input import enter_time
from tests.unit.card_helpers import drop_new_top_levels, hold_style, snapshot_top_levels

FIRST, SECOND, THIRD = (
    TimeRange(600.0, 1200.0),
    TimeRange(1800.0, 2400.0),
    TimeRange(3000.0, 3300.0),
)
ROWS = [("00100000", "00200000"), ("00300000", "00400000"), ("00500000", "00550000")]


@pytest.fixture(autouse=True)
def _environment(qapp, monkeypatch):
    """실제 QSS · 조회 대역(60fps · 1시간) · 네트워크와 안내 창 차단."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))

    def no_network():
        raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", no_network)
    monkeypatch.setattr(DownloadLogger, "_setup_logging", lambda self: None)
    monkeypatch.setattr(
        section_basis,
        "probe_section_basis",
        lambda item: SectionBasis(fps=Fraction(60), duration=3600.0),
    )
    monkeypatch.setattr(mw_mod.QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(mw_mod.QMessageBox, "information", lambda *a, **k: None)
    before = snapshot_top_levels()
    yield
    drop_new_top_levels(before)


class _Engine:
    """DownloadService 대역 — 제출을 적어 두고, 테스트가 일부 실패로 끝낸다."""

    def __init__(self):
        self.submissions: list[dict] = []

    def submit(self, content, **kwargs):
        self.submissions.append({"content": content, **kwargs})
        return self

    def elapsed_seconds(self) -> float:
        return 1.0

    def wait(self, timeout=None) -> bool:
        return True

    def fail_sections(self, failed: set[int]) -> tuple[str, ...]:
        """마지막 제출에서 failed 번호의 컷만 실패했다고 알린다 — 나머지 구간의 파일을 만들고 기록을 남긴다."""
        submission = self.submissions[-1]
        content, data = submission["content"], submission["data"]
        done = frozenset(range(len(content.selections))) - failed
        for number in done:
            with open(content.selection_paths[number], "wb") as file:
                file.write(b"made by the first run")
        data.sections_total = len(content.selections)
        data.sections_done, data.sections_failed = len(done), len(failed)
        data.section_resume = SectionResume(
            selections=tuple(content.selections), paths=tuple(content.selection_paths), done=done
        )
        error = PostprocessError("후처리(cut) 실패")
        error.__cause__ = CutError(CUT_FAILED, "시험")
        submission["on_failed"](error)
        release_output_paths(content.selection_paths)  # 엔진은 끝날 때 이름의 예약을 푼다
        return tuple(content.selection_paths)


def _pump():
    for _ in range(3):
        QApplication.processEvents()


def _edit(qtbot, win, item, rows) -> None:
    """편집 창을 열어 행을 rows로 만들고 확인한다. rows의 항목이 None이면 그 행은 건드리지 않는다."""
    QTest.mouseClick(win.listView.widgetFor(item).fileSizeLabel, Qt.MouseButton.LeftButton)
    _pump()
    dialog = win._sectionDialog
    qtbot.waitUntil(lambda: dialog.viewModel().state == "ready", timeout=3000)
    _pump()
    while len(dialog._rows) < len(rows):
        dialog.addButton.click()
        _pump()
    for index, row in enumerate(rows):
        if row is None:
            continue
        for edit, digits in (
            (dialog._rows[index].startEdit, row[0]),
            (dialog._rows[index].endEdit, row[1]),
        ):
            enter_time(edit, digits)  # 끝 두 자리는 프레임 칸, 그 앞은 시분초 칸
    _pump()
    return dialog


@pytest.fixture
def failed_card(qtbot, tmp_path):
    """구간 셋 가운데 둘째 컷만 실패하고 끝난 뒤 ↻로 대기가 된 카드. `_1` · `_3` 파일이 디스크에 있다."""
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {
            "title": "제목",
            "category": "",
            "channelName": "채널",
            "createdDate": "",
            "duration": 3600,
        },
        [StreamEntry(1080, "u1", frame_rate=60.0)],
        None,
        "",
        str(tmp_path),
        "video",
        None,
    )
    item.total_size = "595.34 MB"
    win = VodDownloader()
    win.resize(1000, win.height())
    win.show()
    QTest.qWaitForWindowExposed(win)
    win.contentManager.model.addItem(item)
    _pump()
    engine = _Engine()
    win.downloadViewModel._service = engine
    _edit(qtbot, win, item, ROWS).okButton.click()
    _pump()
    assert item.selections == (FIRST, SECOND, THIRD), "전제: 구간 셋이 쓰여야 한다"
    win.downloadButton.click()
    _pump()
    paths = engine.fail_sections({1})
    qtbot.waitUntil(lambda: item.downloadState == DownloadState.FAILED, timeout=3000)
    _pump()
    win.listView.widgetFor(item).retryButton.click()
    _pump()
    assert item.downloadState == DownloadState.WAITING
    yield win, item, engine, paths
    for submission in engine.submissions:
        release_output_paths(submission["content"].selection_paths)


def _names(paths) -> list[str]:
    return [os.path.basename(path) for path in paths]


def _restart(win) -> None:
    win.downloadButton.click()
    _pump()


def test_only_the_edited_section_is_received_again_and_the_file_names_stay(qtbot, failed_card):
    """셋 가운데 둘째 구간만 고쳐 재시도하면 둘째만 다시 받고 파일 이름은 _1 · _2 · _3 그대로여야 한다.

    `_1` · `_3`을 끝낸 카드에서 둘째 구간을 00:35:00:00~00:45:00:00으로 고치고 다시 받기 시작
    -> 제출된 selections == (첫째, 고친 둘째, 셋째), 이어받기 기록의 끝낸 구간 == {0, 2},
       파일 이름 == "제목 1080p_1.mp4" · "_2.mp4" · "_3.mp4"(" (1)" 없음), 끝낸 구간의 경로 == 첫 실행의 경로
    -> 넘어간 기록에 그때의 임시 원본은 없다
    """
    win, item, engine, paths = failed_card

    _edit(qtbot, win, item, [None, ("00350000", "00450000"), None]).okButton.click()
    _pump()
    _restart(win)

    assert len(engine.submissions) == 2
    content = engine.submissions[1]["content"]
    assert content.selections == (FIRST, TimeRange(2100.0, 2700.0), THIRD)
    assert content.section_resume.done == frozenset({0, 2})
    assert _names(content.selection_paths) == [f"제목 1080p_{n}.mp4" for n in (1, 2, 3)]
    assert (content.selection_paths[0], content.selection_paths[2]) == (paths[0], paths[2])
    assert content.section_resume.fits(content.selections, content.selection_paths)
    assert content.section_resume.source_path is None
    assert item.sections_done == 2


def test_an_edited_section_that_was_already_made_gets_a_new_name_instead_of_overwriting(
    qtbot, failed_card
):
    """끝낸 구간의 값을 바꾸면 그 번호의 옛 파일을 덮어쓰지 않고 새 이름으로 받아야 한다.

    `_1` 파일이 있는 카드에서 첫째 구간을 00:05:00:00~00:20:00:00으로 고치고 다시 받기 시작
    -> 끝낸 구간 == {2}, 첫째의 파일 이름 == "제목 1080p_1 (1).mp4", 옛 `_1` 파일의 내용 그대로
    """
    win, item, engine, paths = failed_card

    _edit(qtbot, win, item, [("00050000", "00200000"), None, None]).okButton.click()
    _pump()
    _restart(win)

    content = engine.submissions[1]["content"]
    assert content.section_resume.done == frozenset({2})
    assert _names(content.selection_paths) == [
        "제목 1080p_1 (1).mp4",
        "제목 1080p_2.mp4",
        "제목 1080p_3.mp4",
    ]
    with open(paths[0], "rb") as file:
        assert file.read() == b"made by the first run"


def test_added_sections_are_received_and_finished_ones_are_kept(qtbot, failed_card):
    """구간을 더하면 새 구간과 끝내지 못한 구간만 받고, 끝낸 구간은 그대로 두어야 한다.

    `_1` · `_3`을 끝낸 카드에 넷째 구간 00:56:00:00~00:58:00:00을 더하고 다시 받기 시작
    -> selections가 넷, 끝낸 구간 == {0, 2}, 파일 이름 == _1 ~ _4
    """
    win, item, engine, _paths = failed_card

    _edit(qtbot, win, item, [None, None, None, ("00560000", "00580000")]).okButton.click()
    _pump()
    _restart(win)

    content = engine.submissions[1]["content"]
    assert len(content.selections) == 4
    assert content.selections[3] == TimeRange(3360.0, 3480.0)
    assert content.section_resume.done == frozenset({0, 2})
    assert _names(content.selection_paths) == [f"제목 1080p_{n}.mp4" for n in (1, 2, 3, 4)]


def test_reordered_sections_start_over_with_new_names(qtbot, failed_card):
    """번호와 값이 함께 맞는 끝낸 구간이 없으면 기록을 버리고 처음부터 새 이름으로 받아야 한다.

    `_1` · `_3`을 끝낸 카드에서 첫째 행을 맨 아래로 내려 순서를 (둘째, 셋째, 첫째)로 바꾸고 다시 받기 시작
    -> 이어받기 기록 없음, 파일 이름 == "_1 (1)" · "_2" · "_3 (1)"(있는 파일을 피한다)
    """
    win, item, engine, _paths = failed_card

    dialog = _edit(qtbot, win, item, [None, None, None])
    dialog._rows[0].downButton.click()
    _pump()
    dialog._rows[1].downButton.click()
    _pump()
    dialog.okButton.click()
    _pump()
    assert item.selections == (SECOND, THIRD, FIRST), "전제: 순서가 바뀌어야 한다"
    _restart(win)

    content = engine.submissions[1]["content"]
    assert content.section_resume is None
    assert _names(content.selection_paths) == [
        "제목 1080p_1 (1).mp4",
        "제목 1080p_2.mp4",
        "제목 1080p_3 (1).mp4",
    ]


def test_same_number_with_another_value_is_not_resumed(qtbot, failed_card):
    """번호가 같아도 값이 다르면, 값이 같아도 번호가 다르면 이어받지 않아야 한다.

    `_1` · `_3`을 끝낸 카드에서 첫째를 셋째의 값(00:50:00:00~00:55:00:00)으로, 셋째를 첫째의 값으로 바꿈
    (두 값이 서로 자리를 바꿨다) → 다시 받기 시작
    -> 이어받기 기록 없음(끝낸 번호 0 · 2 모두 값이 다르다)
    """
    win, item, engine, _paths = failed_card

    rows = [("00500000", "00550000"), None, ("00100000", "00200000")]
    _edit(qtbot, win, item, rows).okButton.click()
    _pump()
    _restart(win)

    assert engine.submissions[1]["content"].section_resume is None


def test_a_finished_file_that_disappeared_is_received_again(qtbot, tmp_path):
    """끝낸 구간의 파일이 사라졌으면 재시도를 누를 때 그 구간을 끝나지 않은 것으로 돌려 다시 받아야 한다.

    `_1` · `_3`을 끝내고 실패한 카드에서 `_3` 파일을 지운 뒤 ↻ → 둘째 구간을 고치고 다시 받기 시작
    -> 끝낸 구간 == {0}(셋째는 다시 받는다), 파일 이름 == _1 · _2 · _3
    """
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {
            "title": "제목",
            "category": "",
            "channelName": "채널",
            "createdDate": "",
            "duration": 3600,
        },
        [StreamEntry(1080, "u1", frame_rate=60.0)],
        None,
        "",
        str(tmp_path),
        "video",
        None,
    )
    win = VodDownloader()
    win.resize(1000, win.height())
    win.show()
    QTest.qWaitForWindowExposed(win)
    win.contentManager.model.addItem(item)
    _pump()
    engine = _Engine()
    win.downloadViewModel._service = engine
    try:
        _edit(qtbot, win, item, ROWS).okButton.click()
        _pump()
        win.downloadButton.click()
        _pump()
        paths = engine.fail_sections({1})
        qtbot.waitUntil(lambda: item.downloadState == DownloadState.FAILED, timeout=3000)
        _pump()
        os.remove(paths[2])

        win.listView.widgetFor(item).retryButton.click()
        _pump()
        _edit(qtbot, win, item, [None, ("00350000", "00450000"), None]).okButton.click()
        _pump()
        _restart(win)

        content = engine.submissions[1]["content"]
        assert content.section_resume.done == frozenset({0})
        assert _names(content.selection_paths) == [f"제목 1080p_{n}.mp4" for n in (1, 2, 3)]
    finally:
        for submission in engine.submissions:
            release_output_paths(submission["content"].selection_paths)


def test_an_unedited_retry_card_resumes_exactly_as_before(qtbot, failed_card):
    """구간을 고치지 않은 재시도 카드는 전과 같이 그때의 기록 그대로 이어받아야 한다.

    `_1` · `_3`을 끝낸 카드를 편집 없이 다시 받기 시작
    -> 넘어간 이어받기 기록이 실패한 실행이 남긴 객체 그대로, 경로 == 첫 실행의 경로
    """
    win, item, engine, paths = failed_card
    left = item.section_retry[1]

    _restart(win)

    content = engine.submissions[1]["content"]
    assert content.section_resume is left
    assert content.selection_paths == paths
