"""OS 애니메이션 설정 읽기 — Windows · macOS · GNOME · KDE 갈래와 읽지 못할 때의 켬 (#645).

갈래마다 OS 접점(ctypes · 하위 프로세스 · 파일)을 가짜로 바꿔 어느 OS 러너에서도 모든 갈래를 돈다.
conftest의 autouse 픽스처가 `system_animations`를 켬으로 고정하므로, 이 파일은 모듈을 읽을 때 잡아 둔 원래
함수(`REAL_SYSTEM_ANIMATIONS`)를 부른다.
"""

import ctypes
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

import app.system.motion_preference as motion_preference

#: conftest가 고정하기 전의 원래 함수 — 수집 때 잡는다
REAL_SYSTEM_ANIMATIONS = motion_preference.system_animations


# ================================================================ Windows


class _FakeUser32:
    """`SystemParametersInfoW` 대역 — 받은 인자를 적고 넘겨받은 BOOL 칸에 `value`를 쓴다."""

    def __init__(self, value: int, ok: int = 1) -> None:
        self.value = value
        self.ok = ok
        self.calls: list[tuple[int, int, int]] = []

    def SystemParametersInfoW(self, action: int, param: int, out: object, flags: int) -> int:  # noqa: N802
        self.calls.append((action, param, flags))
        out._obj.value = self.value  # ctypes.byref가 감싼 칸
        return self.ok


class _FakeWindll:
    """`ctypes.windll` 대역 — `user32`만 있다."""

    def __init__(self, user32: _FakeUser32) -> None:
        self.user32 = user32


@pytest.mark.parametrize(("value", "expected"), [(1, True), (0, False)])
def test_windows_returns_the_client_area_animation_flag(
    monkeypatch: pytest.MonkeyPatch, value: int, expected: bool
) -> None:
    """Windows 갈래는 SPI_GETCLIENTAREAANIMATION 값이 1이면 참, 0이면 거짓이어야 한다.

    SystemParametersInfoW(0x1042, 0, &BOOL, 0) -> BOOL = value
    """
    user32 = _FakeUser32(value)
    monkeypatch.setattr(ctypes, "windll", _FakeWindll(user32), raising=False)
    assert motion_preference.read_windows() is expected
    assert user32.calls == [(0x1042, 0, 0)]


def test_windows_returns_none_when_the_call_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """SystemParametersInfoW가 0을 돌려주면 Windows 갈래는 None이어야 한다.

    반환 0 · BOOL 칸 0 -> None
    """
    monkeypatch.setattr(ctypes, "windll", _FakeWindll(_FakeUser32(0, ok=0)), raising=False)
    assert motion_preference.read_windows() is None


# ================================================================ macOS

_SEND = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)  # objc_msgSend 대역 꼴
_CLASS_ID = 0x1000  # NSWorkspace 클래스 대역 주소
_WORKSPACE_ID = 0x2000  # sharedWorkspace가 돌려주는 객체 대역 주소
_SELECTORS = {b"sharedWorkspace": 0x10, b"accessibilityDisplayShouldReduceMotion": 0x20}


class _FakeObjc:
    """objc 런타임 대역 — 클래스 · 셀렉터는 고정 주소, `objc_msgSend`는 진짜 C 함수 포인터다."""

    def __init__(self, reduce_motion: bool, workspace: int = _WORKSPACE_ID) -> None:
        self.sent: list[tuple[int, int]] = []

        def send(receiver: int, selector: int) -> int:
            self.sent.append((receiver, selector))
            if selector == _SELECTORS[b"sharedWorkspace"]:
                return workspace
            return int(reduce_motion)

        self._send = _SEND(send)  # 대역이 사는 동안 콜백을 붙잡아 둔다
        self.objc_msgSend = self._send
        self.objc_getClass = self._get_class
        self.sel_registerName = self._register

    @staticmethod
    def _get_class(name: bytes) -> int:
        return _CLASS_ID if name == b"NSWorkspace" else 0

    @staticmethod
    def _register(name: bytes) -> int:
        return _SELECTORS[name]


class _FakeCdll:
    """`ctypes.cdll` 대역 — 불러온 경로를 적고 objc 경로면 `_FakeObjc`를 준다."""

    def __init__(self, objc: _FakeObjc) -> None:
        self.objc = objc
        self.loaded: list[str] = []

    def LoadLibrary(self, path: str) -> object:  # noqa: N802
        self.loaded.append(path)
        return self.objc if path == motion_preference.OBJC_PATH else object()


@pytest.mark.parametrize(("reduce_motion", "expected"), [(False, True), (True, False)])
def test_macos_returns_the_opposite_of_reduce_motion(
    monkeypatch: pytest.MonkeyPatch, reduce_motion: bool, expected: bool
) -> None:
    """macOS 갈래는 동작 줄이기가 꺼져 있으면 참, 켜져 있으면 거짓이어야 한다.

    AppKit · libobjc를 불러온다 → [NSWorkspace sharedWorkspace] → accessibilityDisplayShouldReduceMotion
    -> reduce_motion의 반대
    """
    objc = _FakeObjc(reduce_motion)
    cdll = _FakeCdll(objc)
    monkeypatch.setattr(ctypes, "cdll", cdll)
    assert motion_preference.read_macos() is expected
    assert cdll.loaded == [motion_preference.APPKIT_PATH, motion_preference.OBJC_PATH]
    assert objc.sent == [
        (_CLASS_ID, _SELECTORS[b"sharedWorkspace"]),
        (_WORKSPACE_ID, _SELECTORS[b"accessibilityDisplayShouldReduceMotion"]),
    ]


def test_macos_returns_none_without_a_shared_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """sharedWorkspace가 nil이면 macOS 갈래는 None이어야 한다.

    sharedWorkspace -> 0 -> None, 동작 줄이기는 묻지 않음
    """
    objc = _FakeObjc(False, workspace=0)
    monkeypatch.setattr(ctypes, "cdll", _FakeCdll(objc))
    assert motion_preference.read_macos() is None
    assert len(objc.sent) == 1


# ================================================================ Linux GNOME


def _fake_run(
    calls: list[dict], stdout: str = "", returncode: int = 0, error: Exception | None = None
) -> Callable[..., subprocess.CompletedProcess]:
    """`subprocess.run` 대역 — 받은 인자를 `calls`에 적고 정한 결과를 돌려주거나 `error`를 올린다."""

    def run(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess:
        calls.append({"command": tuple(command), **kwargs})
        if error is not None:
            raise error
        return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="")

    return run


@pytest.mark.parametrize(("stdout", "expected"), [("true\n", True), ("false\n", False)])
def test_gnome_reads_enable_animations(
    monkeypatch: pytest.MonkeyPatch, stdout: str, expected: bool
) -> None:
    """GNOME 갈래는 gsettings 출력이 true면 참, false면 거짓이어야 하고 제한 시간 1초로 불러야 한다.

    gsettings get org.gnome.desktop.interface enable-animations -> stdout
    """
    calls: list[dict] = []
    monkeypatch.setattr(motion_preference.subprocess, "run", _fake_run(calls, stdout))
    assert motion_preference.read_gnome() is expected
    assert calls[0]["command"] == (
        "gsettings",
        "get",
        "org.gnome.desktop.interface",
        "enable-animations",
    )
    assert calls[0]["timeout"] == 1.0


@pytest.mark.parametrize(
    ("stdout", "returncode", "error"),
    [
        ("", 0, FileNotFoundError("gsettings")),  # 명령 없음
        ("", 0, subprocess.TimeoutExpired("gsettings", 1.0)),  # 제한 시간 넘김
        ("No such schema\n", 1, None),  # 스키마 없음
        ("'yes'\n", 0, None),  # true · false가 아님
    ],
    ids=["missing", "timeout", "failed", "unexpected"],
)
def test_gnome_returns_none_when_gsettings_cannot_answer(
    monkeypatch: pytest.MonkeyPatch, stdout: str, returncode: int, error: Exception | None
) -> None:
    """gsettings가 없거나 실패하거나 true · false가 아닌 글을 내면 GNOME 갈래는 None이어야 한다.

    명령 없음 · 제한 시간 넘김 · 종료 코드 1 · 출력 'yes' -> None
    """
    calls: list[dict] = []
    monkeypatch.setattr(
        motion_preference.subprocess, "run", _fake_run(calls, stdout, returncode, error)
    )
    assert motion_preference.read_gnome() is None


# ================================================================ Linux KDE


def _kdeglobals(config_home: Path, text: str) -> None:
    """`config_home/kdeglobals`를 UTF-8로 쓴다."""
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "kdeglobals").write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    ("factor", "expected"), [("0", False), ("0.0", False), ("1", True), ("0.5", True)]
)
def test_kde_reads_animation_duration_factor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, factor: str, expected: bool
) -> None:
    """KDE 갈래는 AnimationDurationFactor가 0이면 거짓, 0이 아니면 참이어야 한다.

    XDG_CONFIG_HOME/kdeglobals [KDE] AnimationDurationFactor=factor
    """
    _kdeglobals(
        tmp_path, f"[General]\nColorScheme=Breeze\n\n[KDE]\nAnimationDurationFactor={factor}\n"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert motion_preference.read_kde() is expected


def test_kde_reads_from_home_config_when_xdg_config_home_is_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """XDG_CONFIG_HOME이 없으면 KDE 갈래는 ~/.config/kdeglobals를 읽어야 한다.

    HOME=tmp_path, ~/.config/kdeglobals [KDE] AnimationDurationFactor=0 -> 거짓
    """
    _kdeglobals(tmp_path / ".config", "[KDE]\nAnimationDurationFactor=0\n")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(motion_preference.os.path, "expanduser", lambda path: str(tmp_path))
    assert motion_preference.read_kde() is False


@pytest.mark.parametrize(
    "text",
    [
        None,  # 파일 없음
        "[KDE]\nSingleClick=false\n",  # 키 없음
        "[KDE]\nAnimationDurationFactor=fast\n",  # 숫자가 아님
        "AnimationDurationFactor=0\n",  # 절 머리 없음(configparser 오류)
    ],
    ids=["no-file", "no-key", "not-a-number", "no-section"],
)
def test_kde_returns_none_when_the_factor_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str | None
) -> None:
    """kdeglobals가 없거나 키가 없거나 숫자가 아니거나 깨졌으면 KDE 갈래는 None이어야 한다.

    네 경우 -> None
    """
    if text is not None:
        _kdeglobals(tmp_path, text)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert motion_preference.read_kde() is None


@pytest.mark.parametrize(
    ("gnome", "kde", "expected"),
    [(False, True, False), (True, False, True), (None, False, False), (None, None, None)],
)
def test_linux_asks_kde_only_when_gnome_cannot_answer(
    monkeypatch: pytest.MonkeyPatch, gnome: bool | None, kde: bool | None, expected: bool | None
) -> None:
    """Linux 갈래는 GNOME 값이 있으면 그 값, 없으면 KDE 값이어야 한다.

    (GNOME, KDE) -> expected
    """
    monkeypatch.setattr(motion_preference, "read_gnome", lambda: gnome)
    monkeypatch.setattr(motion_preference, "read_kde", lambda: kde)
    assert motion_preference.read_linux() is expected


# ================================================================ 갈래 고르기 · 읽지 못함


@pytest.mark.parametrize(
    ("platform", "reader"),
    [("win32", "read_windows"), ("darwin", "read_macos"), ("linux", "read_linux")],
)
@pytest.mark.parametrize("value", [True, False])
def test_system_animations_uses_the_reader_of_the_platform(
    monkeypatch: pytest.MonkeyPatch, platform: str, reader: str, value: bool
) -> None:
    """system_animations는 그 OS 갈래가 읽은 값을 돌려주고 다른 갈래는 부르지 않아야 한다.

    sys.platform=platform, 그 갈래 -> value, 다른 갈래 -> 부르면 실패
    """

    def other() -> bool:
        raise AssertionError("다른 OS 갈래를 불렀다")

    for name in ("read_windows", "read_macos", "read_linux"):
        monkeypatch.setattr(motion_preference, name, (lambda: value) if name == reader else other)
    monkeypatch.setattr(sys, "platform", platform)
    assert REAL_SYSTEM_ANIMATIONS() is value


@pytest.mark.parametrize("outcome", ["none", "error"])
def test_system_animations_is_on_when_the_os_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """그 OS 갈래가 None을 내거나 예외를 올리면 system_animations는 참(켬)이어야 한다.

    갈래 -> None 또는 OSError -> 참
    """

    def reader() -> bool | None:
        if outcome == "error":
            raise OSError("no such library")
        return None

    for name in ("read_windows", "read_macos", "read_linux"):
        monkeypatch.setattr(motion_preference, name, reader)
    assert REAL_SYSTEM_ANIMATIONS() is True
