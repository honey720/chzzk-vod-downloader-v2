"""실제 화면 테스트가 OS 입력 경로로 키를 누르게 한다 — Windows SendInput · macOS CGEventPostToPid (#644).

Qt 테스트 키(`QTest.keyClick`)는 Qt 키 코드를 바로 넣어 OS 키 매퍼를 거치지 않는다. 단축키 후보가 물리 키에서
만들어지는 결함(Ctrl+Shift+Tab → Backtab)은 OS가 만든 키 이벤트로만 드러난다.

⚠️ 실제 키 입력은 그때 앞에 있는 창으로 간다. Windows는 누르기 전에 앞 창이 대상 창인지 확인하고, 아니면 누르지
않고 실패한다. macOS는 이 프로세스에만 보낸다(`CGEventPostToPid`).
"""

import ctypes
import os
import sys
from ctypes import wintypes

SUPPORTED = sys.platform in ("win32", "darwin")  # OS 키 입력을 낼 수 있는 플랫폼

# 수정자 이름 — 물리 키 기준. macOS의 "ctrl"은 Control 키(Qt Meta), "cmd"는 Command 키(Qt Ctrl)다
CTRL = "ctrl"  # Control 키
SHIFT = "shift"  # Shift 키
CMD = "cmd"  # Command 키(macOS만)

# ---- Windows ----
_WIN_VK = {
    CTRL: 0x11,  # VK_CONTROL
    SHIFT: 0x10,  # VK_SHIFT
    "tab": 0x09,  # VK_TAB
}
_INPUT_KEYBOARD = 1  # INPUT.type — 키보드
_KEYEVENTF_KEYUP = 0x0002  # 키 뗌


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUTUNION(ctypes.Union):
    # 공용체 크기가 MOUSEINPUT(가장 큼)이어야 SendInput이 cbSize를 받아들인다
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _win_press(hwnd: int, modifiers: list[str], key: str) -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    foreground = user32.GetForegroundWindow() or 0
    if foreground != hwnd:
        raise RuntimeError(
            f"앞 창이 대상 창이 아니다 — 키를 누르지 않았다({foreground:#x} != {hwnd:#x})"
        )
    codes = [_WIN_VK[m] for m in modifiers] + [_WIN_VK[key]]
    events = [(code, 0) for code in codes] + [(code, _KEYEVENTF_KEYUP) for code in reversed(codes)]
    inputs = (_INPUT * len(events))()
    for slot, (code, flags) in zip(inputs, events, strict=True):
        slot.type = _INPUT_KEYBOARD
        slot.u.ki = _KEYBDINPUT(code, 0, flags, 0, 0)
    sent = user32.SendInput(len(events), inputs, ctypes.sizeof(_INPUT))
    if sent != len(events):
        raise ctypes.WinError(ctypes.get_last_error())


# ---- macOS ----
_MAC_KEYCODE = {
    CTRL: 59,  # kVK_Control
    SHIFT: 56,  # kVK_Shift
    CMD: 55,  # kVK_Command
    "tab": 48,  # kVK_Tab
    "[": 33,  # kVK_ANSI_LeftBracket
    "]": 30,  # kVK_ANSI_RightBracket
}
_MAC_FLAG = {
    CTRL: 0x40000,  # kCGEventFlagMaskControl
    SHIFT: 0x20000,  # kCGEventFlagMaskShift
    CMD: 0x100000,  # kCGEventFlagMaskCommand
}
_APP_SERVICES = "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"


def _mac_press(modifiers: list[str], key: str) -> None:
    quartz = ctypes.CDLL(_APP_SERVICES)
    quartz.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
    quartz.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
    quartz.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    quartz.CGEventPostToPid.argtypes = [ctypes.c_int, ctypes.c_void_p]
    quartz.CFRelease.argtypes = [ctypes.c_void_p]
    pid = os.getpid()
    flags = 0
    sequence: list[tuple[int, bool, int]] = []
    for m in modifiers:
        flags |= _MAC_FLAG[m]
        sequence.append((_MAC_KEYCODE[m], True, flags))
    sequence.append((_MAC_KEYCODE[key], True, flags))
    sequence.append((_MAC_KEYCODE[key], False, flags))
    for m in reversed(modifiers):
        flags &= ~_MAC_FLAG[m]
        sequence.append((_MAC_KEYCODE[m], False, flags))
    for code, down, event_flags in sequence:
        event = quartz.CGEventCreateKeyboardEvent(None, code, down)
        if not event:
            raise RuntimeError("CGEventCreateKeyboardEvent가 이벤트를 만들지 못했다")
        quartz.CGEventSetFlags(event, event_flags)
        quartz.CGEventPostToPid(pid, event)
        quartz.CFRelease(event)


def press(window_id: int, modifiers: list[str], key: str) -> None:
    """수정자를 누른 채 키 하나를 누르고 뗀다 — OS 입력 경로(Windows SendInput · macOS 이 프로세스).

    Args:
        window_id: 키를 받을 창의 `winId()` — Windows는 앞 창이 이 창일 때만 누른다
        modifiers: 누르는 차례의 수정자(`CTRL` · `SHIFT` · `CMD`)
        key: "tab" · "[" · "]"(macOS)

    Raises:
        RuntimeError: Windows에서 앞 창이 대상 창이 아닐 때(키를 누르지 않음)
    """
    if sys.platform == "win32":
        _win_press(window_id, modifiers, key)
    elif sys.platform == "darwin":
        _mac_press(modifiers, key)
    else:
        raise NotImplementedError(f"OS 키 입력이 없는 플랫폼: {sys.platform}")
