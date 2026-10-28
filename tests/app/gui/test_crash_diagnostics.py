"""충돌 진단 (#427) — 충돌 순간의 스택이 로그 폴더에 남고, 처리되지 않은 예외가 앱 로그에 가려져 남는다.

네이티브 충돌은 별도 프로세스에서 일으킨다 — pytest 프로세스는 이미 자기 faulthandler를 켜 두었다.
"""

import io
import logging
import os
import subprocess
import sys
import textwrap
import threading

import pytest
from PySide6.QtCore import QTimer

from app.system.crash_diagnostics import (
    CRASH_FILE_NAME,
    carry_over_previous_crash,
    install_exception_hooks,
)
from core.utils.logging_setup import LOG_FORMAT, RedactingFormatter

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
HOME = os.path.expanduser("~")

# 별도 프로세스의 앞부분 — 저장소를 import 경로에 넣고, Windows의 오류 대화상자를 끈다
_PRELUDE = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {root!r})
    if sys.platform == "win32":
        import ctypes
        # 이 프로세스만의 오류 모드 — 충돌해도 오류 보고 창을 띄우지 않는다
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    """
)

_CRASH = textwrap.dedent(
    """
    import faulthandler
    import threading
    from app.system.crash_diagnostics import install_crash_diagnostics

    install_crash_diagnostics({log_dir!r})
    parked = threading.Event()
    threading.Thread(target=parked.wait, name="parked-worker", daemon=True).start()

    def crash_here_for_test():
        faulthandler._sigsegv()

    crash_here_for_test()
    """
)

_NEXT_START = textwrap.dedent(
    """
    from core.utils.logging_setup import setup_logging
    from app.system.crash_diagnostics import install_crash_diagnostics

    setup_logging({log_dir!r})
    install_crash_diagnostics({log_dir!r})
    """
)


_UNHANDLED = textwrap.dedent(
    """
    import os
    from core.utils.logging_setup import setup_logging
    from app.system.crash_diagnostics import install_crash_diagnostics

    setup_logging({log_dir!r})
    install_crash_diagnostics({log_dir!r})
    raise ValueError(os.path.join(os.path.expanduser("~"), "unhandled-marker.mp4"))
    """
)


def _fake_home(tmp_path, name: str = "tester") -> str:
    """별도 프로세스의 홈 폴더 — 충돌 기록에 홈 폴더 경로가 늘 들어가게 스크립트를 이 아래에 둔다.

    CI 러너는 저장소 · 파이썬이 홈 폴더 밖에 있어 진짜 홈으로는 기록에 홈 경로가 들어가지 않는다.
    """
    home = tmp_path / "home" / name
    home.mkdir(parents=True, exist_ok=True)
    return str(home)


def _run(
    tmp_path, body: str, env: dict | None = None, home_name: str = "tester"
) -> subprocess.CompletedProcess:
    """body를 별도 프로세스로 돌린다 — 홈 폴더(HOME · USERPROFILE)를 _fake_home으로 바꾸고, 스크립트를 그 안에 둔다."""
    log_dir = str(tmp_path / "logs")
    home = _fake_home(tmp_path, home_name)
    script = os.path.join(home, "crash_case.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(_PRELUDE.format(root=ROOT) + body.format(log_dir=log_dir))
    return subprocess.run(
        [sys.executable, script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "HOME": home,
            "USERPROFILE": home,
            **(env or {}),
        },
    )


_DEBUG_CRASH = textwrap.dedent(
    """
    import os
    from app.system.crash_diagnostics import crash_if_requested, install_crash_diagnostics

    install_crash_diagnostics({log_dir!r})
    crash_if_requested()
    print("still running")
    """
)


def _read(path) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


@pytest.fixture
def crashed(tmp_path):
    """별도 프로세스에서 충돌 진단을 켠 뒤 일부러 충돌시킨다 — 그 로그 폴더."""
    done = _run(tmp_path, _CRASH)
    assert done.returncode != 0, "전제: 프로세스가 충돌로 끝났다"
    return tmp_path / "logs"


def test_native_crash_leaves_every_threads_stack_in_the_crash_file(crashed):
    """충돌 진단을 켠 프로세스가 네이티브 충돌로 끝나면 crash.log에 모든 스레드의 스택이 남아야 한다.

    별도 프로세스: install_crash_diagnostics → 기다리는 스레드 하나 → crash_here_for_test()에서
    faulthandler._sigsegv()
    -> crash.log에 충돌 머리줄("Fatal Python error" 또는 "Windows fatal exception"),
       "crash_here_for_test", 스레드 머리줄 2개 이상
    """
    dump = _read(crashed / CRASH_FILE_NAME)

    assert "Fatal Python error" in dump or "Windows fatal exception" in dump
    assert "crash_here_for_test" in dump
    assert dump.count("hread 0x") >= 2  # "Current thread 0x…" · "Thread 0x…"


def test_next_start_moves_the_crash_record_to_the_app_log_with_the_home_folder_hidden(
    crashed, tmp_path
):
    """충돌 뒤 다음 시작은 충돌 기록을 가려서 앱 로그로 옮기고 crash.log를 비워야 한다.

    위 충돌 뒤 별도 프로세스: setup_logging(같은 로그 폴더) → install_crash_diagnostics → 정상 종료
    -> app.log에 "Previous run ended abnormally"와 "crash_here_for_test", 홈 폴더 경로 없음,
       crash.log는 빈 파일
    """
    home = _fake_home(tmp_path)
    assert home in _read(crashed / CRASH_FILE_NAME)  # 전제: 원본 기록에 홈 폴더 경로가 있다

    done = _run(tmp_path, _NEXT_START)

    assert done.returncode == 0, done.stderr
    app_log = _read(crashed / "app.log")
    assert "Previous run ended abnormally" in app_log
    assert "crash_here_for_test" in app_log
    assert home not in app_log
    assert _read(crashed / CRASH_FILE_NAME) == ""


def test_next_start_hides_an_escaped_non_ascii_home_in_the_crash_record(tmp_path):
    r"""홈 폴더 이름이 비ASCII면 충돌 기록에 이스케이프로 적힌 홈 경로도 앱 로그에서 가려져야 한다.

    홈 폴더 이름 "테스터"인 별도 프로세스가 충돌(crash.log에 \ud14c\uc2a4\ud130) → 같은 홈으로 다음 시작
    -> app.log에 "crash_here_for_test", 이름 원문 · 이스케이프 모양 둘 다 없음
    """
    escaped = r"\ud14c\uc2a4\ud130"  # "테스터"를 faulthandler가 적는 모양
    assert _run(tmp_path, _CRASH, home_name="테스터").returncode != 0, "전제: 충돌로 끝났다"
    assert escaped in _read(tmp_path / "logs" / CRASH_FILE_NAME)  # 전제: 이스케이프로 적혔다

    done = _run(tmp_path, _NEXT_START, home_name="테스터")

    assert done.returncode == 0, done.stderr
    app_log = _read(tmp_path / "logs" / "app.log")
    assert "crash_here_for_test" in app_log
    assert escaped not in app_log
    assert "테스터" not in app_log


def test_app_start_logs_an_unhandled_exception_to_the_app_log_with_the_home_folder_hidden(
    tmp_path,
):
    """앱 시작 경로(setup_logging → install_crash_diagnostics) 뒤 처리되지 않은 예외는 app.log에 가려져 남아야 한다.

    별도 프로세스: 위 두 함수를 부른 뒤 최상위에서 ValueError("<홈 폴더>/unhandled-marker.mp4")
    -> 0 아닌 종료, app.log에 "Unhandled exception"과 "unhandled-marker.mp4", 홈 폴더 경로는 없다
    """
    done = _run(tmp_path, _UNHANDLED)

    assert done.returncode != 0
    app_log = _read(tmp_path / "logs" / "app.log")
    assert "Unhandled exception" in app_log
    assert "unhandled-marker.mp4" in app_log
    assert _fake_home(tmp_path) not in app_log


def test_empty_crash_record_leaves_nothing_in_the_app_log(tmp_path, caplog):
    """앞 실행이 정상으로 끝나 충돌 기록이 비어 있으면 앱 로그에 아무것도 남기지 않아야 한다.

    빈 crash.log -> carry_over_previous_crash가 True(비워도 된다), 이 모듈의 로그 레코드 0개
    """
    path = tmp_path / CRASH_FILE_NAME
    path.write_text("", encoding="ascii")
    marker = logging.getLogger("app.system.crash_diagnostics")
    with caplog.at_level(logging.DEBUG, logger="app.system.crash_diagnostics"):
        marker.debug("캡처 확인")  # 음성 단언 전에 캡처가 살아 있음을 본다
        may_clear = carry_over_previous_crash(str(path))

    assert caplog.records[0].getMessage() == "캡처 확인"
    assert may_clear is True
    assert [r for r in caplog.records if r.name == "app.system.crash_diagnostics"][1:] == []


def test_crash_record_longer_than_the_limit_drops_the_line_cut_at_the_limit(tmp_path, caplog):
    """옮길 상한(64KiB)보다 긴 충돌 기록은 상한에서 잘린 끝 줄을 옮기지 않고 잘렸다고 적어야 한다.

    crash.log = "a" 줄들로 상한 10자 앞까지 채우고, 그 뒤에 홈 폴더 경로가 든 줄이 상한을 넘어 이어짐
    -> 옮긴 레코드에 그 줄의 어느 부분도 없다("File " 없음), 끝에 "(cut at 65536 characters)"
    """
    limit = 64 * 1024  # app.system.crash_diagnostics._CARRIED_CHARS — 테스트가 따로 적는다
    filler = ("a" * 99 + "\n") * ((limit - 10) // 100)
    filler += "b" * (limit - 10 - len(filler) - 1) + "\n"
    cut_line = '  File "' + os.path.join(HOME, "project", "module.py") + '", line 1 in f\n'
    path = tmp_path / CRASH_FILE_NAME
    path.write_text(filler + cut_line, encoding="ascii", errors="replace")
    assert len(filler) < limit < len(filler) + len(cut_line)  # 전제: 상한이 그 줄 가운데 있다

    with caplog.at_level(logging.ERROR, logger="app.system.crash_diagnostics"):
        carry_over_previous_crash(str(path))

    (record,) = [r for r in caplog.records if r.name == "app.system.crash_diagnostics"]
    message = record.getMessage()
    assert "File " not in message
    assert message.endswith(f"(cut at {limit} characters)")


def test_unreadable_crash_record_is_kept_instead_of_cleared(tmp_path, monkeypatch):
    """앞 충돌 기록을 읽지 못하면 그 기록을 비우지 않고 이번 실행의 기록을 그 뒤에 이어야 한다.

    crash.log = "old record", 그 파일을 읽으려 하면 PermissionError(쓰기는 된다). faulthandler 연결은
    대역으로 바꾼다(pytest의 것을 덮지 않게)
    -> crash.log가 "old record"로 시작한다, 연결한 파일의 모드가 "a"
    """
    import app.system.crash_diagnostics as module

    path = tmp_path / CRASH_FILE_NAME
    path.write_text("old record", encoding="ascii")
    real_open = open

    def refusing_read(file, mode="r", *args, **kwargs):
        if mode == "r" and os.fspath(file) == str(path):
            raise PermissionError("시험 — 읽을 수 없다")
        return real_open(file, mode, *args, **kwargs)

    connected = []
    monkeypatch.setattr(module, "open", refusing_read, raising=False)
    monkeypatch.setattr(module.faulthandler, "enable", lambda file, **_: connected.append(file))
    monkeypatch.setattr(module, "_crash_file", None)
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)

    module.install_crash_diagnostics(str(tmp_path))
    connected[0].close()

    assert connected[0].mode == "a"
    assert _read(path).startswith("old record")


@pytest.fixture
def hooked(monkeypatch):
    """예외 훅을 건다 — 테스트가 끝나면 원래 훅으로 되돌린다. 이 모듈의 로그를 가리는 포매터로 받는다."""
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter(LOG_FORMAT))
    target = logging.getLogger("app.system.crash_diagnostics")
    target.addHandler(handler)
    install_exception_hooks()
    yield stream
    target.removeHandler(handler)


@pytest.mark.qt_no_exception_capture  # 앞의 훅이 pytest-qt의 것이면 그 훅이 테스트를 실패시킨다
def test_unhandled_exception_on_the_main_thread_is_logged_with_the_home_folder_hidden(hooked):
    """메인 스레드의 처리되지 않은 예외는 앱 로그에 남고 그 글의 홈 폴더 경로는 가려져야 한다.

    sys.excepthook에 ValueError("<홈 폴더>/x.mp4를 열지 못했다")
    -> "Unhandled exception"과 "ValueError", "~" 가 남고 홈 폴더 경로는 없다
    """
    error = ValueError(f"{os.path.join(HOME, 'x.mp4')}를 열지 못했다")

    sys.excepthook(ValueError, error, None)

    written = hooked.getvalue()
    assert "Unhandled exception" in written and "ValueError" in written
    assert "~" in written
    assert HOME not in written


@pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnhandledThreadExceptionWarning"
)  # 앞의 훅이 pytest의 것
def test_unhandled_exception_in_another_thread_is_logged_with_the_thread_name(hooked):
    """다른 스레드의 처리되지 않은 예외는 스레드 이름과 함께 앱 로그에 남고 홈 폴더 경로는 가려져야 한다.

    "fetch-worker" 스레드가 RuntimeError("<홈 폴더>/y를 쓰지 못했다")
    -> "Unhandled exception in thread fetch-worker"와 "RuntimeError", 홈 폴더 경로는 없다
    """

    def fail() -> None:
        raise RuntimeError(f"{os.path.join(HOME, 'y')}를 쓰지 못했다")

    worker = threading.Thread(target=fail, name="fetch-worker")
    worker.start()
    worker.join(timeout=10)

    written = hooked.getvalue()
    assert "Unhandled exception in thread fetch-worker" in written
    assert "RuntimeError" in written
    assert HOME not in written


@pytest.mark.qt_no_exception_capture
def test_exception_in_a_qt_slot_reaches_the_main_hook(hooked, qtbot):
    """Qt 이벤트 처리 중(타이머 슬롯) 난 예외도 처리되지 않은 예외로 앱 로그에 남아야 한다.

    QTimer.singleShot(0, 예외를 내는 함수) — pytest-qt의 예외 가로채기는 끈다
    -> "Unhandled exception"과 "LookupError"가 남는다
    """

    def slot() -> None:
        raise LookupError("슬롯에서 난 예외")

    QTimer.singleShot(0, slot)
    qtbot.waitUntil(lambda: "LookupError" in hooked.getvalue(), timeout=5000)

    assert "Unhandled exception" in hooked.getvalue()


@pytest.mark.parametrize("value", [None, "0", "true"], ids=["unset", "zero", "true"])
def test_debug_crash_does_nothing_unless_the_variable_is_one(tmp_path, monkeypatch, value):
    """CVDV2_DEBUG_CRASH가 없거나 "1"이 아니면 시험용 충돌 경로는 아무것도 하지 않아야 한다.

    별도 프로세스: install_crash_diagnostics → crash_if_requested → "still running" 출력. 변수는 주석의 값
    -> 종료 코드 0, "still running" 출력, crash.log는 빈 파일
    """
    monkeypatch.delenv("CVDV2_DEBUG_CRASH", raising=False)
    env = {} if value is None else {"CVDV2_DEBUG_CRASH": value}

    done = _run(tmp_path, _DEBUG_CRASH, env)

    assert done.returncode == 0, done.stderr
    assert "still running" in done.stdout
    assert _read(tmp_path / "logs" / CRASH_FILE_NAME) == ""


def test_debug_crash_with_the_variable_set_leaves_a_crash_record(tmp_path):
    """CVDV2_DEBUG_CRASH=1이면 시험용 충돌 경로가 충돌하고 crash.log에 그 스택이 남아야 한다.

    별도 프로세스: install_crash_diagnostics → crash_if_requested, CVDV2_DEBUG_CRASH=1
    -> 0 아닌 종료, "still running" 출력 없음, crash.log에 "crash_if_requested"
    """
    done = _run(tmp_path, _DEBUG_CRASH, {"CVDV2_DEBUG_CRASH": "1"})

    assert done.returncode != 0
    assert "still running" not in done.stdout
    assert "crash_if_requested" in _read(tmp_path / "logs" / CRASH_FILE_NAME)
