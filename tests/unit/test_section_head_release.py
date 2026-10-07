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


def _give_section(qtbot, win, item, start: str = "00000105", end: str = "00000220") -> None:
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
