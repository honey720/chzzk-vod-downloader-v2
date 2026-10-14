"""폴더가 놓인 볼륨의 파일 시스템 이름을 읽는다 — OS마다 다른 호출을 이 한 곳에 모은다 (#309).

FAT 계열(FAT · FAT32)은 파일 하나가 4GiB − 1바이트를 넘을 수 없다. 긴 영상의 구간 파일은 그보다
클 수 있고, 넘는 순간의 쓰기는 "디스크에 남은 공간이 없다"(ENOSPC)로 실패한다 — 공간이 넉넉한데도
그렇다. 받기 전에 저장 폴더의 파일 시스템을 읽어 미리 알린다(``core.downloaders.base``).

이름을 읽는 방법은 OS마다 다르다.

- Windows: ``GetVolumePathNameW`` · ``GetVolumeInformationW`` (kernel32) — "FAT32" · "exFAT" · "NTFS"
- Linux: ``/proc/mounts``에서 그 경로를 덮는 가장 긴 마운트 지점의 종류 — "vfat" · "exfat" · "ext4"
- macOS: libc ``statfs``의 ``f_fstypename`` — "msdos" · "exfat" · "apfs". ``stat -f %T``는 쓰지 않는다 —
  macOS의 stat(BSD)에서 ``-f``는 출력 형식을 뜻하고 ``%T``는 파일 종류(폴더는 "/")라, 볼륨과
  상관없이 늘 "/"가 나왔다(rc1 실기)

**읽지 못하면 None이다.** 부르는 쪽은 그때 검사하지 않는다 — 이름을 모른다고 다운로드를 막지 않는다.
"""

import ctypes
import ctypes.util
import os
import platform
import sys

# FAT 계열의 파일 하나의 최대 크기(바이트) — 4GiB − 1. 디렉토리 항목의 크기 칸이 32비트다
FAT_MAX_FILE_BYTES = 4 * 1024**3 - 1

# FAT 계열로 보는 이름(소문자) — Windows는 "FAT" · "FAT32", Linux는 "vfat" · "msdos",
# macOS는 "msdos". exFAT은 들지 않는다(파일 하나의 한계가 사실상 없다)
_FAT_NAMES = frozenset({"fat", "fat12", "fat16", "fat32", "vfat", "msdos"})

_NAME_CHARS = 64  # Windows가 파일 시스템 이름을 적어 줄 버퍼의 글자 수
_MFSTYPENAMELEN = 16  # macOS statfs의 파일 시스템 이름 칸의 바이트 수(<sys/mount.h>)
_MAXPATHLEN = 1024  # macOS statfs의 마운트 경로 칸의 바이트 수(<sys/param.h>)

_kernel32 = None  # Windows의 kernel32 — 처음 쓸 때 한 번만 연다
_statfs = None  # macOS libc의 statfs 함수 — 처음 쓸 때 한 번만 찾는다


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
    # AttributeError: libc에서 statfs 함수를 찾지 못했다(macOS)
    except (OSError, ValueError, AttributeError):
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


class _StatFs(ctypes.Structure):
    """macOS의 ``struct statfs``(64비트 inode 판, <sys/mount.h>) — 배치를 그대로 옮긴다."""

    _fields_ = [
        ("f_bsize", ctypes.c_uint32),
        ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64),
        ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64),
        ("f_fsid", ctypes.c_int32 * 2),
        ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32),
        ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * _MFSTYPENAMELEN),
        ("f_mntonname", ctypes.c_char * _MAXPATHLEN),
        ("f_mntfromname", ctypes.c_char * _MAXPATHLEN),
        ("f_flags_ext", ctypes.c_uint32),
        ("f_reserved", ctypes.c_uint32 * 7),
    ]


def _macos_statfs():
    """libc의 statfs 함수 — 처음 쓸 때 한 번만 찾는다.

    Intel(x86_64)에서는 같은 구조체를 채우는 이름이 ``statfs$INODE64``다. Apple Silicon(arm64)에는
    그 판 하나뿐이라 ``statfs``다.
    """
    global _statfs
    if _statfs is None:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        name = "statfs$INODE64" if platform.machine() == "x86_64" else "statfs"
        function = getattr(libc, name)
        function.argtypes = [ctypes.c_char_p, ctypes.POINTER(_StatFs)]
        function.restype = ctypes.c_int
        _statfs = function
    return _statfs


def _macos_name(path: str) -> str | None:
    """macOS — libc ``statfs``가 채워 주는 파일 시스템 종류(``f_fstypename``)."""
    info = _StatFs()
    if _macos_statfs()(os.fsencode(path), ctypes.byref(info)) != 0:
        return None
    return info.f_fstypename.decode("utf-8", errors="replace") or None
