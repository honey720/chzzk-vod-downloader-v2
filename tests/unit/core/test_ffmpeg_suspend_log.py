"""멈추기 · 다시 시작의 결과가 로그에 남는지 (#458).

일시정지 뒤 끝나지 않는 실행에서 멈추기 · 다시 시작이 들었는지 가르려면 그 결과가 남아야 한다.
``_set_suspended``는 결과를 debug로, ``_Control.poll``은 살아 있는 프로세스를 다시 돌리지 못하면
warning으로 남긴다. 로그 이름은 ``record.name``으로 잰다(``caplog.at_level``의 logger는 거르지 않는다).
"""

import logging
import subprocess
import sys

import pytest

import core.utils.ffmpeg as ffmpeg_module
from core.utils.ffmpeg import _Control, _set_suspended

LOGGER = "core.utils.ffmpeg"


@pytest.fixture
def sleeper():
    """30초 자는 파이썬 자식 프로세스 — 끝날 때 살아 있으면 다시 돌린 뒤 끝낸다."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    yield process
    if process.poll() is None:
        _set_suspended(process, False)
        process.kill()
        process.wait(10)


def _messages(caplog, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == level]


def test_suspend_and_resume_of_a_live_process_are_logged_at_debug(caplog, sleeper):
    """살아 있는 프로세스를 멈추고 다시 돌리면 결과가 debug 두 줄로 남아야 한다.

    30초 자는 자식 프로세스 -> _set_suspended(True) · _set_suspended(False) 모두 True,
    core.utils.ffmpeg의 debug: "멈추기 pid=<pid>" · "다시 시작 pid=<pid>", 결과 표기는
    Windows "NTSTATUS=0x00000000" · 그 밖 "신호를 보냈다"
    """
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert _set_suspended(sleeper, True) is True
        assert _set_suspended(sleeper, False) is True

    lines = _messages(caplog, logging.DEBUG)
    result = "NTSTATUS=0x00000000" if sys.platform == "win32" else "신호를 보냈다"
    assert len(lines) == 2
    assert f"멈추기 pid={sleeper.pid}" in lines[0] and result in lines[0]
    assert f"다시 시작 pid={sleeper.pid}" in lines[1] and result in lines[1]


def test_suspend_of_an_ended_process_is_logged_and_does_nothing(caplog):
    """이미 끝난 프로세스를 멈추라고 하면 아무것도 하지 않고 그 사실이 debug로 남아야 한다.

    바로 끝나는 자식 프로세스(끝난 것을 확인) -> _set_suspended(True)는 False,
    core.utils.ffmpeg의 debug 한 줄에 "멈추기 pid=<pid>" · "이미 끝난"
    """
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(30)

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert _set_suspended(process, True) is False

    lines = _messages(caplog, logging.DEBUG)
    assert len(lines) == 1
    assert f"멈추기 pid={process.pid}" in lines[0] and "이미 끝난" in lines[0]


class _LiveProcess:
    """끝나지 않은 프로세스처럼 보이는 대역 — poll()이 returncode를 돌려준다(None이면 살아 있음)."""

    pid = 4242  # 로그에 찍힐 값을 알아보기 위한 임의의 번호
    returncode = None

    def poll(self):
        return self.returncode


def _toggle_and_fail_resume(monkeypatch, process) -> None:
    """멈추기는 되고 다시 시작은 실패하는 _set_suspended로 일시정지 → 해제를 한 번 거친다."""
    monkeypatch.setattr(ffmpeg_module, "_set_suspended", lambda proc, suspended: suspended)
    paused = [True]
    control = _Control(process, timeout=60, should_stop=None, should_pause=lambda: paused[0])
    assert control.poll() is None  # 일시정지 — 멈춤
    paused[0] = False
    assert control.poll() is None  # 해제 — 다시 시작 실패


def test_a_failed_resume_of_a_live_process_is_logged_as_a_warning(caplog, monkeypatch):
    """살아 있는 프로세스를 다시 돌리지 못하면 _Control.poll이 warning을 남겨야 한다.

    살아 있는 대역(pid=4242), 멈추기 True · 다시 시작 False, 일시정지 → 해제
    -> core.utils.ffmpeg의 warning 한 줄에 "다시 돌리지 못했다" · "pid=4242"
    """
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _toggle_and_fail_resume(monkeypatch, _LiveProcess())

    lines = _messages(caplog, logging.WARNING)
    assert len(lines) == 1
    assert "다시 돌리지 못했다" in lines[0] and "pid=4242" in lines[0]


def test_a_failed_resume_of_an_ended_process_is_not_warned(caplog, monkeypatch):
    """다시 돌리지 못한 까닭이 프로세스가 끝나서라면 warning을 남기지 않아야 한다.

    끝난 대역(returncode=0), 멈추기 True · 다시 시작 False, 일시정지 → 해제
    -> 캡처가 살아 있음(먼저 흘린 표식 warning이 잡힌다), 그 밖의 warning 0줄
    """
    process = _LiveProcess()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        logging.getLogger(LOGGER).warning("표식")
        monkeypatch.setattr(ffmpeg_module, "_set_suspended", lambda proc, suspended: suspended)
        paused = [True]
        control = _Control(process, timeout=60, should_stop=None, should_pause=lambda: paused[0])
        control.poll()
        process.returncode = 0  # 멈춰 있는 동안 끝났다
        paused[0] = False
        control.poll()

    assert _messages(caplog, logging.WARNING) == ["표식"]
