"""앱 자기 프로세스의 메모리를 읽어 로그 한 줄로 남긴다 (#309).

긴 영상의 moov 색인은 수백 MB를 쥔다. 색인을 들었을 때와 놓았을 때 프로세스의 메모리가
실제로 어떻게 달라지는지를 로그로 본다. 의존성을 쓰지 않는다 — OS마다 표준 경로로 읽는다.

- Windows: ``GetProcessMemoryInfo`` — 개인 작업 집합(없으면 작업 집합)과 커밋
- Linux: ``/proc/self/statm`` — RSS
- macOS: ``task_info`` — RSS

읽지 못하면 줄을 남기지 않는다. 앱의 동작은 어느 경우에도 바뀌지 않는다.

하단바 모니터링(#504)이 1초마다 읽는다 — OS 함수 객체(ctypes)는 처음 한 번만 만들어 둔다.
부를 때마다 새로 만들면 한참 뒤 Qt 안에서 접근 위반이 났다.
"""

import ctypes
import functools
import logging
import os
import sys

logger = logging.getLogger(__name__)

LOG_PREFIX = "프로세스 메모리"  # 로그에서 이 줄을 찾는 문자열

_MEGABYTE = 1024 * 1024


def read_process_memory() -> dict[str, int]:
    """이 프로세스의 메모리를 이름 → 바이트로 돌려준다. 읽지 못하면 빈 dict.

    Returns:
        Windows는 ``{"개인 작업 집합": …, "커밋": …}``(오래된 Windows는 "작업 집합"),
        Linux · macOS는 ``{"RSS": …}``
    """
    try:
        if sys.platform == "win32":
            return _read_windows()
        if sys.platform == "darwin":
            return _read_macos()
        return _read_linux()
    except Exception:  # noqa: BLE001 — 재는 일이 앱을 깨뜨리지 않는다
        return {}


def resident_bytes() -> int | None:
    """이 프로세스가 지금 실제 메모리에 둔 크기(바이트) — 하단바 모니터링 값 (#504).

    Windows는 개인 작업 집합(작업 관리자의 「메모리」 열, 없으면 작업 집합), Linux · macOS는 RSS다.
    읽지 못하면 None.
    """
    values = read_process_memory()
    return next(iter(values.values()), None)


def log_process_memory(point: str) -> None:
    """지금의 프로세스 메모리를 로그 한 줄로 남긴다. 읽지 못하면 아무것도 남기지 않는다.

    Args:
        point: 어느 시점인지 — "조회 끝" · "편집 창 닫힘" · "다운로드 시작" 등
    """
    try:
        values = read_process_memory()
        if not values:
            return
        parts = " · ".join(f"{name} {size / _MEGABYTE:,.1f}MB" for name, size in values.items())
        logger.info("%s [%s] %s", LOG_PREFIX, point, parts)
    except Exception:  # noqa: BLE001 — 재는 일이 앱을 깨뜨리지 않는다
        return


@functools.cache
def _windows_api() -> tuple:
    """Windows의 (구조체 클래스, K32GetProcessMemoryInfo, 이 프로세스 핸들) — 한 번만 만든다."""
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        """PROCESS_MEMORY_COUNTERS_EX2 — 끝의 두 칸은 Windows 10 22H2부터 있다."""

        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
            ("PrivateWorkingSetSize", ctypes.c_uint64),
            ("SharedCommitUsage", ctypes.c_uint64),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    query = kernel32.K32GetProcessMemoryInfo
    query.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    query.restype = wintypes.BOOL
    return Counters, query, kernel32.GetCurrentProcess()


def _read_windows() -> dict[str, int]:
    Counters, query, process = _windows_api()
    counters = Counters()
    counters.cb = ctypes.sizeof(Counters)
    if query(process, ctypes.byref(counters), counters.cb):
        return {"개인 작업 집합": counters.PrivateWorkingSetSize, "커밋": counters.PrivateUsage}
    # 끝의 두 칸을 모르는 Windows — 그 앞까지만(PROCESS_MEMORY_COUNTERS_EX) 다시 묻는다
    counters = Counters()
    counters.cb = Counters.PrivateWorkingSetSize.offset
    if query(process, ctypes.byref(counters), counters.cb):
        return {"작업 집합": counters.WorkingSetSize, "커밋": counters.PrivateUsage}
    return {}


def _read_linux() -> dict[str, int]:
    with open("/proc/self/statm", encoding="ascii") as f:
        resident_pages = int(f.read().split()[1])
    return {"RSS": resident_pages * os.sysconf("SC_PAGE_SIZE")}


class _MachBasicInfo(ctypes.Structure):
    """mach_task_basic_info."""

    _fields_ = [
        ("virtual_size", ctypes.c_uint64),
        ("resident_size", ctypes.c_uint64),
        ("resident_size_max", ctypes.c_uint64),
        ("user_time", ctypes.c_int32 * 2),
        ("system_time", ctypes.c_int32 * 2),
        ("policy", ctypes.c_int32),
        ("suspend_count", ctypes.c_int32),
    ]


@functools.cache
def _macos_api() -> ctypes.CDLL:
    """macOS의 libSystem — task_info · mach_task_self의 형을 맞춰 한 번만 연다."""
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    libc.mach_task_self.restype = ctypes.c_uint32
    libc.task_info.argtypes = [
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    libc.task_info.restype = ctypes.c_int
    return libc


def _read_macos() -> dict[str, int]:
    mach_task_basic_info = 20  # task_info의 flavor 번호
    libc = _macos_api()
    info = _MachBasicInfo()
    count = ctypes.c_uint32(ctypes.sizeof(_MachBasicInfo) // ctypes.sizeof(ctypes.c_int32))
    if libc.task_info(
        libc.mach_task_self(), mach_task_basic_info, ctypes.byref(info), ctypes.byref(count)
    ):
        return {}
    return {"RSS": info.resident_size}
