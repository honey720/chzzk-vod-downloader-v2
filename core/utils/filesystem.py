"""폴더가 놓인 볼륨의 파일 시스템 이름을 읽는다 — OS마다 다른 호출을 이 한 곳에 모은다 (#309).

FAT 계열(FAT · FAT32)은 파일 하나가 4GiB − 1바이트를 넘을 수 없다. 긴 영상의 구간 파일은 그보다
클 수 있고, 넘는 순간의 쓰기는 "디스크에 남은 공간이 없다"(ENOSPC)로 실패한다 — 공간이 넉넉한데도
그렇다. 받기 전에 저장 폴더의 파일 시스템을 읽어 미리 알린다(``core.downloaders.base``).

이름을 읽는 방법은 OS마다 다르다.

- Windows: ``GetVolumePathNameW`` · ``GetVolumeInformationW`` (kernel32) — "FAT32" · "exFAT" · "NTFS"
- Linux: ``/proc/mounts``에서 그 경로를 덮는 가장 긴 마운트 지점의 종류 — "vfat" · "exfat" · "ext4"
- macOS: ``stat -f %T`` — "msdos" · "exfat" · "apfs"

**읽지 못하면 None이다.** 부르는 쪽은 그때 검사하지 않는다 — 이름을 모른다고 다운로드를 막지 않는다.
"""

import os
import subprocess
import sys

# FAT 계열의 파일 하나의 최대 크기(바이트) — 4GiB − 1. 디렉토리 항목의 크기 칸이 32비트다
FAT_MAX_FILE_BYTES = 4 * 1024**3 - 1

# FAT 계열로 보는 이름(소문자) — Windows는 "FAT" · "FAT32", Linux는 "vfat" · "msdos",
# macOS는 "msdos". exFAT은 들지 않는다(파일 하나의 한계가 사실상 없다)
_FAT_NAMES = frozenset({"fat", "fat12", "fat16", "fat32", "vfat", "msdos"})

_STAT_TIMEOUT = 5  # macOS의 stat 호출을 기다리는 시간(초) — 응답 없는 마운트에 매달리지 않는다
_NAME_CHARS = 64  # Windows가 파일 시스템 이름을 적어 줄 버퍼의 글자 수

_kernel32 = None  # Windows의 kernel32 — 처음 쓸 때 한 번만 연다


def is_fat(name: str | None) -> bool:
    """그 파일 시스템 이름이 FAT 계열(파일 하나가 4GiB를 넘지 못한다)인지."""
    return name is not None and name.strip().lower() in _FAT_NAMES


def filesystem_name(folder: str) -> str | None:
    """folder가 놓인 볼륨의 파일 시스템 이름. 읽지 못하면 None이다.

    폴더가 아직 없으면 실제로 있는 윗폴더에서 읽는다. 이름의 대소문자는 OS가 준 그대로다.
    """
    path = _existing(folder)
    try:
        if sys.platform == "win32":
            return _windows_name(path)
        if sys.platform == "darwin":
            return _macos_name(path)
        return _linux_name(path)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _existing(folder: str) -> str:
    """folder에서 위로 올라가며 처음 만나는, 실제로 있는 폴더."""
    path = os.path.abspath(folder)
    while not os.path.isdir(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def _windows_name(path: str) -> str | None:
    """Windows — 그 경로의 볼륨 뿌리를 찾아 파일 시스템 이름을 읽는다."""
    import ctypes  # Windows에서만 쓴다

    global _kernel32
    if _kernel32 is None:
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    root = ctypes.create_unicode_buffer(max(len(path) + 2, 260))
    if not _kernel32.GetVolumePathNameW(ctypes.c_wchar_p(path), root, len(root)):
        return None
    name = ctypes.create_unicode_buffer(_NAME_CHARS)
    read = _kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(root.value), None, 0, None, None, None, name, len(name)
    )
    return name.value or None if read else None


def _linux_name(path: str) -> str | None:
    """Linux — /proc/mounts에서 그 경로를 덮는 가장 긴 마운트 지점의 종류."""
    real = os.path.realpath(path)
    with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
        return mount_type(f.read(), real)


def mount_type(mounts: str, path: str) -> str | None:
    """/proc/mounts의 글에서 path를 덮는 가장 긴 마운트 지점의 파일 시스템 종류. 없으면 None.

    마운트 지점의 빈칸 · 탭 · 줄바꿈 · 역슬래시는 8진수(``\\040`` 등)로 적혀 있다 — 풀어서 견준다.
    """
    best, kind = "", None
    for line in mounts.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        point = _unescape(fields[1])
        inside = path == point or path.startswith(point.rstrip("/") + "/")
        if inside and len(point) >= len(best):
            best, kind = point, fields[2]
    return kind


def _unescape(text: str) -> str:
    """/proc/mounts가 8진수로 적은 글자(``\\040`` = 빈칸)를 되돌린다."""
    for code, char in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        text = text.replace(code, char)
    return text


def _macos_name(path: str) -> str | None:
    """macOS — ``stat -f %T``가 적어 주는 파일 시스템 종류."""
    done = subprocess.run(
        ["/usr/bin/stat", "-f", "%T", path],
        capture_output=True,
        text=True,
        timeout=_STAT_TIMEOUT,
        check=False,
    )
    name = done.stdout.strip()
    return name or None if done.returncode == 0 else None
