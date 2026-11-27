"""OS의 애니메이션 설정을 읽는다 — 설정에 사용자가 고른 값이 없을 때 카드 애니메이션의 기본값 (#645).

Qt 6.11에는 움직임 줄이기 힌트가 없어(`QAccessibilityHints`는 대비만 준다) OS마다 직접 읽는다(설계 §14.4).
새 의존성을 쓰지 않는다. 한 OS의 API는 그 OS 갈래 안에서만 부른다(SPEC §8.4).

- Windows: ``SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION)`` — 설정 › 접근성 › 시각 효과 › 애니메이션 효과
- macOS: ``NSWorkspace.accessibilityDisplayShouldReduceMotion`` — 손쉬운 사용 › 디스플레이 › 동작 줄이기.
  objc 런타임을 ctypes로 부른다
- Linux: GNOME 계열 ``gsettings get org.gnome.desktop.interface enable-animations``(하위 프로세스) →
  읽지 못하면 KDE ``kdeglobals``의 ``[KDE] AnimationDurationFactor``. 단 ``XDG_CURRENT_DESKTOP``에
  ``KDE``가 있으면 KDE를 먼저 읽는다 — KDE 세션에 GNOME 스키마가 깔려 있으면 gsettings가 스키마
  기본값 참을 돌려줘 KDE에서 끈 것을 못 본다

읽지 못하면 켬이다 — 이 모듈이 생기기 전의 기본값이다. 앱 시작 때와 앱이 다시 활성이 될 때 읽는다
(`app/viewmodels/card_display.py`).
"""

import configparser
import ctypes
import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)

SPI_GETCLIENTAREAANIMATION = 0x1042  # SystemParametersInfoW 동작 번호 — 창 안 애니메이션 켬 여부
APPKIT_PATH = "/System/Library/Frameworks/AppKit.framework/AppKit"  # NSWorkspace가 든 프레임워크
OBJC_PATH = "/usr/lib/libobjc.A.dylib"  # objc 런타임(objc_getClass · objc_msgSend)
GSETTINGS_COMMAND = (
    "gsettings",
    "get",
    "org.gnome.desktop.interface",
    "enable-animations",
)  # GNOME 계열 애니메이션 켬 여부를 읽는 명령
GSETTINGS_TIMEOUT = 1.0  # gsettings 제한 시간(초) — 창이 뜨는 길에서 부르므로 오래 기다리지 않는다
KDE_SECTION = "KDE"  # kdeglobals의 절 이름
KDE_FACTOR_KEY = "AnimationDurationFactor"  # 애니메이션 시간 배율 — 0이면 애니메이션 끔
DESKTOP_ENV = "XDG_CURRENT_DESKTOP"  # 데스크톱 이름 목록(쌍점으로 나눔, 예: "ubuntu:GNOME")
KDE_DESKTOP = "KDE"  # 위 목록에서 KDE 세션을 뜻하는 이름
DEFAULT = True  # 읽지 못했을 때의 값 — 이 모듈 전의 기본값(켬)


def system_animations() -> bool:
    """OS가 애니메이션을 켜 두었는지 돌려준다 — 읽지 못하면 `DEFAULT`(켬)."""
    try:
        if sys.platform == "win32":
            value = read_windows()
        elif sys.platform == "darwin":
            value = read_macos()
        else:
            value = read_linux()
    except Exception:  # noqa: BLE001 — 읽는 일이 앱을 깨뜨리지 않는다
        logger.debug("OS 애니메이션 설정을 읽지 못함", exc_info=True)
        value = None
    return DEFAULT if value is None else value


def read_windows() -> bool | None:
    """Windows의 「애니메이션 효과」 — 켬이면 참. 함수가 실패하면 None."""
    value = ctypes.c_int()  # BOOL
    ok = ctypes.windll.user32.SystemParametersInfoW(
        SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(value), 0
    )
    if not ok:
        return None
    return bool(value.value)


def read_macos() -> bool | None:
    """macOS의 「동작 줄이기」를 뒤집어 돌려준다 — 동작 줄이기가 켜져 있으면 거짓. 못 읽으면 None.

    `objc_msgSend`는 돌려주는 꼴마다 함수 꼴을 따로 만들어 부른다 — 같은 함수 객체의 `restype`을 바꾸면
    다른 곳의 부름에 남는다.
    """
    ctypes.cdll.LoadLibrary(APPKIT_PATH)  # NSWorkspace 클래스가 런타임에 올라오게 한다
    objc = ctypes.cdll.LoadLibrary(OBJC_PATH)
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    send_id = ctypes.cast(
        objc.objc_msgSend, ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
    )
    send_bool = ctypes.cast(
        objc.objc_msgSend, ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    )
    workspace_class = objc.objc_getClass(b"NSWorkspace")
    if not workspace_class:
        return None
    workspace = send_id(workspace_class, objc.sel_registerName(b"sharedWorkspace"))
    if not workspace:
        return None
    selector = objc.sel_registerName(b"accessibilityDisplayShouldReduceMotion")
    return not send_bool(workspace, selector)


def read_linux() -> bool | None:
    """Linux 데스크톱의 애니메이션 켬 여부를 돌려준다 — 둘 다 못 읽으면 None.

    KDE 세션(`XDG_CURRENT_DESKTOP`에 `KDE`)이면 KDE를 먼저, 못 읽으면 GNOME 계열을 읽는다. 그 밖은
    GNOME 계열을 먼저, 못 읽으면 KDE. KDE 세션에 GNOME 스키마가 깔려 있으면 gsettings가 스키마
    기본값 참을 돌려주기 때문이다(#645).
    """
    readers = (read_kde, read_gnome) if is_kde_session() else (read_gnome, read_kde)
    for reader in readers:
        value = reader()
        if value is not None:
            return value
    return None


def is_kde_session() -> bool:
    """`XDG_CURRENT_DESKTOP`의 이름 목록에 `KDE`가 있으면 참(대소문자 무시)."""
    names = os.environ.get(DESKTOP_ENV, "").split(":")
    return any(name.strip().upper() == KDE_DESKTOP for name in names)


def read_gnome() -> bool | None:
    """`gsettings`로 GNOME 계열의 `enable-animations`를 읽는다 — 명령이 없거나 실패하면 None."""
    try:
        result = subprocess.run(
            GSETTINGS_COMMAND,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=GSETTINGS_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return {"true": True, "false": False}.get(result.stdout.strip())


def read_kde() -> bool | None:
    """KDE `kdeglobals`의 `AnimationDurationFactor` — 0이면 거짓. 파일 · 키가 없거나 숫자가 아니면 None.

    파일은 `XDG_CONFIG_HOME`(절대 경로일 때만, 아니면 `~/.config`) 아래 `kdeglobals`다.
    """
    base = os.environ.get("XDG_CONFIG_HOME", "")
    if not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".config")
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read(os.path.join(base, "kdeglobals"), encoding="utf-8")
    except (configparser.Error, UnicodeDecodeError):
        return None
    raw = parser.get(KDE_SECTION, KDE_FACTOR_KEY, fallback=None)
    if raw is None:
        return None
    try:
        return float(raw) != 0
    except ValueError:
        return None
