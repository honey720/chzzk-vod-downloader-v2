"""실제 화면 테스트가 OS 키 메시지 · 이벤트로 키를 누르게 한다 — Windows 창 메시지 · macOS NSEvent (#644).

Qt 테스트 키(`QTest.keyClick`)는 Qt 키 코드를 바로 넣어 OS 키 매퍼를 거치지 않는다. 단축키 후보가 물리 키에서
만들어지는 결함(Ctrl+Shift+Tab → Backtab)은 OS가 만든 키 이벤트로만 드러난다.

두 OS 모두 키를 이 프로세스에만 넣는다 — Windows는 대상 창에 키 메시지를 넣고(`PostMessageW`), macOS는
NSEvent를 만들어 이 앱에 넣는다(`[NSApp sendEvent:]`). 앞 창이 다른 프로그램이어도 그 프로그램은 키를 받지 않는다.
"""

import ctypes
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
    "tab": 48,  # kVK_Tab
    "[": 33,  # kVK_ANSI_LeftBracket
    "]": 30,  # kVK_ANSI_RightBracket
}
_MAC_FLAG = {
    CTRL: 1 << 18,  # NSEventModifierFlagControl
    SHIFT: 1 << 17,  # NSEventModifierFlagShift
    CMD: 1 << 20,  # NSEventModifierFlagCommand
}
_MAC_CHARS = {  # 키 → (Shift 없이, Shift와) 글자 — Shift+Tab은 NSBackTabCharacter(0x19)
    "tab": ("\t", "\x19"),
    "[": ("[", "{"),
    "]": ("]", "}"),
}
_NS_KEY_DOWN = 10  # NSEventTypeKeyDown
_NS_KEY_UP = 11  # NSEventTypeKeyUp
_LIBOBJC = "/usr/lib/libobjc.A.dylib"


class _NSPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


def _mac_press(view: int, modifiers: list[str], key: str, wait: Callable[[], None]) -> None:
    """NSEvent 키 누름 · 뗌을 만들어 `[NSApp sendEvent:]`로 이 앱에 넣는다.

    Qt Cocoa 플러그인은 NSEvent의 keyCode · 글자 · 수정자 플래그로 키 이벤트와 단축키 후보를 만든다 — 실제
    키보드와 같은 길이다. 이벤트는 이 프로세스 안에서만 돈다. `CGEventPostToPid`로 보낸 이벤트는 Qt에 오지
    않았다(#644).
    """
    objc = ctypes.CDLL(_LIBOBJC)
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    address = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value

    def send(restype: type | None, receiver: int | None, selector: bytes, *args: tuple) -> object:
        argtypes = [ctypes.c_void_p, ctypes.c_void_p, *(type_ for type_, _ in args)]
        function = ctypes.CFUNCTYPE(restype, *argtypes)(address)
        return function(receiver, objc.sel_registerName(selector), *(value for _, value in args))

    def ns_string(text: str) -> int:
        return send(
            ctypes.c_void_p,
            objc.objc_getClass(b"NSString"),
            b"stringWithUTF8String:",
            (ctypes.c_char_p, text.encode("utf-8")),
        )

    app = send(ctypes.c_void_p, objc.objc_getClass(b"NSApplication"), b"sharedApplication")
    window = send(ctypes.c_void_p, view, b"window")
    number = send(ctypes.c_long, window, b"windowNumber")
    flags = 0
    for modifier in modifiers:
        flags |= _MAC_FLAG[modifier]
    plain, shifted = _MAC_CHARS[key]
    chars = ns_string(shifted if SHIFT in modifiers else plain)
    for event_type in (_NS_KEY_DOWN, _NS_KEY_UP):
        event = send(
            ctypes.c_void_p,
            objc.objc_getClass(b"NSEvent"),
            b"keyEventWithType:location:modifierFlags:timestamp:windowNumber:context:characters:"
            b"charactersIgnoringModifiers:isARepeat:keyCode:",
            (ctypes.c_ulong, event_type),
            (_NSPoint, _NSPoint(0.0, 0.0)),
            (ctypes.c_ulong, flags),
            (ctypes.c_double, 0.0),
            (ctypes.c_long, number),
            (ctypes.c_void_p, None),
            (ctypes.c_void_p, chars),
            (ctypes.c_void_p, chars),
            (ctypes.c_bool, False),
            (ctypes.c_ushort, _MAC_KEYCODE[key]),
        )
        if not event:
            raise RuntimeError("NSEvent keyEventWithType가 이벤트를 만들지 못했다")
        send(None, app, b"sendEvent:", (ctypes.c_void_p, event))
    wait()


def press(window_id: int, modifiers: list[str], key: str, wait: Callable[[], None]) -> None:
    """수정자를 누른 채 키 하나를 누르고 떼고, wait로 Qt가 처리하기를 기다린다 — OS 키 경로로.

    Args:
        window_id: 키를 받을 창의 `winId()`(Windows HWND · macOS NSView)
        modifiers: 누르는 차례의 수정자(`CTRL` · `SHIFT` · `CMD`)
        key: "tab" · "[" · "]"(macOS)
        wait: 키를 넣은 뒤 부른다 — Qt가 키를 처리할 때까지 기다린다(Windows는 그동안 수정자를 눌린 채 둔다)
    """
    if sys.platform == "win32":
        _win_press(window_id, modifiers, key, wait)
    elif sys.platform == "darwin":
        _mac_press(window_id, modifiers, key, wait)
    else:
        raise NotImplementedError(f"OS 키 입력이 없는 플랫폼: {sys.platform}")
