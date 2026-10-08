"""다운로드가 끝난 뒤 moov 색인이 메모리에서 풀리는지 (#309).

긴 영상의 해석된 색인은 수백 MB~1GB다. 앱이 "놓았다"고 적어도 어딘가가 쥐고 있으면
프로세스 메모리는 줄지 않는다. 여기서는 대역 없이 실제 배선을 탄다 — 메인 창 · 편집 창 ·
실제 조회(``app.section_basis``) · 실제 서비스(``DownloadService``) · 실제 엔진
(``FileDownloader``) · 실제 ffmpeg. 요청만 소켓 없이 메모리의 mp4로 답하는 호스트로 보낸다.

판정은 색인(``Mp4Head``)에 건 약한 참조다. 다운로드가 끝난 뒤(완료 · 실패 · 정지 · 일부 실패
뒤 기록을 지움) **순환 쓰레기 수집 없이** 죽어 있어야 한다 — 테스트 동안 수집기를 꺼 둔다.
서로를 가리키는 객체들(핸들 ↔ 엔진)에 색인이 매달려 있으면 참조가 다 끊겨도 수집기가 돌
때까지 남는데, 오래 사는 객체가 수백만 개인 프로세스에서는 전체 수집이 드물게 돈다 —
실기에서 "놓았다"고 적은 뒤에도 메모리가 줄지 않던 까닭이다. 살아 있으면 누가 쥐고
있는지 참조 사슬을 실패 메시지에 적는다.
"""

import gc
import logging
import os
import subprocess
import threading
import time
import types
import weakref

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import app.process_memory as process_memory
import app.theme as theme
import app.viewmodels.section_edit_viewmodel as section_edit_module
import core.api.mp4 as mp4_module
import core.downloaders.file_downloader as fd_module
import core.utils.ffmpeg as ffmpeg_module
import core.utils.hybrid_cut as cut_module
import main as main_module
from app.download_logger import DownloadLogger
from app.viewmodels.data import ContentItem
from app.views import mainWindow as mw_mod
from app.views.mainWindow import VodDownloader
from core.api.representations import StreamEntry
from core.models.download_state import DownloadState
from core.models.mp4_index import Mp4Head
from core.utils.ffmpeg import get_ffmpeg_exe
from tests.unit.section_input import enter_time
from tests.unit.card_helpers import drop_new_top_levels, hold_style, snapshot_top_levels
from tests.unit.core.long_ffmpeg import LONG_RUNNING, end_all, record_processes
from tests.unit.core.range_host import RangeHost

FINISH_TIMEOUT = 60_000  # ms — 실제 ffmpeg로 구간 하나를 자른다


@pytest.fixture(scope="module")
def server(tmp_path_factory) -> RangeHost:
    """30fps 6초짜리 mp4 하나를 내주는 호스트 — 1초마다 키프레임, moov가 앞에 있다."""
    path = str(tmp_path_factory.mktemp("release_source") / "plain.mp4")
    done = subprocess.run(
        [
            get_ffmpeg_exe(),
            "-hide_banner",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=30:duration=6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=6",
            "-c:a",
            "aac",
            "-ac",
            "2",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-bf",
            "2",
            "-g",
            "30",
            "-movflags",
            "+faststart",
            path,
        ],  # fmt: skip
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert done.returncode == 0, done.stderr
    with open(path, "rb") as f:
        return RangeHost({"plain": f.read()})


@pytest.fixture(autouse=True)
def _environment(qapp, server, monkeypatch):
    """실제 QSS · 요청은 호스트로 · 카드의 크기 조회와 안내 창 차단 · 로그 파일 차단."""
    theme.set_color_scheme("dark")
    qapp.setStyle(hold_style(theme.build_style()))
    qapp.setPalette(theme.build_palette())
    qapp.setStyleSheet(theme.load_stylesheet(main_module.resource_path(theme.QSS_RELATIVE_PATH)))

    def no_network():
        raise RuntimeError("network disabled in tests")

    monkeypatch.setattr("app.widgets.widget.get_thread_session", no_network)
    monkeypatch.setattr(fd_module, "get_thread_session", server.session)
    monkeypatch.setattr(mp4_module, "get_thread_session", server.session)
    monkeypatch.setattr(DownloadLogger, "_setup_logging", lambda self: None)
    monkeypatch.setattr(mw_mod.QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(mw_mod.QMessageBox, "information", lambda *a, **k: None)
    # 색인이 사라진 뒤의 메모리 줄을 틈 없이 적게 한다 — 앞 테스트의 것이 뒤 테스트로 넘어오지 않게
    monkeypatch.setattr(section_edit_module, "INDEX_RELEASE_LOG_DELAY_MS", 0)
    before = snapshot_top_levels()
    gc.collect()
    gc.disable()  # 참조 계수만으로 풀리는지 본다 — 순환 수집이 가려 주지 않게
    try:
        yield
    finally:
        gc.enable()
    drop_new_top_levels(before)


def _pump():
    for _ in range(3):
        QApplication.processEvents()


@pytest.fixture
def window(server, tmp_path):
    """실제 메인 창과 대기 카드 하나 — 서비스와 엔진은 제품의 것 그대로다."""
    item = ContentItem(
        "https://chzzk.naver.com/video/1",
        {"title": "제목", "category": "", "channelName": "채널", "createdDate": "", "duration": 6},
        [StreamEntry(1080, server.url("plain"), frame_rate=30.0)],
        None,
        "",
        str(tmp_path),
        "video",
        None,
    )
    item.total_size = "1.00 MB"
    win = VodDownloader()
    win.resize(1200, win.height())
    win.show()
    QTest.qWaitForWindowExposed(win)
    win.contentManager.model.addItem(item)
    _pump()
    return win, item


def _give_section(
    qtbot, win, item, start: str = "00000105", end: str = "00000220", wait_size: bool = True
) -> None:
    """편집 창으로 구간 하나를 넣는다 — 기본은 1초 5프레임~2초 20프레임(프레임 35~80)."""
    QTest.mouseClick(win.listView.widgetFor(item).fileSizeLabel, Qt.MouseButton.LeftButton)
    _pump()
    dialog = win._sectionDialog
    qtbot.waitUntil(lambda: dialog.viewModel().state == "ready", timeout=10_000)
    _pump()
    for edit, digits in ((dialog._rows[0].startEdit, start), (dialog._rows[0].endEdit, end)):
        enter_time(edit, digits)  # 끝 두 자리는 프레임 칸, 그 앞은 시분초 칸
    _pump()
    dialog.okButton.click()
    _pump()
    assert len(item.selections) == 1, "전제: 구간이 쓰여야 한다"
    if not wait_size:
        return
    # 받을 크기를 세는 백그라운드 일이 끝나면 카드가 받은 바이트 대신 만든 색인을 쥔다
    sizer = win.contentManager._sectionSizer
    qtbot.waitUntil(lambda: sizer.pendingCount() == 0, timeout=10_000)
    _pump()


def _held_head(item) -> weakref.ref:
    """카드가 든 moov에 약한 참조를 건다 — 강한 참조를 이 함수 밖으로 내보내지 않는다."""
    kept = item.section_head
    assert kept is not None and isinstance(kept[1], Mp4Head), "전제: 카드가 실제 moov를 들고 있다"
    return weakref.ref(kept[1])


def _collect() -> None:
    """지연 삭제와 남은 통지를 배달한다. 쓰레기 수집은 돌리지 않는다."""
    for _ in range(3):
        _pump()


def _holders(target, depth: int = 6) -> str:
    """target을 쥔 참조 사슬을 글로 적는다 — 실패 메시지용. 프레임 · 이 함수의 지역은 뺀다."""
    lines: list[str] = []
    seen = {id(target)}
    level = [(target, type(target).__name__)]
    for _ in range(depth):
        following = []
        for obj, path in level:
            for holder in gc.get_referrers(obj):
                if id(holder) in seen or isinstance(holder, types.FrameType):
                    continue
                if holder is level or holder is following or holder is lines:
                    continue
                seen.add(id(holder))
                name = type(holder).__name__
                if isinstance(holder, dict):
                    keys = [key for key, value in holder.items() if value is obj]
                    name = f"dict{keys[:3]}"
                elif isinstance(holder, types.CellType):
                    name = "closure cell"
                elif isinstance(holder, (types.FunctionType, types.MethodType)):
                    name = f"function {getattr(holder, '__qualname__', '?')}"
                step = f"{path} <- {name}"
                lines.append(step)
                following.append((holder, step))
        level = following[:40]
        if not level:
            break
    return "\n".join(lines[-25:]) or "(쥔 것을 찾지 못했다)"


def _assert_released(head: weakref.ref) -> None:
    _collect()
    target = head()
    if target is not None:
        chains = _holders(target)
        del target
        pytest.fail("moov 색인이 풀리지 않았다 — 쥐고 있는 것:\n" + chains)


def _download(qtbot, win, item, until=DownloadState.FINISHED) -> None:
    win.downloadButton.click()
    qtbot.waitUntil(lambda: item.downloadState == until, timeout=FINISH_TIMEOUT)
    qtbot.waitUntil(lambda: win.downloadViewModel.handle is None, timeout=FINISH_TIMEOUT)
    _wait_for_engine_threads()


# 진행률이 이만큼(초) 그대로면 섰다고 본다 — 제품이 요청을 확인하는 간격(50ms)의 여섯 배
_STANDING_SECONDS = 0.3


def _wait_until_progress_stands(qtbot, item) -> None:
    """카드의 진행률이 ``_STANDING_SECONDS`` 동안 그대로일 때까지 기다린다.

    일시정지 요청은 다음 확인 때 ffmpeg에 닿고, 그 전에 ffmpeg가 낸 진행은 그 뒤에 도착한다.
    """
    seen = [item.download_progress, time.monotonic()]

    def stands() -> bool:
        now = time.monotonic()
        if item.download_progress != seen[0]:
            seen[:] = [item.download_progress, now]
        return now - seen[1] >= _STANDING_SECONDS

    qtbot.waitUntil(stands, timeout=5_000)


def _wait_for_engine_threads() -> None:
    """엔진을 돌린 스레드가 끝나기를 기다린다 — 끝나기 전에는 스레드가 엔진을 쥐고 있다."""
    for thread in threading.enumerate():
        if thread is not threading.current_thread() and "Download" in thread.name:
            thread.join(10)


def test_index_is_freed_after_a_section_download_finishes(qtbot, window):
    """구간 다운로드가 끝나면 moov 색인을 아무도 쥐고 있지 않아야 한다.

    편집 창에서 구간(프레임 35~80)을 확인 → 전역 다운로드 → 완료
    -> 카드는 완료, 구간 파일이 만들어짐, 색인의 약한 참조 == None
    """
    win, item = window
    _give_section(qtbot, win, item)
    head = _held_head(item)

    _download(qtbot, win, item)

    assert item.section_paths and all(map(os.path.isfile, item.section_paths))
    _assert_released(head)


def test_download_started_while_the_total_is_counted_parses_once_and_frees_the_index(
    qtbot, window, monkeypatch
):
    """확인 직후(받을 크기를 세는 중) 받기 시작해도 해석은 한 번만 돌고, 끝나면 색인이 풀려야 한다.

    해석 함수를 문으로 막고 호출을 세는 대역으로 바꿈. 구간을 확인(크기 계산이 해석 안에 들어가
    선다) → 전역 다운로드(실제 엔진이 준비 단계에서 같은 moov를 청한다) → 엔진이 기다리도록
    잠깐 둔 뒤 문을 엶 → 완료
    -> 해석 1회, 구간 파일이 만들어짐, 준비 로그에 "moov reused", 그 색인의 약한 참조 == None
    """
    win, item = window
    real = mp4_module.index_mp4
    gate = threading.Event()
    built: list[weakref.ref] = []
    calls = []

    def gated(raw):
        calls.append(1)
        assert gate.wait(30), "문이 열리지 않았다"
        head = real(raw)
        built.append(weakref.ref(head))
        return head

    monkeypatch.setattr(mp4_module, "index_mp4", gated)
    notes = []
    real_note = fd_module.FileDownloader._prepare_note
    monkeypatch.setattr(
        fd_module.FileDownloader,
        "_prepare_note",
        lambda self: notes.append(real_note(self)) or notes[-1],
    )
    _give_section(qtbot, win, item, wait_size=False)
    qtbot.waitUntil(lambda: len(calls) == 1, timeout=10_000)

    win.downloadButton.click()
    qtbot.waitUntil(lambda: item.downloadState == DownloadState.RUNNING, timeout=10_000)
    qtbot.wait(300)  # 엔진이 준비 단계에서 같은 moov를 청하고 기다리는 구간을 넓힌다
    gate.set()
    qtbot.waitUntil(lambda: item.downloadState == DownloadState.FINISHED, timeout=FINISH_TIMEOUT)
    qtbot.waitUntil(lambda: win.downloadViewModel.handle is None, timeout=FINISH_TIMEOUT)
    _wait_for_engine_threads()
    sizer = win.contentManager._sectionSizer
    qtbot.waitUntil(lambda: sizer.pendingCount() == 0, timeout=10_000)

    assert len(calls) == 1
    assert item.section_paths and all(map(os.path.isfile, item.section_paths))
    assert notes == ["moov reused"]
    _assert_released(built[0])


def test_index_is_freed_after_the_same_card_is_downloaded_twice(qtbot, window):
    """같은 카드를 두 번 받아도 앞의 색인과 뒤의 색인이 모두 풀려야 한다.

    구간을 확인해 받고 → 완료 카드를 대기로 되돌려 구간을 다시 확인해 받음
    -> 두 번의 조회가 받은 색인의 약한 참조가 모두 None
    """
    win, item = window
    _give_section(qtbot, win, item)
    first = _held_head(item)
    _download(qtbot, win, item)

    item.downloadState = DownloadState.WAITING
    item.download_progress = 0
    win.contentManager.model.notifyChanged(item)
    _pump()
    _give_section(qtbot, win, item, "00000200", "00000310")
    second = _held_head(item)
    _download(qtbot, win, item)

    _assert_released(first)
    _assert_released(second)


def test_index_is_freed_after_the_download_fails(qtbot, window, monkeypatch):
    """다운로드가 준비 뒤에 실패해도(이어받기 기록 없이) moov 색인이 풀려야 한다.

    받는 요청이 예외를 내게 함 → 구간을 확인해 전역 다운로드 → 실패
    -> 카드는 실패, 이어받기 기록 없음, 색인의 약한 참조 == None
    """
    win, item = window
    _give_section(qtbot, win, item)
    head = _held_head(item)

    def broken(self, *args, **kwargs):
        raise OSError("전송 실패(대역)")

    monkeypatch.setattr(fd_module.FileDownloader, "_download_part", broken)

    _download(qtbot, win, item, until=DownloadState.FAILED)

    assert item.section_retry is None
    _assert_released(head)


def test_index_is_kept_only_by_the_retry_record_after_a_partial_failure(qtbot, window, monkeypatch):
    """컷이 실패해 이어받기 기록이 남으면 색인은 그 기록만 쥐고, 기록을 지우면 풀려야 한다.

    ffmpeg 실행이 모두 실패하게 함 → 구간을 확인해 전역 다운로드 → 실패(기록 남음)
    -> 기록이 있는 동안 색인이 살아 있다, 기록을 지운 뒤 약한 참조 == None
    """
    win, item = window
    _give_section(qtbot, win, item)
    head = _held_head(item)

    def failing(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "boom")

    monkeypatch.setattr(cut_module, "run_ffmpeg", failing)

    _download(qtbot, win, item, until=DownloadState.FAILED)
    _collect()

    assert item.section_retry is not None, "전제: 이어받기 기록이 남았다"
    assert head() is not None, "전제: 재시도가 쓸 색인은 기록이 쥐고 있다"

    item.section_retry = None

    _assert_released(head)


def test_index_is_freed_after_the_download_is_stopped(qtbot, window, monkeypatch):
    """받는 도중 정지해도 moov 색인이 풀려야 한다.

    받는 요청을 문으로 붙잡아 둠 → 구간을 확인해 전역 다운로드 → 받는 중에 정지 → 문을 연다
    -> 카드는 대기, 색인의 약한 참조 == None
    """
    win, item = window
    _give_section(qtbot, win, item)
    head = _held_head(item)
    entered, gate = threading.Event(), threading.Event()
    real = fd_module.FileDownloader._download_part

    def held(self, *args, **kwargs):
        entered.set()
        assert gate.wait(30), "문이 열리지 않았다"
        return real(self, *args, **kwargs)

    monkeypatch.setattr(fd_module.FileDownloader, "_download_part", held)
    # 정지 버튼의 확인 창에서 "예"를 고른 것으로 한다
    monkeypatch.setattr(mw_mod.QMessageBox, "warning", lambda *a, **k: mw_mod.QMessageBox.Yes)
    win.downloadButton.click()
    qtbot.waitUntil(entered.is_set, timeout=FINISH_TIMEOUT)

    win.stopButton.click()
    gate.set()
    qtbot.waitUntil(lambda: item.downloadState == DownloadState.WAITING, timeout=FINISH_TIMEOUT)
    qtbot.waitUntil(lambda: win.downloadViewModel.handle is None, timeout=FINISH_TIMEOUT)
    _wait_for_engine_threads()

    _assert_released(head)


def test_stopping_during_the_cut_does_not_make_the_app_give_up_waiting_for_the_worker(
    qtbot, window, monkeypatch, caplog, tmp_path
):
    """구간을 자르는 도중 정지해도 앱이 워커를 기다리다 포기하지 않아야 한다 — ffmpeg가 바로 끝난다.

    컷의 오디오 단계를 10분 도는 명령으로 바꿔 띄움. 그 ffmpeg가 뜬 뒤 정지 버튼을 누름
    (정지는 워커가 끝나기를 2초까지 기다린다 — 그동안 앱이 선다)
    -> 정지 버튼의 처리가 1.5초 안에 끝난다, "대기를 포기한다" 경고가 없다(같은 로거의 표식은 잡힌다)
    -> 카드는 대기, 핸들 없음, 띄운 ffmpeg가 모두 끝났다, 저장 폴더에 mp4가 남지 않았다
    """
    win, item = window
    _give_section(qtbot, win, item)
    processes = record_processes(monkeypatch)
    launched = threading.Event()
    real = cut_module.run_ffmpeg

    def slowed(args, **kwargs):
        if "audio.m4a" in args:
            kwargs.pop("on_out_time", None)
            threading.Timer(0.3, launched.set).start()  # 프로세스가 뜬 뒤에 알린다
            # 제품이 멈추지 못하면 10분을 돈다 — 띄운 프로세스를 직접 끝내 워커가 끝나게 한다
            guard = threading.Timer(8.0, end_all, [processes])
            guard.daemon = True
            guard.start()
            return real(LONG_RUNNING, **kwargs)
        return real(args, **kwargs)

    monkeypatch.setattr(cut_module, "run_ffmpeg", slowed)
    monkeypatch.setattr(mw_mod.QMessageBox, "warning", lambda *a, **k: mw_mod.QMessageBox.Yes)
    logger_name = "app.viewmodels.download_viewmodel"
    try:
        with caplog.at_level(logging.WARNING, logger=logger_name):
            win.downloadButton.click()
            qtbot.waitUntil(launched.is_set, timeout=FINISH_TIMEOUT)
            assert processes[-1].poll() is None, "전제: 정지할 때 컷의 ffmpeg가 돌고 있다"

            started = time.perf_counter()
            win.stopButton.click()
            took = time.perf_counter() - started
            logging.getLogger(logger_name).warning("표식 — 이 로거의 경고가 잡힌다")

        messages = [r.getMessage() for r in caplog.records if r.name == logger_name]
        assert "표식 — 이 로거의 경고가 잡힌다" in messages
        assert not [message for message in messages if "대기를 포기" in message]
        assert took < 1.5
        assert item.downloadState == DownloadState.WAITING
        assert win.downloadViewModel.handle is None
        _wait_for_engine_threads()
        assert all(process.poll() is not None for process in processes)
        assert [name for name in os.listdir(tmp_path) if name.endswith(".mp4")] == []
    finally:
        end_all(processes)


def test_pausing_during_the_cut_shows_paused_and_resuming_goes_back_to_cutting(
    qtbot, window, monkeypatch
):
    """구간을 자르는 도중 일시정지하면 카드가 "Paused"로 서 있고, 재개하면 "Cutting"으로 돌아가 끝나야 한다.

    컷의 오디오 단계의 ffmpeg를 띄우는 순간에 전역 버튼으로 일시정지(버튼이 눌릴 때까지 테스트가
    그 ffmpeg를 세워 둔다) → 진행률이 선 뒤 0.8초 지켜봄 → 다시 눌러 재개
    -> 일시정지 중: 카드 PAUSED, 상태 문구가 "% · Paused"로 끝난다, 진행률이 0.8초 동안 그대로다,
       그 ffmpeg가 살아 있다, 카드가 완료되지 않았다
    -> 재개 직후: 카드 RUNNING, 상태 문구에 "Cutting"
    -> 끝: 카드 FINISHED, 구간 파일이 만들어진다, 띄운 ffmpeg가 모두 끝났다
    """
    win, item = window
    _give_section(qtbot, win, item)
    reached, pressed = threading.Event(), threading.Event()
    audio = []

    def on_launch(command, process) -> None:
        if command[-1] == "audio.m4a":
            audio.append(process)
            # 버튼이 눌릴 때까지 막 뜬 ffmpeg를 세워 둔다 — 짧은 단계가 그사이 끝나지 않게.
            # 풀고 돌아가면 제품이 띄운 직후의 확인에서 일시정지를 보고 멈춘다
            ffmpeg_module._set_suspended(process, True)
            reached.set()
            pressed.wait(30)
            ffmpeg_module._set_suspended(process, False)

    processes = record_processes(monkeypatch, on_launch)
    card = win.listView.widgetFor(item)
    try:
        win.downloadButton.click()
        qtbot.waitUntil(reached.is_set, timeout=FINISH_TIMEOUT)
        win.downloadButton.click()  # 일시정지
        pressed.set()
        _pump()
        _wait_until_progress_stands(qtbot, item)
        first = (item.downloadState, card.statusLabel.text(), item.download_progress)
        qtbot.wait(800)
        second = (item.downloadState, card.statusLabel.text(), item.download_progress)
        alive = audio[0].poll() is None

        win.downloadButton.click()  # 재개
        _pump()
        resumed = (item.downloadState, card.statusLabel.text())
        qtbot.waitUntil(
            lambda: item.downloadState == DownloadState.FINISHED, timeout=FINISH_TIMEOUT
        )
        qtbot.waitUntil(lambda: win.downloadViewModel.handle is None, timeout=FINISH_TIMEOUT)
        _wait_for_engine_threads()

        assert first[0] == second[0] == DownloadState.PAUSED
        assert first[1].endswith("% · Paused") and second[1] == first[1]
        assert second[2] == first[2]
        assert alive, "전제: 일시정지 중에 컷의 ffmpeg가 살아 있다(끝나지 않았다)"
        assert resumed[0] == DownloadState.RUNNING and "Cutting" in resumed[1]
        assert item.section_paths and all(map(os.path.isfile, item.section_paths))
        assert all(process.poll() is not None for process in processes)
    finally:
        end_all(processes)


def test_stopping_a_cut_that_is_paused_ends_the_suspended_ffmpeg(
    qtbot, window, monkeypatch, caplog, tmp_path
):
    """컷 도중 일시정지한 채 정지하면(앱을 닫을 때도 이 길이다) 멈춰 있던 ffmpeg가 끝나고 아무것도 남지 않아야 한다.

    컷의 오디오 단계를 10분 도는 명령으로 바꿔 띄움. 그 ffmpeg가 뜬 뒤 일시정지 → 0.4초 뒤 정지 버튼
    -> 정지 버튼의 처리가 1.5초 안에 끝난다, "대기를 포기한다" 경고가 없다(같은 로거의 표식은 잡힌다)
    -> 카드는 대기, 핸들 없음, 띄운 ffmpeg가 모두 끝났다, 저장 폴더에 mp4가 남지 않았다
    """
    win, item = window
    _give_section(qtbot, win, item)
    processes = record_processes(monkeypatch)
    launched = threading.Event()
    real = cut_module.run_ffmpeg

    def slowed(args, **kwargs):
        if args[-1] == "audio.m4a":
            kwargs.pop("on_out_time", None)
            threading.Timer(0.3, launched.set).start()  # 프로세스가 뜬 뒤에 알린다
            guard = threading.Timer(8.0, end_all, [processes])  # 제품이 못 끝내면 테스트가 끝낸다
            guard.daemon = True
            guard.start()
            return real(LONG_RUNNING, **kwargs)
        return real(args, **kwargs)

    monkeypatch.setattr(cut_module, "run_ffmpeg", slowed)
    monkeypatch.setattr(mw_mod.QMessageBox, "warning", lambda *a, **k: mw_mod.QMessageBox.Yes)
    logger_name = "app.viewmodels.download_viewmodel"
    try:
        with caplog.at_level(logging.WARNING, logger=logger_name):
            win.downloadButton.click()
            qtbot.waitUntil(launched.is_set, timeout=FINISH_TIMEOUT)
            win.downloadButton.click()  # 일시정지 — 그 ffmpeg가 멈춘다
            qtbot.wait(400)
            assert item.downloadState == DownloadState.PAUSED, "전제: 일시정지됐다"
            assert processes[-1].poll() is None, "전제: 멈춘 ffmpeg가 살아 있다"

            started = time.perf_counter()
            win.stopButton.click()
            took = time.perf_counter() - started
            logging.getLogger(logger_name).warning("표식 — 이 로거의 경고가 잡힌다")

        messages = [r.getMessage() for r in caplog.records if r.name == logger_name]
        assert "표식 — 이 로거의 경고가 잡힌다" in messages
        assert not [message for message in messages if "대기를 포기" in message]
        assert took < 1.5
        assert item.downloadState == DownloadState.WAITING
        assert win.downloadViewModel.handle is None
        _wait_for_engine_threads()
        assert all(process.poll() is not None for process in processes)
        assert [name for name in os.listdir(tmp_path) if name.endswith(".mp4")] == []
    finally:
        end_all(processes)


def test_index_is_freed_when_the_card_is_deleted_while_downloading(qtbot, window, monkeypatch):
    """받는 도중 카드를 지워(취소) 다운로드가 끝나도 moov 색인이 풀려야 한다.

    받는 요청을 문으로 붙잡아 둠 → 구간을 확인해 전역 다운로드 → 받는 중에 정지하고 카드를 지움
    -> 카드가 목록에 없다, 색인의 약한 참조 == None
    """
    win, item = window
    _give_section(qtbot, win, item)
    head = _held_head(item)
    entered, gate = threading.Event(), threading.Event()
    real = fd_module.FileDownloader._download_part

    def held(self, *args, **kwargs):
        entered.set()
        assert gate.wait(30), "문이 열리지 않았다"
        return real(self, *args, **kwargs)

    monkeypatch.setattr(fd_module.FileDownloader, "_download_part", held)
    # 정지 버튼의 확인 창에서 "예"를 고른 것으로 한다
    monkeypatch.setattr(mw_mod.QMessageBox, "warning", lambda *a, **k: mw_mod.QMessageBox.Yes)
    win.downloadButton.click()
    qtbot.waitUntil(entered.is_set, timeout=FINISH_TIMEOUT)

    win.stopButton.click()
    gate.set()
    qtbot.waitUntil(lambda: item.downloadState == DownloadState.WAITING, timeout=FINISH_TIMEOUT)
    qtbot.waitUntil(lambda: win.downloadViewModel.handle is None, timeout=FINISH_TIMEOUT)
    win.contentManager.removeItem(item)
    _pump()
    _wait_for_engine_threads()

    assert win.contentManager.model.getRow(item) is None
    _assert_released(head)


def test_engine_is_freed_after_the_download_finishes(qtbot, window, monkeypatch):
    """다운로드가 끝나면 엔진 객체도 순환 수집 없이 사라져야 한다 — 핸들과 서로를 붙잡지 않는다.

    엔진의 run에 들어설 때 그 엔진에 약한 참조를 걸어 둠. 구간을 확인해 전역 다운로드 → 완료
    -> 카드는 완료, 엔진의 약한 참조 == None
    """
    win, item = window
    _give_section(qtbot, win, item)
    engines = []
    real = fd_module.FileDownloader.run

    def watched(self):
        engines.append(weakref.ref(self))
        return real(self)

    monkeypatch.setattr(fd_module.FileDownloader, "run", watched)

    _download(qtbot, win, item)
    _collect()

    assert len(engines) == 1
    assert engines[0]() is None


def test_released_line_is_logged_only_after_the_last_reference_is_gone(
    qtbot, window, monkeypatch, caplog
):
    """ "색인을 놓은 뒤" 줄은 앱이 참조를 놓았을 때가 아니라 색인이 실제로 사라진 뒤에만 남아야 한다.

    메모리 읽기를 고정값으로 둠(적기까지의 틈은 이 파일에서 0이다). 테스트가 색인을 따로 붙든 채 구간을 확인해
    받고 완료까지 감 → 붙든 것을 놓음
    -> 붙들고 있는 동안: "다운로드 끝" 줄은 있고 "색인을 놓은 뒤" 줄은 없다
    -> 놓은 뒤: "프로세스 메모리 [색인을 놓은 뒤] RSS 512.0MB" 한 줄이 남는다
    """
    win, item = window
    monkeypatch.setattr(process_memory, "read_process_memory", lambda: {"RSS": 512 * 1024 * 1024})
    caplog.set_level(logging.INFO)
    _collect()  # 앞 테스트의 색인이 사라지며 남긴 알림을 먼저 비운다
    caplog.clear()

    def lines() -> list[str]:
        return [r.getMessage() for r in caplog.records if r.name == "app.process_memory"]

    _give_section(qtbot, win, item)
    held = [item.section_head[1]]  # 엔진 밖의 누군가가 색인을 쥐고 있다

    _download(qtbot, win, item)
    _collect()

    assert "프로세스 메모리 [다운로드 끝] RSS 512.0MB" in lines()
    assert not any("색인을 놓은 뒤" in line for line in lines())

    held.clear()
    qtbot.waitUntil(lambda: any("색인을 놓은 뒤" in line for line in lines()), timeout=5000)

    assert lines().count("프로세스 메모리 [색인을 놓은 뒤] RSS 512.0MB") == 1
