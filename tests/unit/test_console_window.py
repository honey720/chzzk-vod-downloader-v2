"""테스트가 Windows에서 띄우는 자식 프로세스에 창 숨김 플래그를 기본으로 주는지 (#399).

``tests/console_window.py``의 규칙과, ``conftest.py``가 실제로 건 ``subprocess.Popen``을 잰다.
"""

import subprocess
import sys
import types

import pytest

from tests.console_window import CREATE_NO_WINDOW, install, with_hidden_window

# 자식 파이썬이 자기 콘솔에 붙은 프로세스 수와 콘솔 창 핸들을 찍는다 — 창 숨김 플래그로 띄우면
# 창 없는 새 콘솔을 혼자 쓴다(1, None). 플래그 없이 띄우면 부모의 콘솔을 함께 쓰거나(2 이상)
# 부모에게 콘솔이 없으면 창이 있는 새 콘솔을 받는다(핸들 있음)
_REPORT_CONSOLE = (
    "import ctypes\n"
    "k = ctypes.windll.kernel32\n"
    "k.GetConsoleWindow.restype = ctypes.c_void_p\n"
    "ids = (ctypes.c_uint32 * 64)()\n"
    "print(k.GetConsoleProcessList(ids, 64), k.GetConsoleWindow())\n"
)
# 가상 환경의 python.exe는 진짜 인터프리터를 자식으로 띄우는 실행기라 콘솔을 둘이 쓴다
_INTERPRETER = getattr(sys, "_base_executable", sys.executable)


def test_call_without_creationflags_gets_the_hidden_window_flag():
    """creationflags 없이 부르면 CREATE_NO_WINDOW가 더해져야 한다 (#399).

    args=(["x"],), kwargs={"stdout": PIPE} -> kwargs에 creationflags=0x08000000, 나머지는 그대로
    """
    kwargs = with_hidden_window((["x"],), {"stdout": subprocess.PIPE})

    assert kwargs == {"stdout": subprocess.PIPE, "creationflags": CREATE_NO_WINDOW}


@pytest.mark.parametrize("flags", [0, 0x00000200])  # 0 · CREATE_NEW_PROCESS_GROUP
def test_call_with_creationflags_keeps_them(flags):
    """creationflags를 키워드로 직접 주면 0이어도 그 값 그대로여야 한다 (#399).

    kwargs={"creationflags": flags} -> 같은 kwargs
    """
    assert with_hidden_window((["x"],), {"creationflags": flags}) == {"creationflags": flags}


def test_call_with_creationflags_given_by_position_keeps_them():
    """creationflags를 위치 인자(14번째)로 주면 키워드를 더하지 않아야 한다 (#399).

    args 14개(마지막이 creationflags=0) -> kwargs 그대로(빈 dict)
    """
    args = (["x"], -1, None, None, None, None, None, True, False, None, None, None, None, 0)

    assert with_hidden_window(args, {}) == {}


class _RecordingPopen:
    """받은 인자를 적는 Popen 대역."""

    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_install_changes_nothing_outside_windows(platform):
    """Windows가 아니면 install이 Popen을 바꾸지 않아야 한다 (#399).

    Popen이 대역인 모듈, platform -> False, Popen은 같은 객체
    """
    module = types.SimpleNamespace(Popen=_RecordingPopen)

    assert install(module, platform) is False
    assert module.Popen is _RecordingPopen


def test_install_on_windows_wraps_popen_once_and_passes_the_flags_through():
    """Windows면 Popen을 한 번만 감싸고, 감싼 Popen이 기본 플래그 · 직접 준 플래그를 원래 Popen에 넘겨야 한다 (#399).

    Popen이 대역인 모듈에 install을 두 번 -> True · False. 감싼 Popen(["a"]) · Popen(["b"], creationflags=0)
    -> 원래 Popen이 받은 creationflags == 0x08000000 · 0
    """
    module = types.SimpleNamespace(Popen=_RecordingPopen)

    assert install(module, "win32") is True
    assert install(module, "win32") is False
    plain = module.Popen(["a"])
    explicit = module.Popen(["b"], creationflags=0)

    assert isinstance(plain, _RecordingPopen)
    assert plain.kwargs == {"creationflags": CREATE_NO_WINDOW}
    assert explicit.kwargs == {"creationflags": 0}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows의 콘솔 창")
def test_child_started_by_a_test_without_flags_gets_a_console_without_a_window():
    """테스트가 플래그 없이 띄운 자식 프로세스는 창 없는 자기 콘솔을 받아야 한다 — conftest가 건 Popen (#399).

    subprocess.run([인터프리터, -c, 콘솔 보고])를 creationflags 없이 부름
    -> 자식의 콘솔에 붙은 프로세스 1개(부모 콘솔을 함께 쓰지 않음), 콘솔 창 핸들 없음
    """
    done = subprocess.run(
        [_INTERPRETER, "-c", _REPORT_CONSOLE], capture_output=True, text=True, check=True
    )

    attached, window = done.stdout.split()
    assert (int(attached), window) == (1, "None")
