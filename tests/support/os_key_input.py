"""실제 화면 테스트가 OS 키 메시지 · 이벤트로 키를 누르게 한다 — Windows 창 메시지 · macOS CGEventPostToPid (#644).

Qt 테스트 키(`QTest.keyClick`)는 Qt 키 코드를 바로 넣어 OS 키 매퍼를 거치지 않는다. 단축키 후보가 물리 키에서
만들어지는 결함(Ctrl+Shift+Tab → Backtab)은 OS가 만든 키 이벤트로만 드러난다.

두 OS 모두 키를 이 프로세스에만 넣는다 — Windows는 대상 창에 키 메시지를 넣고(`PostMessageW`), macOS는
이 프로세스에 키 이벤트를 보낸다(`CGEventPostToPid`). 앞 창이 다른 프로그램이어도 그 프로그램은 키를 받지 않는다.
"""

import ctypes
import os
import sys
from collections.abc import Callable
from ctypes import wintypes

SUPPORTED = sys.platform in ("win32", "darwin")  # OS 키 입력을 낼 수 있는 플랫폼

# 수정자 이름 — 물리 키 기준. macOS의 "ctrl"은 Control 키(Qt Meta), "cmd"는 Command 키(Qt Ctrl)다
CTRL = "ctrl"  # Control 키
SHIFT = "shift"  # Shift 키
CMD = "cmd"  # Command 키(macOS만)

# ---- Windows ----
_WIN_VK = {
    CTRL: (0x11, 0xA2),  # VK_CONTROL · VK_LCONTROL
    SHIFT: (0x10, 0xA0),  # VK_SHIFT · VK_LSHIFT
}
_WIN_KEY = {
    "tab": (0x09, 0x0F),  # VK_TAB · 스캔 코드
}
_WM_KEYDOWN = 0x0100  # 키 누름 메시지
_WM_KEYUP = 0x0101  # 키 뗌 메시지
_KEY_DOWN_BIT = 0x80  # 키 상태 배열에서 눌림을 뜻하는 비트
_KEYUP_FLAGS = 0xC0000000  # WM_KEYUP lParam — 앞 상태 눌림(30) · 떼는 중(31)


def _win_press(hwnd: int, modifiers: list[str], key: str, wait: Callable[[], None]) -> None:
    """이 스레드의 키 상태에 수정자를 눌러 두고, 창에 WM_KEYDOWN · WM_KEYUP을 넣는다.

    Qt Windows 키 매퍼는 메시지의 가상 키 · 스캔 코드와 `GetKeyState`로 키 이벤트와 단축키 후보를 만든다.
    `SetKeyboardState`는 이 스레드의 키 상태만 바꾸고, 메시지는 이 창에만 들어가 앞 창이 어느 것이든
    다른 프로그램은 키를 받지 않는다. Qt가 메시지를 처리하는 동안(wait) 수정자를 눌린 채 두고 되돌린다.
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    saved = (ctypes.c_ubyte * 256)()
    if not user32.GetKeyboardState(saved):
        raise ctypes.WinError(ctypes.get_last_error())
    held = (ctypes.c_ubyte * 256)(*saved)
    for modifier in modifiers:
        for code in _WIN_VK[modifier]:
            held[code] |= _KEY_DOWN_BIT
    vk, scan = _WIN_KEY[key]
    if not user32.SetKeyboardState(held):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        for message, lparam in (
            (_WM_KEYDOWN, 1 | scan << 16),
            (_WM_KEYUP, 1 | scan << 16 | _KEYUP_FLAGS),
        ):
            if not user32.PostMessageW(hwnd, message, vk, lparam):
                raise ctypes.WinError(ctypes.get_last_error())
        wait()
    finally:
        user32.SetKeyboardState(saved)


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


def _mac_press(modifiers: list[str], key: str, wait: Callable[[], None]) -> None:
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
    wait()


def press(window_id: int, modifiers: list[str], key: str, wait: Callable[[], None]) -> None:
    """수정자를 누른 채 키 하나를 누르고 떼고, wait로 Qt가 처리하기를 기다린다 — OS 키 경로로.

    Args:
        window_id: 키를 받을 창의 `winId()`(Windows만 쓴다)
        modifiers: 누르는 차례의 수정자(`CTRL` · `SHIFT` · `CMD`)
        key: "tab" · "[" · "]"(macOS)
        wait: 키를 넣은 뒤 부른다 — Qt가 키를 처리할 때까지 기다린다(Windows는 그동안 수정자를 눌린 채 둔다)
    """
    if sys.platform == "win32":
        _win_press(window_id, modifiers, key, wait)
    elif sys.platform == "darwin":
        _mac_press(modifiers, key, wait)
    else:
        raise NotImplementedError(f"OS 키 입력이 없는 플랫폼: {sys.platform}")
