"""하단바 — 모니터링 다섯 칸 · 실행 결과 · ▶ ↔ ‖ · ■ (#504 — 셸 S2).

실제 메인 창에서 버튼을 누른다. 엔진 자리는 서비스 대역이 맡아 제출을 적어 두고, 진행 · 완료 ·
실패는 대역이 받은 콜백(제품이 넘긴 것)을 불러 낸다. 1초 갱신은 타이머의 timeout 신호로 낸다.
"""

from collections.abc import Iterator

import pytest
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QMessageBox

import app.theme as theme
import CVDv2 as main_module
from app.download.logger import DownloadLogger
from app.viewmodels.data import ContentItem
from app.viewmodels.item_state import ItemState
from app.viewmodels.footer_monitor import (
    TransferReading,
    combine_readings,
    transfer_reading,
)
from app.views import mainWindow as mw_mod
from app.views.mainWindow import VodDownloader
from app.widgets.view import ContentListView
from core.media.representations import StreamEntry
from core.models.download_state import DownloadState
from core.models.events import DownloadCounters, ProgressEvent
from tests.app.gui.card_helpers import drop_new_top_levels, hold_style, snapshot_top_levels
from typing import Never

MIB = 1024 * 1024


class _Engine:
    """DownloadService 대역 — 제출을 적어 둔다."""

    def __init__(self) -> None:
        self.submissions: list[dict] = []

    def submit(self, content, **kwargs) -> "_Engine":
        self.submissions.append({"content": content, **kwargs})
        return self

    def elapsed_seconds(self) -> float:
        return 1.0

    def wait(self, timeout=None) -> bool:
        return True


@pytest.fixture(autouse=True)
def _environment(qapp, monkeypatch) -> Iterator[None]:
    """실제 QSS · 네트워크와 안내 창 차단 · 로그 파일 차단. 테스트가 띄운 창은 끝에 파괴한다."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))

    def no_network() -> Never:
        raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", no_network)
    monkeypatch.setattr(DownloadLogger, "_setup_logging", lambda self: None)
    monkeypatch.setattr(mw_mod.QMessageBox, "information", lambda *a, **k: None)
    before = snapshot_top_levels()
    yield
    drop_new_top_levels(before)


@pytest.fixture
def answers(monkeypatch) -> tuple[list[str], dict[str, QMessageBox.StandardButton]]:
    """중지 확인 창 대역 — 물은 횟수를 적고, 정해 둔 답을 돌려준다(기본 예)."""
    asked: list[str] = []
    reply = {"button": QMessageBox.Yes}

    def warning(parent, title, text, *args, **kwargs) -> QMessageBox.StandardButton:
        asked.append(text)
        return reply["button"]

    monkeypatch.setattr(mw_mod.QMessageBox, "warning", warning)
    return asked, reply


def _pump() -> None:
    for _ in range(3):
        QApplication.processEvents()


def _card(path: str, n: int = 1, size: str | None = "10.00 MB") -> ContentItem:
    item = ContentItem(
        f"https://chzzk.naver.com/video/{n}",
        {
            "title": f"제목 {n}",
            "category": "",
            "channelName": "채널",
            "createdDate": "",
            "duration": 60,
        },
        [StreamEntry(1080, f"u{n}", frame_rate=60.0)],
        None,
        "",
        path,
        "video",
        None,
    )
    item.total_size = size
    return item


def _window(
    tmp_path, cards: int = 1, size: str | None = "10.00 MB"
) -> tuple[VodDownloader, list[ContentItem], _Engine]:
    """실제 메인 창 · 서비스 대역 · 대기 카드 `cards`장."""
    win = VodDownloader()
    # 1초 갱신은 테스트가 timeout 신호로 낸다(_tick) — 타이머가 스스로 돌면 갱신 횟수가 시간에 달린다
    win.monitorTimer.stop()
    win.resize(1200, win.height())
    win.show()
    QTest.qWaitForWindowExposed(win)
    items = [_card(str(tmp_path), n, size) for n in range(1, cards + 1)]
    for item in items:
        win.contentManager.model.addItem(item)
    _pump()
    engine = _Engine()
    win.downloadViewModel._service = engine
    return win, items, engine


def _tick(win) -> None:
    """1초 갱신 한 번 — 타이머의 timeout 신호로."""
    win.monitorTimer.timeout.emit()
    _pump()


def _progress(engine, downloaded: int, total: int | None, speed_mb: float, threads: int) -> None:
    """마지막 제출의 진행 콜백에 통지 하나를 넣는다(파일 다운로드 — 세그먼트 기반 아님)."""
    event = ProgressEvent(
        downloaded_size=downloaded,
        total_size=total,
        speed=speed_mb,
        active_threads=threads,
        counters=DownloadCounters(total_size=total),
    )
    engine.submissions[-1]["on_progress"](event)
    _pump()


def _finish(engine) -> None:
    """마지막 제출을 완료로 끝낸다 — 상태 기계를 끝내고 완료 콜백을 부른다."""
    submission = engine.submissions[-1]
    submission["data"].model.finish()
    submission["on_finished_counters"](DownloadCounters())
    _pump()


def _fail(engine) -> None:
    """마지막 제출을 실패로 끝낸다."""
    submission = engine.submissions[-1]
    submission["data"].model.fail()
    submission["on_failed_key"](None, DownloadCounters())
    _pump()


def _free_measured(qtbot, win) -> None:
    """지금 저장 위치의 여유 공간을 다 잴 때까지 기다린다."""
    qtbot.waitUntil(lambda: win.freeSpace.value(win.saveLocations.current()) is not None)
    _pump()


# ---------------------------------------------------------------------------
# ① 값 묶기 — 순수 계산
# ---------------------------------------------------------------------------


def test_reading_of_a_file_download_is_the_size_left() -> None:
    """받을 크기를 아는 파일 다운로드의 남은 바이트는 받을 크기 − 받은 크기여야 한다.

    받을 크기 10 MiB, 받은 크기 4 MiB, 속도 2 MB/s, 연결 3
    -> 남은 6 MiB · 속도 2 MiB/s · 연결 3
    """
    event = ProgressEvent(4 * MIB, 10 * MIB, 2.0, 3)
    reading = transfer_reading(event, DownloadCounters(total_size=10 * MIB), False)
    assert reading == TransferReading(2.0 * MIB, 3, 6.0 * MIB)


def test_reading_of_a_segment_download_counts_the_segments_left() -> None:
    """세그먼트 기반 다운로드의 남은 바이트는 받은 세그먼트의 평균 크기 × 남은 세그먼트 수여야 한다.

    세그먼트 10개 중 4개 끝냄(그중 1개는 다시 씀), 받은 크기 3 MiB
    -> 평균 1 MiB × 남은 6개 = 6 MiB
    """
    counters = DownloadCounters(max_threads=10, completed_threads=4, reused_threads=1)
    reading = transfer_reading(ProgressEvent(3 * MIB, None, 1.0, 2), counters, True)
    assert reading.remaining_bytes == 6 * MIB


@pytest.mark.parametrize(
    ("event", "counters", "segmented"),
    [
        (ProgressEvent(4 * MIB, None, 2.0, 3), DownloadCounters(), False),  # 받을 크기 모름
        (ProgressEvent(0, None, 0.0, 0), DownloadCounters(max_threads=10), True),  # 받은 것 없음
        (ProgressEvent(4 * MIB, 10 * MIB, 0.0, 0), DownloadCounters(merging=True), False),  # 후처리
    ],
)
def test_reading_without_a_basis_has_no_size_left(event, counters, segmented) -> None:
    """받을 크기를 모름 · 받은 세그먼트 없음 · 후처리 단계면 남은 바이트는 None이어야 한다."""
    assert transfer_reading(event, counters, segmented).remaining_bytes is None


def test_snapshot_without_a_download_has_only_free_space() -> None:
    """받는 중인 다운로드가 없으면 묶은 값은 받는 중이 아니고 여유 공간만 들어야 한다.

    읽은 값 없음 · 메모리 100 · 여유 500 -> receiving 거짓, memory None, free 500
    """
    snapshot = combine_readings([], 100, 500)
    assert (snapshot.receiving, snapshot.memory, snapshot.free) == (False, None, 500)


def test_snapshot_time_left_is_the_size_left_over_the_speed() -> None:
    """전체 남은 시간은 남은 바이트의 합 ÷ 전체 속도여야 한다.

    (속도 2 MiB/s · 남은 6 MiB) + (1 MiB/s · 3 MiB) -> 9 MiB ÷ 3 MiB/s = 3초, 연결 합
    """
    readings = [TransferReading(2 * MIB, 3, 6 * MIB), TransferReading(1 * MIB, 2, 3 * MIB)]
    snapshot = combine_readings(readings, None, None)
    assert (snapshot.remaining_seconds, snapshot.connections) == (3.0, 5)


def test_snapshot_time_left_is_dropped_when_one_download_has_no_size_left() -> None:
    """남은 바이트를 셀 수 없는 다운로드가 하나라도 섞이면 전체 남은 시간은 None이어야 한다."""
    readings = [TransferReading(2 * MIB, 3, 6 * MIB), TransferReading(1 * MIB, 2, None)]
    assert combine_readings(readings, None, None).remaining_seconds is None


# ---------------------------------------------------------------------------
# ② 모니터링 칸 — 실제 창
# ---------------------------------------------------------------------------


def test_footer_without_a_download_shows_only_free_space(qtbot, tmp_path) -> None:
    """받는 중인 카드가 없으면 하단바 모니터링은 여유 공간 한 칸만 보여야 한다.

    대기 카드 1장, 다운로드 시작 전 -> 보이는 칸 ["free"], 글 "Free … GB", 툴팁 "Free space · … GB"
    """
    win, _items, _engine = _window(tmp_path)
    _free_measured(qtbot, win)
    _tick(win)
    assert win.footerMonitor.isVisible()
    assert win.footerMonitor.shownKeys() == ["free"]
    label = win.footerMonitor.labels["free"]
    assert label.text().startswith("Free ") and label.text().endswith(" GB")
    assert label.toolTip().startswith("Free space · ")


def test_footer_while_receiving_shows_all_five_values(qtbot, tmp_path, answers) -> None:
    """받을 크기를 아는 카드를 받는 중이면 다섯 칸이 다 보이고 값이 통지를 따라야 한다.

    받을 크기 10 MiB · 받은 크기 0 · 속도 2.0 MB/s · 연결 4 통지
    -> 칸 speed · remain · conn · mem · free, "↓ 2.0 MB/s", "0:05 left", "Connections 4", "Memory … MB"
    """
    win, _items, engine = _window(tmp_path)
    _free_measured(qtbot, win)
    win.downloadButton.click()
    _pump()
    _progress(engine, 0, 10 * MIB, 2.0, 4)
    _tick(win)
    labels = win.footerMonitor.labels
    assert win.footerMonitor.shownKeys() == ["speed", "remain", "conn", "mem", "free"]
    assert labels["speed"].text() == "↓ 2.0 MB/s"
    assert labels["speed"].toolTip() == "Speed · 2.0 MB/s"
    assert labels["remain"].text() == "0:05 left"
    assert labels["conn"].text() == "Connections 4"
    assert labels["conn"].toolTip() == "Connections · 4"
    assert labels["mem"].text().startswith("Memory ") and labels["mem"].text().endswith(" MB")
    assert labels["mem"].toolTip().startswith("Memory · ")


def test_footer_drops_the_time_left_when_the_size_is_unknown(qtbot, tmp_path, answers) -> None:
    """받을 크기를 모르는 카드를 받는 중이면 남은 시간 칸만 빠져야 한다.

    받을 크기 None · 속도 2.0 MB/s · 연결 4 통지 -> 칸 speed · conn · mem · free
    """
    win, _items, engine = _window(tmp_path, size=None)
    _free_measured(qtbot, win)
    win.downloadButton.click()
    _pump()
    _progress(engine, 1 * MIB, None, 2.0, 4)
    _tick(win)
    assert win.footerMonitor.shownKeys() == ["speed", "conn", "mem", "free"]


def test_footer_while_paused_shows_only_free_space(qtbot, tmp_path, answers) -> None:
    """모두 일시정지하면 받는 것이 없어 하단바 모니터링은 여유 공간만 보여야 한다."""
    win, _items, engine = _window(tmp_path)
    _free_measured(qtbot, win)
    win.downloadButton.click()
    _pump()
    _progress(engine, 0, 10 * MIB, 2.0, 4)
    win.downloadButton.click()  # ‖ 모두 일시정지
    _pump()
    assert win.footerMonitor.shownKeys() == ["free"]


def test_a_tick_redraws_only_the_footer(qtbot, tmp_path, answers, monkeypatch) -> None:
    """1초 갱신은 카드 1000장에서도 카드 갱신 통지 · 목록 전체 맞추기를 내지 않아야 한다.

    카드 1000장 · 받는 중 · 통지 하나 -> 갱신 한 번 동안 itemChanged 0건 · 목록 다시 배치(_relayout) 0회 ·
    모든 카드 보임 맞추기(_applyFilter) 0회
    """
    # 창을 만들기 전에 감싼다 — 목록이 순서 신호(orderReset · filterChanged)에 이을 때 감싼 함수를 붙잡아,
    # 신호로 오든 직접 부르든 실제로 목록 전체를 맞춘 횟수를 센다(창을 만든 뒤 바꾸면 신호 연결은 원래 함수다)
    whole_list: list[str] = []
    for name in ("_relayout", "_applyFilter"):
        original = getattr(ContentListView, name)

        def counted(self, _original=original, _name=name) -> None:
            whole_list.append(_name)
            return _original(self)

        monkeypatch.setattr(ContentListView, name, counted)
    win, _items, engine = _window(tmp_path, cards=1000)
    win.downloadButton.click()
    _pump()
    _progress(engine, 0, 10 * MIB, 2.0, 4)
    assert whole_list, "전제: 감싼 함수가 목록에 이어졌다(창을 만들 때 한 번은 맞춘다)"
    changed = QSignalSpy(win.contentManager.model.itemChanged)
    whole_list.clear()
    _tick(win)
    assert win.footerMonitor.shownKeys()[0] == "speed", "전제: 받는 중의 값이 보인다"
    assert changed.count() == 0
    assert whole_list == []


# ---------------------------------------------------------------------------
# ③ 실행 결과
# ---------------------------------------------------------------------------


def test_result_appears_when_the_run_ends(tmp_path, answers) -> None:
    """받던 카드가 모두 끝나 다음 카드가 없으면 「완료 N」이 보여야 한다.

    대기 카드 1장 → 다운로드 → 완료 -> 결과 글 "Completed 1"(실패 없음 — 실패 부분 없음)
    """
    win, _items, engine = _window(tmp_path)
    win.downloadButton.click()
    _pump()
    assert not win.runResultLabel.isVisible(), "전제: 받는 중에는 결과가 없다"
    _finish(engine)
    assert win.runResultLabel.isVisible()
    assert win.runResultLabel.text() == "Completed 1"


def test_result_counts_the_failed_cards(tmp_path, answers) -> None:
    """끝난 작업에 실패 카드가 있으면 결과에 「 · 실패 N」이 붙어야 한다.

    대기 카드 2장 → 첫 장 완료 → 둘째 장 실패 -> "Completed 1 · Failed 1"
    """
    win, _items, engine = _window(tmp_path, cards=2)
    win.downloadButton.click()
    _pump()
    _finish(engine)
    assert len(engine.submissions) == 2, "전제: 둘째 장을 이어 받는다"
    _fail(engine)
    assert win.runResultLabel.text() == "Completed 1 · Failed 1"


def test_result_goes_when_a_waiting_card_is_added(tmp_path, answers) -> None:
    """결과가 보이는 동안 대기 카드가 생기면 결과는 사라져야 한다."""
    win, _items, engine = _window(tmp_path)
    win.downloadButton.click()
    _pump()
    _finish(engine)
    assert win.runResultLabel.isVisible(), "전제: 결과가 보인다"
    win.contentManager.model.addItem(_card(str(tmp_path), 9))
    _pump()
    assert not win.runResultLabel.isVisible()


def test_result_goes_when_the_next_run_starts(tmp_path, answers) -> None:
    """결과가 보이는 동안 다음 다운로드를 시작하면 결과는 사라지고 그 작업이 끝날 때 다시 보여야 한다.

    완료 1 → 실패 카드를 ↻로 대기 → 다운로드 -> 받는 중 결과 없음 → 완료 -> "Completed 2"
    """
    win, items, engine = _window(tmp_path, cards=2)
    win.downloadButton.click()
    _pump()
    _finish(engine)
    _fail(engine)
    assert win.runResultLabel.text() == "Completed 1 · Failed 1", "전제"
    win.listView.widgetFor(items[1]).retryButton.click()
    _pump()
    win.downloadButton.click()
    _pump()
    assert not win.runResultLabel.isVisible()
    _finish(engine)
    assert win.runResultLabel.text() == "Completed 2"


def test_result_follows_deleting_a_card_while_shown(tmp_path, answers) -> None:
    """결과가 보이는 동안 실패 카드를 지우면 숫자가 지금 목록을 따르고, 목록이 비면 사라져야 한다.

    완료 1 · 실패 1 → 실패 카드 지움 -> "Completed 1" → 완료 카드 지움 -> 결과 없음
    """
    win, items, engine = _window(tmp_path, cards=2)
    win.downloadButton.click()
    _pump()
    _finish(engine)
    _fail(engine)
    win.contentManager.removeItem(items[1])
    _pump()
    assert win.runResultLabel.text() == "Completed 1"
    win.contentManager.removeItem(items[0])
    _pump()
    assert not win.runResultLabel.isVisible()


def test_deleting_cards_without_a_download_shows_no_result(tmp_path) -> None:
    """다운로드 없이 카드를 지워 완료 카드만 남아도 결과는 나타나지 않아야 한다.

    완료 카드 1장 + 대기 카드 1장 → 대기 카드 지움 -> 결과 없음
    """
    win, items, _engine = _window(tmp_path)
    done = _card(str(tmp_path), 7)
    done.downloadState = DownloadState.FINISHED
    win.contentManager.model.addItem(done)
    _pump()
    win.contentManager.removeItem(items[0])
    _pump()
    assert not win.runResultLabel.isVisible()


# ---------------------------------------------------------------------------
# ④ 버튼
# ---------------------------------------------------------------------------


def test_download_button_toggles_between_play_and_pause(tmp_path, answers) -> None:
    """다운로드 버튼은 받는 중이면 ‖ 「모두 일시정지」, 일시정지면 ▶ 「이어 받기」여야 한다.

    시작 전 ▶ "Download" → 누름 ‖ "Pause all" → 누름 ▶ "Resume"(카드 PAUSED) → 누름 ‖ "Pause all"
    """
    win, items, _engine = _window(tmp_path)
    button = win.downloadButton
    assert (button.iconName(), button.toolTip()) == ("resume", "Download")
    button.click()
    _pump()
    assert (button.iconName(), button.toolTip()) == ("pause", "Pause all")
    button.click()
    _pump()
    assert (button.iconName(), button.toolTip()) == ("resume", "Resume")
    assert items[0].downloadState == DownloadState.PAUSED
    button.click()
    _pump()
    assert (button.iconName(), button.toolTip()) == ("pause", "Pause all")


def test_stop_asks_first_and_returns_the_card_to_waiting(tmp_path, answers) -> None:
    """■를 누르면 확인 창을 묻고, 예를 고르면 받는 중인 카드가 대기로 돌아가야 한다.

    받는 중 → ■ · 답 예 -> 물은 횟수 1, 카드 WAITING, 다운로드 버튼 ▶ "Download", ■ 꺼짐
    """
    asked, _reply = answers
    win, items, _engine = _window(tmp_path)
    assert win.stopButton.toolTip() == "Stop"
    win.downloadButton.click()
    _pump()
    assert win.stopButton.isEnabled(), "전제: 받는 중에는 ■를 누를 수 있다"
    win.stopButton.click()
    _pump()
    assert len(asked) == 1
    assert items[0].downloadState == DownloadState.WAITING
    assert win.downloadButton.toolTip() == "Download"
    assert not win.stopButton.isEnabled()


def test_stop_answered_no_keeps_the_download(tmp_path, answers) -> None:
    """■ 확인 창에서 아니오를 고르면 다운로드가 그대로여야 한다.

    받는 중 → ■ · 답 아니오 -> 물은 횟수 1, 카드 RUNNING, 다운로드 버튼 ‖ "Pause all"
    """
    asked, reply = answers
    reply["button"] = QMessageBox.No
    win, items, _engine = _window(tmp_path)
    win.downloadButton.click()
    _pump()
    win.stopButton.click()
    _pump()
    assert len(asked) == 1
    assert items[0].downloadState == DownloadState.RUNNING
    assert win.downloadButton.toolTip() == "Pause all"


# ---------------------------------------------------------------------------
# ⑤ 창 최소폭
# ---------------------------------------------------------------------------


def test_footer_values_do_not_raise_the_window_minimum_width(qtbot, tmp_path, answers) -> None:
    """모니터링 다섯 칸 · 실행 결과가 보여도 콘텐츠 최소폭은 받는 것이 없을 때와 같아야 한다.

    대기 카드 1장, 다섯 칸이 보이기 전의 최소폭 = 받는 중(긴 값) · 결과가 보일 때의 최소폭 = 창 최소폭
    """
    win, _items, engine = _window(tmp_path)
    idle = win._contentMinimumWidth()
    assert idle == win.minimumWidth(), "전제: 창을 만들 때와 같은 최소폭"
    win.downloadButton.click()
    _pump()
    _progress(engine, 0, 10 * MIB, 987.6, 48)
    _tick(win)
    assert len(win.footerMonitor.shownKeys()) >= 4, "전제: 받는 중의 칸이 보인다"
    receiving = win._contentMinimumWidth()
    _finish(engine)
    assert win.runResultLabel.isVisible(), "전제: 결과가 보인다"
    finished = win._contentMinimumWidth()
    assert (receiving, finished) == (idle, idle)


def test_footer_values_do_not_raise_the_footer_minimum_width(qtbot, tmp_path, answers) -> None:
    """하단바 칸의 최소폭은 값을 보여도 글의 폭만큼 늘지 않아야 한다 — 칸마다 1px와 간격만 는다.

    받는 것 없음(칸 0~1개) → 받는 중(긴 값, 칸 4~5개)
    -> 하단바 최소폭이 는 양 == 는 칸 수 × (1 + 칸 사이 간격)
    (하단바는 헤더바보다 좁아 창 최소폭으로는 이 차이가 드러나지 않는다)
    """
    win, _items, engine = _window(tmp_path)
    # 여유 공간 칸이 보인 뒤에 기준을 잡는다 — 칸 0개에서 시작하면 첫 칸은 간격을 받지 않아 한 칸만큼 어긋난다
    _free_measured(qtbot, win)
    _tick(win)
    assert win.footerMonitor.shownKeys() == ["free"], "전제: 기준은 여유 공간 한 칸"
    win._contentMinimumWidth()  # 레이아웃 캐시를 비운다
    before = win.infoFrame.minimumSizeHint().width()
    shown_before = len(win.footerMonitor.shownKeys())
    win.downloadButton.click()
    _pump()
    _progress(engine, 0, 10 * MIB, 987.6, 48)
    _tick(win)
    win._contentMinimumWidth()
    after = win.infoFrame.minimumSizeHint().width()
    added = len(win.footerMonitor.shownKeys()) - shown_before
    assert added >= 3, "전제: 받는 중의 칸이 더 보인다"
    gap = win.footerMonitor.layout().spacing()
    assert after - before == added * (1 + gap), f"하단바 최소폭 {before} → {after}, 는 칸 {added}개"


def test_window_minimum_width_does_not_depend_on_the_cards(tmp_path) -> None:
    """창 최소폭은 카드가 0장이든 50장이든 같아야 한다."""
    empty, _items, _engine = _window(tmp_path, cards=0)
    full, _items, _engine = _window(tmp_path, cards=50)
    assert empty.minimumWidth() == full.minimumWidth() == full._contentMinimumWidth()


# ---------------------------------------------------------------------------
# ⑥ 다운로드 버튼(▶) 켜짐 — 시작할 것이 있을 때만 (#504, 오너 2026-11-07)
# ---------------------------------------------------------------------------


def test_download_button_is_off_for_an_empty_list(tmp_path) -> None:
    """목록이 비면 다운로드 버튼은 꺼지고 도형은 비활성 색이어야 한다.

    카드 0장 -> isEnabled 거짓, 도형 색 토큰 "textDisabled"
    """
    win, _items, _engine = _window(tmp_path, cards=0)
    assert not win.downloadButton.isEnabled()
    assert win.downloadButton.colorToken() == "textDisabled"


def test_download_button_is_on_with_a_waiting_card(tmp_path) -> None:
    """대기 카드가 한 장이라도 있으면 다운로드 버튼은 켜져야 한다."""
    win, _items, _engine = _window(tmp_path, cards=1)
    assert win.downloadButton.isEnabled()


def test_download_button_waits_for_a_card_still_loading(tmp_path) -> None:
    """조회 중 카드만 있으면 꺼지고, 그 카드가 대기가 되면 켜져야 한다.

    조회 중 카드 1장 -> 꺼짐 / 그 카드를 대기로 바꿔 알림 -> 켜짐
    """
    win, _items, _engine = _window(tmp_path, cards=0)
    card = _card(str(tmp_path), 5)
    card.downloadState = ItemState.LOADING
    win.contentManager.model.addItem(card)
    _pump()
    assert not win.downloadButton.isEnabled()
    card.downloadState = DownloadState.WAITING
    win.contentManager.model.notifyChanged(card)
    _pump()
    assert win.downloadButton.isEnabled()


def test_download_button_stays_on_while_receiving_and_paused(tmp_path, answers) -> None:
    """받는 중(‖)과 일시정지(▶ 이어 받기)에는 대기 카드가 없어도 켜져 있어야 한다.

    카드 1장 → 다운로드(대기 카드 0) -> 켜짐 · ‖ → 일시정지 -> 켜짐 · ▶
    """
    win, _items, _engine = _window(tmp_path, cards=1)
    win.downloadButton.click()
    _pump()
    assert (win.downloadButton.isEnabled(), win.downloadButton.iconName()) == (True, "pause")
    win.downloadButton.click()
    _pump()
    assert (win.downloadButton.isEnabled(), win.downloadButton.iconName()) == (True, "resume")


def test_download_button_is_off_when_only_finished_and_failed_cards_remain(
    tmp_path, answers
) -> None:
    """작업이 끝나 완료 · 실패 카드만 남으면 다운로드 버튼은 꺼져야 한다.

    카드 2장 → 첫 장 완료 · 둘째 장 실패 -> 꺼짐
    """
    win, _items, engine = _window(tmp_path, cards=2)
    win.downloadButton.click()
    _pump()
    _finish(engine)
    _fail(engine)
    assert win.runResultLabel.isVisible(), "전제: 작업이 끝났다"
    assert not win.downloadButton.isEnabled()


def test_download_button_turns_off_when_the_last_waiting_card_is_deleted(tmp_path) -> None:
    """마지막 대기 카드를 지우면 다운로드 버튼은 꺼져야 한다.

    대기 카드 1장 + 완료 카드 1장 → 대기 카드 지움 -> 꺼짐
    """
    win, items, _engine = _window(tmp_path, cards=1)
    done = _card(str(tmp_path), 7)
    done.downloadState = DownloadState.FINISHED
    win.contentManager.model.addItem(done)
    _pump()
    assert win.downloadButton.isEnabled(), "전제: 대기 카드가 있다"
    win.contentManager.removeItem(items[0])
    _pump()
    assert not win.downloadButton.isEnabled()


def test_download_button_says_none_can_start_when_the_waiting_card_is_being_edited(
    tmp_path, answers
) -> None:
    """대기 카드가 구간 편집 창에 열려 있으면 ▶는 켜져 있되 누르면 「지금 시작할 수 있는 VOD가 없다」 안내여야 한다.

    대기 카드 1장, 그 카드의 구간 편집 시작 -> ▶ 켜짐, 누르면 안내 글 "No VOD can be started right now."
    한 번 · 제출 0건
    """
    asked, _reply = answers
    win, items, engine = _window(tmp_path, cards=1)
    win.contentManager.beginSectionEdit(items[0])
    assert win.downloadButton.isEnabled(), "전제: 대기 카드가 있어 켜져 있다"
    win.downloadButton.click()
    _pump()
    assert asked == ["No VOD can be started right now."]
    assert engine.submissions == []
