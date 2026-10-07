"""구간을 정하며 받은 moov를 다운로드가 다시 쓰는지 (#309).

실제 창에서 카드 클릭 → 편집 창 → 확인 → 전역 다운로드까지 지난다. 조회 자리는 대역이
맡아 해상도마다 다른 표식 객체를 moov로 돌려준다. 엔진 자리는 서비스 대역이 맡아 넘어온
``Content``를 적어 둔다 — 엔진이 넘겨받은 moov를 다시 받지 않는 것은
``tests/unit/core/test_file_sections.py``가 잰다.
"""

import gc
import weakref
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
from app.viewmodels.section_edit_viewmodel import keep_section_head, take_section_head
from app.views import mainWindow as mw_mod
from app.views.mainWindow import VodDownloader
from core.api.representations import StreamEntry
from core.models.download_state import DownloadState
from core.utils.paths import release_output_paths
from tests.unit.card_helpers import drop_new_top_levels, hold_style, snapshot_top_levels

ROW = ("00100000", "00200000")


class _Head:
    """moov 대역 — 어느 해상도를 조회하며 받았는지만 안다."""

    def __init__(self, resolution: int):
        self.resolution = resolution


class _Probe:
    """조회 대역 — 60fps · 1시간. 조회마다 그 해상도의 새 moov 대역을 돌려준다."""

    def __init__(self):
        self.heads: list[_Head] = []

    def __call__(self, item) -> SectionBasis:
        head = _Head(item.resolution)
        self.heads.append(head)
        return SectionBasis(fps=Fraction(60), duration=3600.0, mp4_head=head)


class _Engine:
    """DownloadService 대역 — 제출을 적어 둔다."""

    def __init__(self):
        self.submissions: list[dict] = []

    def submit(self, content, **kwargs):
        self.submissions.append({"content": content, **kwargs})
        return self

    def elapsed_seconds(self) -> float:
        return 1.0

    def wait(self, timeout=None) -> bool:
        return True


@pytest.fixture
def probe(qapp, monkeypatch):
    """실제 QSS · 조회 대역 · 네트워크와 안내 창 차단."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))

    def no_network():
        raise RuntimeError("network disabled in tests")

    fake = _Probe()
    monkeypatch.setattr("app.widgets.widget.get_thread_session", no_network)
    monkeypatch.setattr(DownloadLogger, "_setup_logging", lambda self: None)
    monkeypatch.setattr(section_basis, "probe_section_basis", fake)
    monkeypatch.setattr(mw_mod.QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(mw_mod.QMessageBox, "information", lambda *a, **k: None)
    before = snapshot_top_levels()
    yield fake
    drop_new_top_levels(before)


def _pump():
    for _ in range(3):
        QApplication.processEvents()


def _item(tmp_path, title: str = "제목") -> ContentItem:
    """대기 카드 — 1080p(주소 u1)와 480p(주소 u2), 둘 다 60fps."""
    item = ContentItem(
        f"https://chzzk.naver.com/video/{title}",
        {
            "title": title,
            "category": "",
            "channelName": "채널",
            "createdDate": "",
            "duration": 3600,
        },
        [StreamEntry(1080, "u1", frame_rate=60.0), StreamEntry(480, "u2", frame_rate=60.0)],
        None,
        "",
        str(tmp_path),
        "video",
        None,
    )
    item.total_size = "595.34 MB"
    return item


@pytest.fixture
def window(probe):
    """실제 메인 창과 서비스 대역. 끝나면 예약한 파일 이름을 푼다."""
    win = VodDownloader()
    win.resize(1000, win.height())
    win.show()
    QTest.qWaitForWindowExposed(win)
    engine = _Engine()
    win.downloadViewModel._service = engine
    yield win, engine
    for submission in engine.submissions:
        release_output_paths(submission["content"].selection_paths)


def _add(win, item: ContentItem) -> None:
    win.contentManager.model.addItem(item)
    _pump()


def _give_section(qtbot, win, item: ContentItem) -> None:
    """편집 창으로 구간 하나(10분~20분)를 넣는다."""
    QTest.mouseClick(win.listView.widgetFor(item).fileSizeLabel, Qt.MouseButton.LeftButton)
    _pump()
    dialog = win._sectionDialog
    qtbot.waitUntil(lambda: dialog.viewModel().state == "ready", timeout=3000)
    _pump()
    for edit, digits in zip((dialog._rows[0].startEdit, dialog._rows[0].endEdit), ROW):
        QTest.keyClick(edit, Qt.Key.Key_Delete)
        QTest.keyClicks(edit, digits)
        edit.commit()
    _pump()
    dialog.okButton.click()
    _pump()
    assert len(item.selections) == 1, "전제: 구간이 쓰여야 한다"


def _pick(win, item: ContentItem, resolution: int) -> None:
    """카드에서 그 해상도의 버튼을 실제로 눌러 고른다 — 접혀 있으면 먼저 펼친다."""
    widget = win.listView.widgetFor(item)
    index = next(i for i, rep in enumerate(item.unique_reps) if rep[0] == resolution)
    widget.buttons[index].click()
    _pump()
    if widget.isExpanded():
        widget.buttons[index].click()
        _pump()
    assert item.resolution == resolution, "전제: 해상도가 바뀌어야 한다"


def _settle(qtbot, win) -> None:
    refitter = win.contentManager._sectionRefitter
    qtbot.waitUntil(lambda: refitter.pendingCount() == 0, timeout=5000)
    _pump()


def test_the_moov_fetched_by_the_editor_is_handed_to_the_engine(qtbot, tmp_path, window, probe):
    """편집 창이 조회하며 받은 moov는 같은 주소의 다운로드를 시작할 때 엔진에 넘어가야 한다.

    1080p에서 편집 창을 열어 구간 하나를 확인, 전역 다운로드
    -> 엔진에 넘어온 content.mp4_head is 편집 창의 조회가 받은 것, 카드에는 남지 않음
    """
    win, engine = window
    item = _item(tmp_path)
    _add(win, item)
    _give_section(qtbot, win, item)
    assert item.section_head == ("u1", probe.heads[0]), "전제: 카드가 주소와 함께 들고 있다"

    win.downloadButton.click()
    _pump()

    assert engine.submissions[0]["content"].mp4_head is probe.heads[0]
    assert item.section_head is None


def test_the_moov_of_the_new_resolution_is_handed_after_a_resolution_change(
    qtbot, tmp_path, window, probe
):
    """해상도를 바꾸면 앞 해상도의 moov를 버리고 새 해상도를 조회하며 받은 것을 넘겨야 한다.

    1080p에서 구간을 확인한 뒤 480p를 고름, 조회가 끝난 뒤 전역 다운로드
    -> 엔진에 넘어온 content.mp4_head is 480p의 조회가 받은 것(1080p의 것이 아니다)
    """
    win, engine = window
    item = _item(tmp_path)
    _add(win, item)
    _give_section(qtbot, win, item)
    _pick(win, item, 480)
    _settle(qtbot, win)
    assert [head.resolution for head in probe.heads] == [1080, 480], "전제: 새 해상도를 조회했다"

    win.downloadButton.click()
    _pump()

    assert engine.submissions[0]["content"].mp4_head is probe.heads[1]


def test_the_moov_of_the_old_resolution_is_dropped_the_moment_the_resolution_changes(
    qtbot, tmp_path, window, probe, monkeypatch
):
    """해상도를 바꾼 순간 — 새 조회가 끝나기 전에도 — 카드는 앞 해상도의 moov를 놓아야 한다.

    1080p에서 구간을 확인, 480p의 조회가 실패하게 한 뒤 480p를 고름
    -> 카드의 section_head is None, 다운로드하면 content.mp4_head is None(엔진이 새로 받는다)
    """
    win, engine = window
    item = _item(tmp_path)
    _add(win, item)
    _give_section(qtbot, win, item)

    def failing(probed):
        raise RuntimeError("조회 실패(대역)")

    monkeypatch.setattr(section_basis, "probe_section_basis", failing)
    _pick(win, item, 480)
    assert item.section_head is None
    _settle(qtbot, win)

    win.downloadButton.click()
    _pump()

    assert engine.submissions[0]["content"].mp4_head is None


def test_a_moov_fetched_at_another_address_is_not_handed_over(tmp_path):
    """카드가 든 moov의 주소가 지금의 주소와 다르면 넘기지 않고 버려야 한다.

    주소 u1에서 받은 moov를 둔 카드의 주소가 u2로 바뀜(다시 맞추기를 거치지 않은 해상도 변경)
    -> take_section_head == None, 카드의 section_head is None
    """
    item = _item(tmp_path)
    keep_section_head(item, "u1", _Head(1080))
    item.select_rep(next(rep for rep in item.unique_reps if rep[0] == 480))
    assert item.base_url == "u2", "전제: 주소가 바뀌었다"

    assert take_section_head(item) is None
    assert item.section_head is None


def test_deleting_the_card_drops_its_moov(qtbot, tmp_path, window, probe):
    """카드를 지우면 그 카드가 들고 있던 moov를 아무도 붙잡지 않아야 한다.

    구간을 확인해 moov를 든 카드를 목록에서 지움
    -> 카드의 section_head is None, 조회 대역의 기록을 비운 뒤 그 moov의 약한 참조 == None
    """
    win, engine = window
    item = _item(tmp_path)
    _add(win, item)
    _give_section(qtbot, win, item)
    held = weakref.ref(probe.heads[0])

    win.contentManager.removeItem(item)
    _pump()
    probe.heads.clear()
    gc.collect()

    assert item.section_head is None
    assert held() is None


def test_only_the_most_recent_card_keeps_a_moov(qtbot, tmp_path, window, probe):
    """다른 카드의 구간을 확인하면 앞 카드가 들고 있던 moov는 버려야 한다 — 한 번에 한 카드만 든다.

    카드 둘의 구간을 차례로 확인
    -> 첫 카드의 section_head is None, 둘째 카드의 section_head == (u1, 둘째 조회가 받은 것)
    """
    win, engine = window
    first, second = _item(tmp_path, "첫째"), _item(tmp_path, "둘째")
    _add(win, first)
    _add(win, second)
    _give_section(qtbot, win, first)

    _give_section(qtbot, win, second)

    assert first.section_head is None
    assert second.section_head == ("u1", probe.heads[1])


def test_the_moov_is_released_when_the_download_ends(qtbot, tmp_path, window, probe):
    """다운로드가 끝나면 엔진에 넘겼던 moov를 뷰모델이 붙잡고 있지 않아야 한다.

    구간을 확인하고 다운로드를 시작, 서비스 대역이 완료를 알림
    -> 완료 뒤 뷰모델이 든 마지막 다운로드의 content.mp4_head is None
    """
    win, engine = window
    item = _item(tmp_path)
    _add(win, item)
    _give_section(qtbot, win, item)
    win.downloadButton.click()
    _pump()
    submission = engine.submissions[0]
    assert submission["content"].mp4_head is probe.heads[0], "전제: 엔진에 넘어갔다"

    item.downloadState = DownloadState.FINISHED  # 배치가 이 카드를 다시 고르지 않게 한다
    submission["on_finished"]()
    qtbot.waitUntil(lambda: win.downloadViewModel.handle is None, timeout=3000)

    assert submission["content"].mp4_head is None
