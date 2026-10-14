"""플랫폼 어댑터 — QStandardPaths·QDesktopServices 대체를 한 모듈로 (#211, Phase A3).

PySide6는 렌더링 외에도 이 둘을 제공했다(#208 ⑦ 조사):
- `QStandardPaths.writableLocation(DownloadLocation)` → `get_default_download_dir()`
- `QDesktopServices.openUrl` → `open_folder()` (폴더를 그냥 연다, `config/dialog.py`의
  `openLogsFolder`가 오늘 이렇게 쓰고 있다)

**`QDesktopServices`가 이미 절반만 문제를 풀고 있었다**(#211 조사): "폴더 열기"는
`QDesktopServices`로 충분했지만 "파일을 파일탐색기에서 선택된 채로 보여주기"는
Qt에도 대응 API가 없어서, `content/widget.py`의 `requestOpenDir`가 이미 3-way
수동 OS 분기(Windows `explorer.exe /select,` / macOS `open -R` / Linux
`nautilus`)를 쓰고 있었다. `reveal_in_file_manager()`는 그 분기를 그대로
옮긴 것이다 — Linux `nautilus` 고정(GNOME 전제, KDE·XFCE에서 실패할 수
있음 — `#193` 감사가 이미 지적)을 유지할지 낮출지는 **이 모듈에서 결정하지
않는다**. 기존 동작 보존이 기본값이다.

**상대 경로 절대화(`#146` ⓑ-4)는 이 모듈에 없다** — `app/viewmodels/path_gates.py`가
이미 "저장 경로 관문 판정의 단일 지점"으로 존재하고 이건 OS 상호작용이 아니라
경로 판정/정규화 문제라, Phase B에서 그 모듈에 자연스럽게 편입하는 쪽으로
판단했다(#211에서 결정, 여기서 구현하지 않음).
"""

import logging
import os
import platform
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import platformdirs

logger = logging.getLogger(__name__)


def get_default_download_dir() -> str:
    """기본 다운로드 폴더 — `QStandardPaths.writableLocation(DownloadLocation)` 대체."""
    return platformdirs.user_downloads_dir()


def _start_detached(args: list[str]) -> bool:
    """`QProcess.startDetached`와 같은 의미로 프로세스를 띄운다 — 시작 성공만 본다.

    원본(Qt)도 프로세스가 끝나기를 기다리지 않는다(detached) — 여기서
    `subprocess.run`으로 기다리면 파일탐색기 프로세스 기동이 느린 환경에서
    호출자(장차 JS→Python 호출 스레드)를 붙잡아 둘 수 있다. `#136`/`#137`/
    PR #135 이후 이 프로젝트가 계속 지켜온 "외부 I/O를 기다리다 UI를 얼리지
    않는다" 원칙을 그대로 따른다. 성공 판정도 원본과 동일하게 "프로세스
    시작 자체가 됐는가"이지 "그 프로세스가 0으로 끝났는가"가 아니다.
    """
    try:
        subprocess.Popen(args)  # noqa: S603 -- 인자는 전부 이 모듈이 하드코딩한 실행파일명 + 경로
        return True
    except OSError:
        return False


def open_folder(path: str) -> bool:
    """폴더를 연다 — `QDesktopServices.openUrl(QUrl.fromLocalFile(path))` 대체.

    `config/dialog.py`의 `openLogsFolder`가 오늘 이 패턴으로 로그 폴더를 연다.
    `os.startfile`은 Windows에만 있는 속성이라(다른 OS에서는 AttributeError,
    `#181`이 실제로 이걸로 죽었었다) 반드시 분기해야 한다.

    Returns
    -------
    성공 여부. 실패해도 예외를 던지지 않는다(호출자가 안내 문구를 결정한다 —
    `#181`/`config/dialog.py`와 동일한 "실패는 False로 알린다" 계약).
    """
    system = platform.system()
    if system == "Windows":
        try:
            os.startfile(path)  # noqa: S606 -- Windows 전용 API, 분기로 보호됨
            return True
        except OSError:
            return False
    if system == "Darwin":
        return _start_detached(["open", path])
    if system == "Linux":
        return _start_detached(["xdg-open", path])
    return False


def reveal_in_file_manager(path: str) -> bool:
    """파일탐색기에서 경로를 보여준다 — `content/widget.py`의 `requestOpenDir` 그대로 이식.

    경로가 파일이면 파일탐색기에서 그 파일이 선택된 채로 열린다(3-OS 수동 분기,
    Qt에도 대응 API가 없어 원래부터 이 방식이었다). 폴더면 `open_folder()`와
    동일하게 그냥 연다.

    Returns
    -------
    성공 여부. 원본과 동일하게 실패해도 예외를 던지지 않는다.
    """
    if not os.path.isfile(path):
        return open_folder(path)

    system = platform.system()
    native_path = os.path.normpath(path)  # QDir.toNativeSeparators(path) 대응
    if system == "Windows":
        return _start_detached(["explorer.exe", "/select,", native_path])
    if system == "Darwin":
        return _start_detached(["open", "-R", native_path])
    if system == "Linux":
        return _start_detached(["nautilus", native_path])
    return False


# ================================================================ 화면이 굼떠지지 않게 — GIL 넘김 · 타이머 (#309)
#
# 다른 스레드가 CPU를 쓰는 동안(색인 해석 · 다운로드) 메인 스레드는 파이썬으로 들어올 때마다
# GIL을 다시 받아야 한다. 받으려는 쪽은 넘김 간격만큼 기다렸다가 넘겨 달라고 하는데, Windows에서는
# 그 기다림이 타이머 한 틱(약 15.6ms)으로 올림된다 — 화면 일 한 번이 파이썬을 수십 번 드나들어
# 8ms짜리 일이 0.8초가 됐다(실측). 타이머 단위를 1ms로 청하면 기다림이 넘김 간격만큼만 든다.
#
# - 넘김 간격은 Windows에서만 1ms로 줄인다. 0.5ms는 쓰지 않는다 — 기다리지 않고 되풀이해 묻게 되어
#   다운로드 중 CPU가 2~4배가 된다(실측)
# - 타이머 단위는 백그라운드 일이 도는 동안에만 청한다. 청하는 동안 깨어나는 횟수가 늘어 전력을 더 쓴다
# - Windows는 창이 앞에 보일 때만 그 청을 들어준다(가려지거나 최소화되면 무시한다 — 실측)
# - GUI가 시작할 때 켠 뒤에만 움직인다(``enable_responsive_timing``). 헤드리스는 켜지 않는다
# - 다른 OS에서는 아무것도 하지 않는다

_GIL_SWITCH_SECONDS = 0.001  # Windows에서 쓰는 GIL 넘김 간격(초) — 파이썬 기본은 0.005
_TIMER_PERIOD_MS = 1  # 백그라운드 일이 도는 동안 청하는 타이머 단위(ms)

_timing_enabled = False  # GUI가 켰는지 — 꺼져 있으면 아래 함수들은 아무것도 하지 않는다
_work_lock = threading.Lock()  # 아래 두 값을 지킨다 — 일은 여러 스레드에서 시작하고 끝난다
_work_count = 0  # 지금 도는 백그라운드 일의 수
_timer_requested = False  # 타이머 단위를 청해 둔 상태인지 — 청이 받아들여졌을 때만 참이다
_winmm = None  # winmm.dll — 한 번만 연다


def enable_responsive_timing() -> None:
    """GUI 앱이 시작할 때 한 번 부른다 — Windows에서 GIL 넘김 간격을 줄이고 타이머 청을 켠다.

    다른 OS에서는 아무것도 하지 않는다. 헤드리스 실행은 부르지 않는다 — 화면이 없다.
    """
    global _timing_enabled
    if platform.system() != "Windows":
        return
    sys.setswitchinterval(_GIL_SWITCH_SECONDS)
    _timing_enabled = True
    logger.info("화면 반응 설정: GIL 넘김 간격 %gms", sys.getswitchinterval() * 1000)


def begin_background_work() -> None:
    """CPU를 쓰는 백그라운드 일 하나가 시작한다 — 첫 일이면 타이머 단위를 청한다.

    ``end_background_work``와 짝을 맞춰 부른다(``background_work``가 맞춰 준다). 어느 스레드에서
    불러도 된다. 켜지 않았거나(``enable_responsive_timing``) Windows가 아니면 수만 센다.
    """
    global _work_count, _timer_requested
    with _work_lock:
        _work_count += 1
        if _work_count == 1 and _timing_enabled:
            # 모듈 전역을 호출 시점에 조회한다 — 테스트의 monkeypatch 지점
            _timer_requested = _request_timer_resolution(_TIMER_PERIOD_MS)
            logger.debug(
                "타이머 단위 %dms 청함: %s",
                _TIMER_PERIOD_MS,
                "받아들여짐" if _timer_requested else "받아들여지지 않음",
            )


def end_background_work() -> None:
    """백그라운드 일 하나가 끝났다 — 마지막 일이면 청해 둔 타이머 단위를 푼다."""
    global _work_count, _timer_requested
    with _work_lock:
        if _work_count == 0:
            return  # 짝이 맞지 않는 호출 — 0 아래로 내려가지 않는다
        _work_count -= 1
        if _work_count == 0 and _timer_requested:
            _timer_requested = False
            _release_timer_resolution(_TIMER_PERIOD_MS)
            logger.debug("타이머 단위 %dms 청을 풂", _TIMER_PERIOD_MS)


@contextmanager
def background_work() -> Iterator[None]:
    """블록이 도는 동안을 백그라운드 일 하나로 센다 — 예외로 나가도 끝을 알린다."""
    begin_background_work()
    try:
        yield
    finally:
        end_background_work()


def _timer_library():
    """winmm.dll을 한 번만 연다. Windows가 아니거나 열 수 없으면 None."""
    global _winmm
    if _winmm is None:
        import ctypes

        loader = getattr(ctypes, "WinDLL", None)  # Windows에만 있다
        if loader is None:
            return None
        try:
            _winmm = loader("winmm")
        except OSError:
            return None
    return _winmm


def _request_timer_resolution(period_ms: int) -> bool:
    """타이머 단위를 period_ms로 청한다(``timeBeginPeriod``). 받아들여졌으면 참.

    이 프로세스의 청이다 — 시스템 설정을 바꾸지 않고, 프로세스가 끝나면 함께 풀린다.
    """
    library = _timer_library()
    if library is None:
        return False
    try:
        return library.timeBeginPeriod(period_ms) == 0  # TIMERR_NOERROR
    except OSError:
        return False


def _release_timer_resolution(period_ms: int) -> None:
    """청해 둔 타이머 단위를 푼다(``timeEndPeriod``) — 청할 때와 같은 값으로 부른다."""
    library = _timer_library()
    if library is None:
        return
    try:
        library.timeEndPeriod(period_ms)
    except OSError:
        return
