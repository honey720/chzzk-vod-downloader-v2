"""테스트가 Windows에서 메시지 상자를 띄울 때 시스템 알림음이 나지 않게 하는 도구 (#664).

Qt는 ``QMessageBox::showEvent``에서 접근성 이벤트 ``QAccessible::Alert``를 낸다. Windows 플랫폼
플러그인(``qwindowsuiaaccessibility.cpp``)은 접근성이 꺼져 있어도 이 이벤트를 받으면 먼저 소리를 낸다 —
Information은 ``SystemAsterisk``, Warning은 ``SystemExclamation``, Critical은 ``SystemHand``. 메뉴를
열거나 고를 때(``PopupMenuStart`` · ``MenuCommand``)도 같은 길이다. 그래서 실제 화면 테스트가 경고
대화상자를 띄울 때마다 알림음이 난다.

``QAccessible::installUpdateHandler``로 처리기를 걸면 ``QAccessible::updateAccessibility``가 플랫폼
플러그인 대신 그 처리기만 부른다(Qt 6.11 ``qaccessible.cpp``). 이 함수는 PySide6에 바인딩이 없어
``Qt6Gui.dll``에서 ctypes로 찾는다. 처리기는 받은 이벤트 수만 센다.

``conftest.py``가 세션을 시작할 때 ``install``을 부른다. 앱 실행(``CVDv2.py``)은 이 모듈을 읽지 않으므로
앱을 쓸 때의 알림음은 그대로다. 접근성 인터페이스 조회(``QAccessible.queryAccessibleInterface``)는 이
처리기와 상관없다 — 바뀌는 것은 스크린 리더 같은 UI Automation 클라이언트에 가는 알림뿐이다.
"""

import ctypes
from pathlib import Path

# Qt6Gui.dll이 내보내는 QAccessible::installUpdateHandler의 MSVC 이름(x64).
# static UpdateHandler installUpdateHandler(UpdateHandler) — UpdateHandler는 void (*)(QAccessibleEvent *)
INSTALL_SYMBOL = "?installUpdateHandler@QAccessible@@SAP6AXPEAVQAccessibleEvent@@@ZP6AX0@Z@Z"

# 건 처리기(ctypes 객체 — 가비지 컬렉션되지 않게 붙잡아 둔다) · 처리기가 받은 이벤트 수
_state: dict = {"handler": None, "updates": 0}


def _on_update(event: int | None) -> None:
    """``QAccessible::updateAccessibility``가 플랫폼 플러그인 대신 부르는 처리기. 수만 센다."""
    _state["updates"] += 1


_HANDLER_TYPE = ctypes.CFUNCTYPE(None, ctypes.c_void_p)  # void (*)(QAccessibleEvent *)


def _install_function() -> "ctypes._CFuncPtr":
    """``Qt6Gui.dll``의 ``installUpdateHandler``를 ctypes 함수로 돌려준다(Windows 전용).

    Raises:
        AttributeError: Qt6Gui.dll에 그 이름이 없을 때(Qt 버전이 바뀌어 이름이 달라짐)
    """
    import PySide6  # Qt6Gui.dll이 PySide6 폴더에 있다

    gui = ctypes.WinDLL(str(Path(PySide6.__file__).resolve().parent / "Qt6Gui.dll"))
    function = getattr(gui, INSTALL_SYMBOL)
    function.restype = ctypes.c_void_p
    function.argtypes = [ctypes.c_void_p]
    return function


def install(platform: str) -> bool:
    """접근성 갱신 처리기를 건다 — Windows일 때만. 두 번 불러도 한 번만 건다.

    Args:
        platform: ``sys.platform`` 값

    Returns:
        걸었으면 True

    Raises:
        AttributeError: Qt6Gui.dll에서 ``installUpdateHandler``를 찾지 못했을 때 — 조용히 넘어가면
            알림음이 다시 나도 아무도 모른다
    """
    if platform != "win32" or _state["handler"] is not None:
        return False
    handler = _HANDLER_TYPE(_on_update)
    _install_function()(ctypes.cast(handler, ctypes.c_void_p))
    _state["handler"] = handler
    return True


def installed() -> bool:
    """이 프로세스에서 처리기를 걸었으면 True."""
    return _state["handler"] is not None


def update_count() -> int:
    """처리기가 받은 접근성 이벤트 수."""
    return _state["updates"]


def current_handler_address() -> int:
    """지금 Qt에 걸린 접근성 갱신 처리기의 주소(없으면 0)를 돌려준다 — Windows 전용.

    ``installUpdateHandler``는 바꾸기 전 처리기를 돌려주므로, 빈 처리기를 걸어 읽고 바로 되돌린다.
    """
    function = _install_function()
    previous = function(None) or 0
    function(previous or None)
    return previous
