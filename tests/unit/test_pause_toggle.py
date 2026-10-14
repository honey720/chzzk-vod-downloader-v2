"""일시정지 · 재개가 짧은 간격으로 여러 번 눌려도 엔진과 화면이 어긋나지 않는지 (#309).

실제 메인 창에서 전역 다운로드 버튼과 카드의 ⏸를 누른다. 엔진 자리는 서비스 대역이 맡아
제출을 적어 둔다 — 상태 머신(``DownloadData.model``)은 제품의 것 그대로다.
"""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

import app.theme as theme
import main as main_module
from app.download_logger import DownloadLogger
from app.viewmodels.data import ContentItem
from app.views import mainWindow as mw_mod
from app.views.mainWindow import VodDownloader
from core.api.representations import StreamEntry
from core.models.download_state import DownloadState
from tests.unit.card_helpers import drop_new_top_levels, hold_style, snapshot_top_levels


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


@pytest.fixture(autouse=True)
def _environment(qapp, monkeypatch):
    """실제 QSS · 네트워크와 안내 창 차단 · 로그 파일 차단. 테스트가 띄운 창은 끝에 파괴한다."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))

    def no_network():
        raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", no_network)
    monkeypatch.setattr(DownloadLogger, "_setup_logging", lambda self: None)
    monkeypatch.setattr(mw_mod.QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(mw_mod.QMessageBox, "information", lambda *a, **k: None)
    before = snapshot_top_levels()
    yield
    drop_new_top_levels(before)


def _pump() -> None:
    for _ in range(3):
        QApplication.processEvents()


@pytest.fixture
def downloading(tmp_path):
    """받는 중인 카드 하나 — 실제 메인 창 · 서비스 대역. 전역 다운로드를 한 번 눌러 둔다."""
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {"title": "제목", "category": "", "channelName": "채널", "createdDate": "", "duration": 60},
        [StreamEntry(1080, "u1", frame_rate=60.0)],
        None,
        "",
        str(tmp_path),
        "video",
        None,
    )
    item.total_size = "10.00 MB"
    win = VodDownloader()
    win.resize(1200, win.height())
    win.show()
    QTest.qWaitForWindowExposed(win)
    win.contentManager.model.addItem(item)
    _pump()
    engine = _Engine()
    win.downloadViewModel._service = engine
    win.downloadButton.click()
    _pump()
    assert len(engine.submissions) == 1, "전제: 다운로드가 제출됐다"
    return win, item, engine


def _engine_state(engine: _Engine) -> DownloadState:
    return engine.submissions[-1]["data"].model.state


@pytest.mark.parametrize("presses", [1, 2, 9, 10])
def test_rapid_presses_leave_the_engine_the_card_and_the_button_in_the_same_state(
    downloading, presses
):
    """일시정지 버튼이 이벤트를 처리할 틈 없이 여러 번 눌려도 마지막 누름의 상태로 끝나고 셋이 같아야 한다.

    받는 중인 카드에서 전역 버튼과 카드의 ⏸를 번갈아 1 · 2 · 9 · 10번 누름(사이에 이벤트를 돌리지 않는다)
    -> 홀수 번: 엔진 PAUSED · 카드 PAUSED · 전역 버튼 "Download" / 짝수 번: RUNNING · RUNNING · "Pause"
    -> 엔진의 일시정지 신호(pause_event)가 그 상태와 같다(일시정지면 내려가 있다)
    """
    win, item, engine = downloading
    card = win.listView.widgetFor(item)

    for press in range(presses):
        if press % 2:
            QTest.mouseClick(card.pauseButton, Qt.MouseButton.LeftButton)
        else:
            win.downloadButton.click()
    _pump()

    expected = DownloadState.PAUSED if presses % 2 else DownloadState.RUNNING
    model = engine.submissions[0]["data"].model
    assert (model.state, item.downloadState) == (expected, expected)
    assert win.downloadButton.text() == ("Download" if presses % 2 else "Pause")
    assert model.pause_event.is_set() == (expected == DownloadState.RUNNING)
    assert len(engine.submissions) == 1, "누름이 새 다운로드를 시작하지 않았다"


def test_a_press_that_lands_after_the_engine_finished_changes_nothing(downloading):
    """엔진이 막 끝났고 그 알림이 아직 처리되지 않은 틈에 눌린 일시정지는 카드와 버튼을 바꾸지 않아야 한다.

    엔진의 상태를 FINISHED로 바꿈(끝났다는 알림은 아직 오지 않았다). 전역 버튼을 누름
    -> 일시정지 알림 0회, 카드는 PAUSED가 아니다, 전역 버튼의 글자 "Pause" 그대로
    """
    win, item, engine = downloading
    model = engine.submissions[0]["data"].model
    paused = QSignalSpy(win.downloadViewModel.paused)
    model.finish()

    win.downloadButton.click()
    _pump()

    assert paused.count() == 0
    assert item.downloadState != DownloadState.PAUSED
    assert win.downloadButton.text() == "Pause"


def test_a_download_started_after_a_paused_one_was_stopped_does_not_start_paused(downloading):
    """일시정지했다가 중지한 카드를 다시 받기 시작하면 새 다운로드는 일시정지를 이어받지 않아야 한다.

    받는 중 → 일시정지 → 중지 → 전역 다운로드
    -> 제출 2건, 둘째의 엔진 상태 RUNNING · 일시정지 신호가 올라가 있다, 카드 RUNNING, 버튼 "Pause"
    """
    win, item, engine = downloading
    win.downloadButton.click()
    _pump()
    assert _engine_state(engine) == DownloadState.PAUSED, "전제: 일시정지됐다"
    win.stopDownload()
    _pump()
    assert item.downloadState == DownloadState.WAITING, "전제: 대기로 돌아왔다"

    win.downloadButton.click()
    _pump()

    assert len(engine.submissions) == 2
    model = engine.submissions[1]["data"].model
    assert model is not engine.submissions[0]["data"].model
    assert model.state == DownloadState.RUNNING and model.pause_event.is_set()
    assert item.downloadState == DownloadState.RUNNING
    assert win.downloadButton.text() == "Pause"
