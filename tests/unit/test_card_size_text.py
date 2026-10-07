"""받는 동안의 크기 표시 · 받을 구간의 합 · 준비 중 표시 (#309).

두 층으로 잰다.

- 카드 하나(위젯을 직접 세운다): 상태 · 전달 방식마다 3행 우측에 무엇이 적히는지, 좁은 폭에서
  무엇이 먼저 양보하는지
- 실제 창(카드 클릭 → 편집 창 → 확인 → 해상도 변경 → 전역 다운로드): 받을 구간의 합이 언제
  계산되고 바뀌는지, 준비 중 표시가 언제 켜지고 꺼지는지. 조회 자리는 대역이 맡아 실제 mp4
  바이트를 해석한 moov를 돌려준다(해상도마다 다른 파일). 엔진 자리는 서비스 대역이다

크기의 기대값은 제품의 표시 함수를 부르지 않고 글자 그대로 적는다.
"""

import dataclasses
import logging
import threading

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

import app.process_memory as process_memory
import app.section_basis as section_basis
import app.theme as theme
import main as main_module
from app.download_logger import DownloadLogger
from app.section_basis import SectionBasis
from app.viewmodels.data import ContentItem
from app.views import mainWindow as mw_mod
from app.views.mainWindow import VodDownloader
from app.widgets.widget import ContentItemWidget
from core.api.mp4 import read_mp4_head
from core.api.representations import StreamEntry
from core.models.download_state import DownloadState
from core.models.events import ProgressEvent
from core.models.plan import TimeRange
from core.utils.mp4_ranges import sections_download_size
from core.utils.paths import release_output_paths
from tests.unit.card_helpers import drop_new_top_levels, hold_style, shown, snapshot_top_levels
from tests.unit.core.mp4_builder import audio_spec, build_mp4, video_spec

MB = 1024 * 1024
OTHER_PATH = "D:/vod/archive/2026/summer/finals/T1-vs-GEN-full-set-highlights-and-interviews"
TWO_SECTIONS = (TimeRange(600.0, 1200.0), TimeRange(1800.0, 2400.0))  # 합 20분


@pytest.fixture(autouse=True)
def _environment(qapp, monkeypatch):
    """실제 QSS · 네트워크와 안내 창 차단 · 로그 파일 차단. 테스트가 띄운 창은 끝에 파괴한다."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))

    class _FailingSession:
        def head(self, *args, **kwargs):
            raise RuntimeError("network disabled in tests")

        def get(self, *args, **kwargs):
            raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", lambda: _FailingSession())
    monkeypatch.setattr("app.widgets.widget._global_download_path", "C:/dl")
    monkeypatch.setattr(DownloadLogger, "_setup_logging", lambda self: None)
    monkeypatch.setattr(mw_mod.QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(mw_mod.QMessageBox, "information", lambda *a, **k: None)
    before = snapshot_top_levels()
    yield
    drop_new_top_levels(before)


def _pump():
    for _ in range(3):
        QApplication.processEvents()


# ================================================================ 카드 하나


def _card(
    content_type: str,
    state: DownloadState,
    *,
    selections=(),
    received: int = 0,
    section_bytes: int | None = None,
    cutting: bool = False,
    preparing: bool = False,
    width: int = 1400,
):
    """상태를 정한 카드 하나를 보이는 창 안에 놓는다. 영상 전체 크기는 28.99 GB다."""
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {
            "title": "제목",
            "category": "",
            "channelName": "채널",
            "createdDate": "",
            "duration": 28800,
        },
        [["1080", "u1"], ["720", "u2"]],
        None,
        "",
        OTHER_PATH,  # 전역 경로와 달라 경로 글자가 보인다
        content_type,
        None,
    )
    item.downloadState = DownloadState.WAITING
    item.total_size = "28.99 GB"
    widget = ContentItemWidget(item, 0)
    widget.addRepresentationButtons()
    item.total_size = "28.99 GB"
    item.selections = tuple(selections)
    item.section_bytes = section_bytes
    item.downloadState = state
    item.download_progress = 42
    item.download_speed = "8.2 MB/s"
    item.download_remain_time = "00:12:34"
    item.download_size = received
    item.post_process = cutting
    item.preparing = preparing
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


@pytest.mark.parametrize("state", [DownloadState.RUNNING, DownloadState.PAUSED])
def test_encoded_vod_card_shows_received_over_total_while_downloading(qtbot, state):
    """인코딩 완료 VOD 카드는 받는 중 · 일시정지에 "받은 크기 / 전체 크기"를 적어야 한다.

    전체 28.99 GB, 받은 100 MiB, 넓은 창
    -> "1080p · 100.00 MB / 28.99 GB"
    """
    window, widget = _card("video", state, received=100 * MB)
    qtbot.addWidget(window)

    assert shown(widget.fileSizeLabel) == "1080p · 100.00 MB / 28.99 GB"


def test_encoded_vod_card_shows_the_actual_size_when_finished(qtbot):
    """완료된 인코딩 완료 VOD 카드는 실제 크기만 적어야 한다.

    받은 1536 MiB -> "1080p · 1.50 GB"
    """
    window, widget = _card("video", DownloadState.FINISHED, received=1536 * MB)
    qtbot.addWidget(window)

    assert shown(widget.fileSizeLabel) == "1080p · 1.50 GB"


@pytest.mark.parametrize(
    ("transfer_bytes", "expected"),
    [(None, "1080p · 100.00 MB"), (2048 * MB, "1080p · 100.00 MB / 2.00 GB")],
)
def test_encoded_vod_card_never_shows_the_lookup_placeholder_as_a_size(
    qtbot, transfer_bytes, expected
):
    """크기 조회가 끝나지 않은 카드는 "Checking..."을 크기로 적지 않고, 엔진이 정한 크기가 있으면 그것을 적어야 한다.

    total_size가 "Checking...", 받은 100 MiB. 엔진이 정한 크기 없음 / 2048 MiB
    -> "1080p · 100.00 MB" / "1080p · 100.00 MB / 2.00 GB"
    """
    window, widget = _card("video", DownloadState.RUNNING, received=100 * MB)
    qtbot.addWidget(window)
    widget.item.total_size = "Checking..."
    widget.item.transfer_bytes = transfer_bytes
    widget.setData(widget.item, 0)
    _pump()

    assert shown(widget.fileSizeLabel) == expected


def test_section_card_shows_received_over_the_section_total_not_the_whole_video(qtbot):
    """인코딩 완료 VOD의 구간 카드는 받는 동안 "받은 크기 / 받을 구간의 합"을 적어야 한다.

    구간 둘, 받을 합 200 MiB, 받은 50 MiB, 영상 전체 28.99 GB
    -> "1080p · 50.00 MB / 200.00 MB" (전체 크기는 어디에도 없다)
    """
    window, widget = _card(
        "video",
        DownloadState.RUNNING,
        selections=TWO_SECTIONS,
        received=50 * MB,
        section_bytes=200 * MB,
    )
    qtbot.addWidget(window)

    assert shown(widget.fileSizeLabel) == "1080p · 50.00 MB / 200.00 MB"


def test_section_card_shows_the_total_as_received_while_cutting(qtbot):
    """자르는 동안 구간 카드는 받은 크기를 받을 크기와 같게 적어야 한다.

    구간 둘, 받을 합 200 MiB, 컷 단계(마지막 통지의 받은 크기는 199 MiB)
    -> "1080p · 200.00 MB / 200.00 MB"
    """
    window, widget = _card(
        "video",
        DownloadState.RUNNING,
        selections=TWO_SECTIONS,
        received=199 * MB,
        section_bytes=200 * MB,
        cutting=True,
    )
    qtbot.addWidget(window)

    assert shown(widget.fileSizeLabel) == "1080p · 200.00 MB / 200.00 MB"


def test_section_card_shows_the_actual_size_when_finished(qtbot):
    """완료된 구간 카드는 만든 파일의 실제 크기만 적어야 한다.

    받을 합 200 MiB, 완료 통지의 크기 180 MiB -> "1080p · 180.00 MB"
    """
    window, widget = _card(
        "video",
        DownloadState.FINISHED,
        selections=TWO_SECTIONS,
        received=180 * MB,
        section_bytes=200 * MB,
    )
    qtbot.addWidget(window)

    assert shown(widget.fileSizeLabel) == "1080p · 180.00 MB"


def test_section_card_without_a_known_total_shows_only_what_it_received(qtbot):
    """받을 구간의 합을 모르는 구간 카드는 받은 크기만 적어야 한다 — 영상 전체 크기를 적지 않는다.

    구간 둘, 받을 합 모름(None), 받은 50 MiB, 영상 전체 28.99 GB -> "1080p · 50.00 MB"
    """
    window, widget = _card(
        "video", DownloadState.RUNNING, selections=TWO_SECTIONS, received=50 * MB
    )
    qtbot.addWidget(window)

    assert shown(widget.fileSizeLabel) == "1080p · 50.00 MB"


@pytest.mark.parametrize(
    ("content_type", "selections"),
    [("m3u8", TWO_SECTIONS), ("m3u8", ()), ("hls_aes", TWO_SECTIONS), ("hls_aes", ())],
    ids=["fmp4-sections", "fmp4-whole", "ts-sections", "ts-whole"],
)
def test_segment_cards_show_only_what_they_received(qtbot, content_type, selections):
    """세그먼트 방식(fMP4 · TS) 카드는 구간이 있든 없든 받는 동안 받은 크기만, 끝나면 실제 크기만 적어야 한다.

    받은 50 MiB, 카드 데이터에 영상 전체 크기 28.99 GB와 구간 합 200 MiB가 들어 있어도
    -> 받는 중 "1080p · 50.00 MB", 완료(180 MiB) "1080p · 180.00 MB"
    """
    window, widget = _card(
        content_type,
        DownloadState.RUNNING,
        selections=selections,
        received=50 * MB,
        section_bytes=200 * MB,
    )
    qtbot.addWidget(window)
    assert shown(widget.fileSizeLabel) == "1080p · 50.00 MB"

    widget.item.downloadState = DownloadState.FINISHED
    widget.item.download_size = 180 * MB
    widget.setData(widget.item, 0)
    _pump()

    assert shown(widget.fileSizeLabel) == "1080p · 180.00 MB"


def test_waiting_section_card_of_an_encoded_vod_shows_the_section_total(qtbot):
    """대기 중인 인코딩 완료 VOD의 구간 카드는 구간 요약 뒤에 받을 구간의 합을 적어야 한다.

    구간 둘(합 20분), 받을 합 200 MiB -> "Sections 2 · 20:00 · 200.00 MB"
    """
    window, widget = _card(
        "video", DownloadState.WAITING, selections=TWO_SECTIONS, section_bytes=200 * MB
    )
    qtbot.addWidget(window)

    assert shown(widget.fileSizeLabel) == "Sections 2 · 20:00 · 200.00 MB"


@pytest.mark.parametrize("content_type", ["m3u8", "hls_aes"])
def test_waiting_section_card_of_a_segment_vod_shows_no_size(qtbot, content_type):
    """대기 중인 세그먼트 방식의 구간 카드는 크기를 적지 않아야 한다 — 영상 전체 크기도 적지 않는다.

    구간 둘(합 20분), 영상 전체 28.99 GB -> "Sections 2 · 20:00"
    """
    window, widget = _card(
        content_type, DownloadState.WAITING, selections=TWO_SECTIONS, section_bytes=200 * MB
    )
    qtbot.addWidget(window)

    assert shown(widget.fileSizeLabel) == "Sections 2 · 20:00"


def _narrow_until(window, widget, done) -> None:
    """done()이 참이 될 때까지 창을 4px씩 좁힌다 — 절대 px 없이 유도한다. 최소폭에서 멈추면 실패다."""
    width = window.width()
    while not done():
        width -= 4
        window.resize(width, window.height())
        _pump()
        assert widget.width() == width, (
            f"폭 {width}px에서 최소폭에 걸렸다 — 재려던 모양에 닿지 못했다"
        )


def test_received_size_is_the_first_thing_to_give_way_when_the_card_narrows(qtbot):
    """창이 좁아지면 받은 크기가 가장 먼저 빠지고, 그때 진행 문구와 경로 글자는 그대로 보여야 한다.

    인코딩 완료 VOD 받는 중(받은 100 MiB / 전체 28.99 GB), 넓은 창에서 4px씩 좁힘
    -> 처음 글이 바뀐 폭에서: 크기 == "1080p · 28.99 GB", 진행 문구는 잘리지 않음, 경로 글자가 보임
    -> 더 좁혀 경로가 아이콘이 될 때까지: 크기 글은 다시 길어지지 않음
    """
    window, widget = _card("video", DownloadState.RUNNING, received=100 * MB)
    qtbot.addWidget(window)
    full = "1080p · 100.00 MB / 28.99 GB"
    assert shown(widget.fileSizeLabel) == full, "전제: 넓은 창에서는 다 적는다"

    _narrow_until(window, widget, lambda: widget.fileSizeLabel.text() != full)

    assert shown(widget.fileSizeLabel) == "1080p · 28.99 GB"
    assert QLabel.text(widget.statusLabel) == widget.statusLabel.text()  # 말줄임 없음
    assert widget.directoryLabel.isVisible()

    seen = []
    _narrow_until(
        window,
        widget,
        lambda: seen.append(widget.fileSizeLabel.text()) or not widget.directoryLabel.isVisible(),
    )
    assert set(seen) == {"1080p · 28.99 GB"}


def test_preparing_card_shows_the_text_and_a_bar_without_a_value(qtbot):
    """준비 중인 카드는 "Preparing"을 적고 막대를 값 없는 상태(범위 0~0)로 둬야 한다.

    받는 중 카드에 preparing을 켬 -> 상태 문구 == "Preparing", 막대의 최대값 == 0
    끔 -> 상태 문구가 "42%"로 시작, 막대의 최대값 == 100, 값 == 42
    """
    window, widget = _card("video", DownloadState.RUNNING, preparing=True)
    qtbot.addWidget(window)

    assert shown(widget.statusLabel) == "Preparing"
    assert widget.progressBar.isVisible()
    assert widget.progressBar.maximum() == 0

    widget.item.preparing = False
    widget.setData(widget.item, 0)
    _pump()

    assert shown(widget.statusLabel).startswith("42%")
    assert (widget.progressBar.maximum(), widget.progressBar.value()) == (100, 42)


# ================================================================ 실제 창


def _mp4_head(scale: int):
    """실제 mp4 바이트(10fps 12프레임 · 1.2초)를 해석한 moov. scale로 샘플 크기를 키운다."""
    spec = video_spec()
    spec = dataclasses.replace(spec, sizes=[size * scale for size in spec.sizes])
    data = build_mp4([spec, audio_spec()]).data
    return read_mp4_head(lambda offset, size: data[offset : offset + size])


class _Probe:
    """조회 대역 — 해상도마다 다른 실제 moov를 돌려준다(1080p는 샘플이 480p의 3배)."""

    def __init__(self):
        self.heads = {1080: _mp4_head(3), 480: _mp4_head(1)}
        self.fail: set[int] = set()
        self.gate = threading.Event()  # 내려 두면 조회가 기다린다
        self.gate.set()

    def __call__(self, item) -> SectionBasis:
        assert self.gate.wait(5), "조회 대역의 문이 열리지 않았다"
        if item.resolution in self.fail:
            raise RuntimeError("조회 실패(대역)")
        head = self.heads[item.resolution]
        return SectionBasis(fps=head.index.fps, duration=head.index.duration, mp4_head=head)


class _Engine:
    """DownloadService 대역 — 제출을 적어 둔다. 테스트가 엔진처럼 콜백을 부른다."""

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
def probe(monkeypatch):
    fake = _Probe()
    monkeypatch.setattr(section_basis, "probe_section_basis", fake)
    return fake


@pytest.fixture
def window(probe, tmp_path):
    """실제 메인 창 · 대기 카드 하나(1080p · 480p, 10fps) · 서비스 대역."""
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {"title": "제목", "category": "", "channelName": "채널", "createdDate": "", "duration": 1},
        [StreamEntry(1080, "u1", frame_rate=10.0), StreamEntry(480, "u2", frame_rate=10.0)],
        None,
        "",
        str(tmp_path),
        "video",
        None,
    )
    item.total_size = "28.99 GB"
    win = VodDownloader()
    win.resize(1400, win.height())
    win.show()
    QTest.qWaitForWindowExposed(win)
    win.contentManager.model.addItem(item)
    _pump()
    engine = _Engine()
    win.downloadViewModel._service = engine
    yield win, item, engine
    for submission in engine.submissions:
        release_output_paths(submission["content"].selection_paths)


def _give_section(qtbot, win, item) -> None:
    """편집 창으로 구간 하나(0.2초~1.0초 — 10fps의 프레임 2~10)를 넣는다."""
    QTest.mouseClick(win.listView.widgetFor(item).fileSizeLabel, Qt.MouseButton.LeftButton)
    _pump()
    dialog = win._sectionDialog
    qtbot.waitUntil(lambda: dialog.viewModel().state == "ready", timeout=3000)
    _pump()
    for edit, digits in (
        (dialog._rows[0].startEdit, "00000002"),
        (dialog._rows[0].endEdit, "00000100"),
    ):
        QTest.keyClick(edit, Qt.Key.Key_Delete)
        QTest.keyClicks(edit, digits)
        edit.commit()
    _pump()
    dialog.okButton.click()
    _pump()
    assert item.selections == (TimeRange(0.2, 1.0),), "전제: 구간이 쓰여야 한다"


def _pick(win, item, resolution: int) -> None:
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


def test_confirming_sections_computes_the_bytes_the_engine_will_receive(qtbot, window, probe):
    """구간을 확인하면 카드에 받을 구간의 합이 그 moov로 계산돼 있어야 하고, 대기 카드가 그것을 적어야 한다.

    1080p에서 구간 0.2~1.0초를 확인
    -> section_bytes == sections_download_size(1080p의 색인, 그 구간), 요약이 그 크기(KB)로 끝난다
    """
    win, item, _engine = window

    _give_section(qtbot, win, item)

    expected = sections_download_size(probe.heads[1080].index, item.selections)
    assert 1024 < expected < 1024 * 1024, "전제: KB 단위로 적히는 크기다"
    assert item.section_bytes == expected
    assert shown(win.listView.widgetFor(item).fileSizeLabel).endswith(f"· {expected / 1024:.2f} KB")


def test_changing_resolution_recomputes_the_section_total_from_the_new_moov(qtbot, window, probe):
    """해상도를 바꾸면 앞 해상도의 구간 합을 바로 지우고, 새 moov가 오면 그것으로 다시 계산해야 한다.

    1080p에서 구간을 확인한 뒤 480p를 고름(480p의 샘플은 1080p의 1/3)
    -> 고른 직후 section_bytes is None, 조회가 끝난 뒤 == 480p의 색인으로 센 값(1080p의 값과 다르다)
    """
    win, item, _engine = window
    _give_section(qtbot, win, item)
    before = item.section_bytes

    probe.gate.clear()
    try:
        _pick(win, item, 480)
        cleared = item.section_bytes
    finally:
        probe.gate.set()
    _settle(qtbot, win)

    expected = sections_download_size(probe.heads[480].index, item.selections)
    assert cleared is None
    assert item.section_bytes == expected
    assert expected != before


def test_card_takes_the_total_the_engine_decided_when_no_moov_was_available(qtbot, window, probe):
    """moov를 못 받은 구간 카드는 받기 시작 때 엔진이 정한 크기를 받을 크기로 삼아야 한다.

    1080p에서 구간을 확인한 뒤 480p를 고르되 480p의 조회가 실패, 전역 다운로드.
    엔진처럼 구간 수 1 · 전체 크기 4321바이트를 적고 진행을 한 번 알림
    -> 조회 실패 뒤 section_bytes is None, 진행 통지 뒤 == 4321
    """
    win, item, engine = window
    _give_section(qtbot, win, item)
    probe.fail.add(480)
    _pick(win, item, 480)
    _settle(qtbot, win)
    assert item.section_bytes is None

    win.downloadButton.click()
    _pump()
    submission = engine.submissions[0]
    submission["data"].sections_total = 1
    submission["data"].total_size = 4321
    submission["on_progress"](
        ProgressEvent(downloaded_size=0, total_size=4321, speed=0.0, active_threads=0)
    )
    qtbot.waitUntil(lambda: item.section_bytes == 4321, timeout=3000)
    assert item.transfer_bytes == 4321


def _first_progress(submission) -> None:
    """엔진이 준비를 끝내고 받기 전에 보내는 첫 진행 통지."""
    submission["on_progress"](
        ProgressEvent(downloaded_size=0, total_size=1000, speed=0.0, active_threads=0)
    )


def test_card_shows_preparing_until_the_engine_reports_progress(qtbot, window, monkeypatch):
    """준비가 길어지면 카드가 "Preparing"을 보이고, 엔진의 첫 진행 통지가 오면 사라져야 한다.

    "준비 중"을 켜는 지연을 0으로 둠. 전역 다운로드 → 타이머가 돈 뒤 → 첫 진행 통지
    -> 켜진 동안: preparing, 상태 문구 "Preparing", 막대의 최대값 0
    -> 통지 뒤: preparing이 꺼짐, 상태 문구가 "0%"로 시작, 막대의 최대값 100
    """
    win, item, engine = window
    win.downloadViewModel._prepareTimer.setInterval(0)
    win.downloadButton.click()
    widget = win.listView.widgetFor(item)

    qtbot.waitUntil(lambda: item.preparing, timeout=3000)
    _pump()
    assert shown(widget.statusLabel) == "Preparing"
    assert widget.progressBar.maximum() == 0

    _first_progress(engine.submissions[0])
    qtbot.waitUntil(lambda: not item.preparing, timeout=3000)
    _pump()

    assert shown(widget.statusLabel).startswith("0%")
    assert widget.progressBar.maximum() == 100


def test_a_short_prepare_never_shows_preparing(qtbot, window):
    """준비가 지연(0.5초)보다 먼저 끝나면 "Preparing"은 한 번도 보이지 않아야 한다.

    전역 다운로드 직후 첫 진행 통지가 옴
    -> 통지가 닿은 뒤 preparing이 꺼져 있고 타이머가 멈춰 있다(뒤늦게 켜지지 않는다),
       그사이 카드가 그린 상태 문구에 "Preparing"이 없다
    """
    win, item, engine = window
    widget = win.listView.widgetFor(item)
    seen = []
    win.contentManager.model.itemChanged.connect(lambda *_: seen.append(widget.statusLabel.text()))
    win.downloadButton.click()

    _first_progress(engine.submissions[0])
    qtbot.waitUntil(lambda: shown(widget.statusLabel).startswith("0%"), timeout=3000)

    assert not item.preparing
    assert not win.downloadViewModel._prepareTimer.isActive()
    assert "Preparing" not in seen


def test_preparing_is_cleared_when_the_download_fails_while_preparing(qtbot, window):
    """준비 중에 다운로드가 실패하면 "Preparing"이 남지 않아야 한다.

    지연 0으로 "준비 중"이 켜진 뒤 엔진이 실패를 알림
    -> 카드는 실패 상태, preparing이 꺼져 있다, 막대의 최대값 100
    """
    win, item, engine = window
    win.downloadViewModel._prepareTimer.setInterval(0)
    win.downloadButton.click()
    widget = win.listView.widgetFor(item)
    qtbot.waitUntil(lambda: item.preparing, timeout=3000)

    engine.submissions[0]["on_failed"](RuntimeError("준비 실패(대역)"))
    qtbot.waitUntil(lambda: item.downloadState == DownloadState.FAILED, timeout=3000)
    _pump()

    assert not item.preparing
    assert widget.progressBar.maximum() == 100


def test_memory_is_logged_at_each_point_of_the_section_download(qtbot, window, monkeypatch, caplog):
    """구간을 정해 받는 동안 정해진 시점마다 프로세스 메모리 줄을 순서대로 남겨야 한다.

    메모리 읽기를 고정값으로 바꿈. 편집 창에서 구간을 확인 → 전역 다운로드 → 완료 통지
    -> app.process_memory 로거의 시점 == [조회 끝, 편집 창 닫힘, 다운로드 시작, 다운로드 끝, 색인을 놓은 뒤]
    """
    win, item, engine = window
    monkeypatch.setattr(process_memory, "read_process_memory", lambda: {"RSS": 512 * MB})
    caplog.set_level(logging.INFO)

    _give_section(qtbot, win, item)
    win.downloadButton.click()
    _pump()
    item.downloadState = DownloadState.FINISHED  # 배치가 이 카드를 다시 고르지 않게 한다
    engine.submissions[0]["on_finished"]()
    qtbot.waitUntil(lambda: win.downloadViewModel.handle is None, timeout=3000)

    records = [r for r in caplog.records if r.name == "app.process_memory"]
    assert [r.getMessage() for r in records] == [
        "프로세스 메모리 [조회 끝] RSS 512.0MB",
        "프로세스 메모리 [편집 창 닫힘] RSS 512.0MB",
        "프로세스 메모리 [다운로드 시작] RSS 512.0MB",
        "프로세스 메모리 [다운로드 끝] RSS 512.0MB",
        "프로세스 메모리 [색인을 놓은 뒤] RSS 512.0MB",
    ]
