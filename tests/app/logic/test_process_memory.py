"""앱 프로세스의 메모리를 읽어 로그로 남기는 것 (#309) — ``app/system/process_memory.py``."""

import logging
import sys

import app.system.process_memory as process_memory
from app.system.process_memory import log_process_memory, read_process_memory
from typing import Never

MB = 1024 * 1024


def test_reading_returns_positive_sizes_on_this_platform() -> None:
    """지금 도는 OS에서 프로세스 메모리를 읽으면 이름마다 양수 바이트가 나와야 한다.

    Windows는 "커밋"과 ("개인 작업 집합" 또는 "작업 집합"), Linux는 "RSS".
    macOS의 읽기는 로컬에서 돌려 보지 못했다 — 읽히면 "RSS"가 양수이고, 안 읽히면 빈 dict다
    """
    values = read_process_memory()

    if sys.platform == "win32":
        assert "커밋" in values
        assert {"개인 작업 집합", "작업 집합"} & set(values)
    elif sys.platform == "darwin":
        assert set(values) <= {"RSS"}
    else:
        assert set(values) == {"RSS"}
    assert all(isinstance(size, int) and size > 0 for size in values.values())


def _return_freed_heap_to_the_system() -> None:
    """macOS에서 malloc이 풀어 둔 채 들고 있는 페이지를 시스템에 돌려준다 — 다른 OS에서는 하지 않는다.

    macOS의 RSS(``resident_size``)에는 malloc이 free한 뒤 「다시 쓸 수 있음」으로만 표시해 둔 페이지가
    남는다. 메모리가 빠듯하면 커널이 새 페이지를 줄 때 그것을 거둬 가 RSS가 그만큼 준다 — 쥐는 사이 다른
    페이지가 빠져 늘어난 몫이 작게 보인다(CI macOS 256.9 → 305.0MB, 64 MiB를 쥐고 +45.9MiB). 기준을 재기
    전에 돌려줘 두면 재는 동안 거둬 갈 것이 덜 남는다
    """
    if sys.platform != "darwin":
        return
    import ctypes

    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    libc.malloc_zone_pressure_relief.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    libc.malloc_zone_pressure_relief.restype = ctypes.c_size_t
    libc.malloc_zone_pressure_relief(None, 0)  # 모든 zone · 돌려줄 수 있는 만큼


def test_reading_follows_memory_the_process_takes() -> None:
    """프로세스가 메모리를 더 쥐면 읽은 값이 그만큼 늘어야 한다.

    (macOS: 풀어 둔 heap 페이지를 먼저 돌려줌) 128 MiB를 0이 아닌 값으로 채워 쥠
    -> 읽은 값(가장 큰 것) 증가 >= 96 MiB. 쥔 양의 3/4로 앞 판(64 · 48)과 같은 비율이고, 다른 페이지가
    흔들려도 되는 몫만 16에서 32 MiB로 늘었다. 읽지 못하는 OS에서는 재지 않는다
    """
    _return_freed_heap_to_the_system()
    before = read_process_memory()
    if not before:
        return
    held = bytearray(b"") * (128 * MB)

    after = read_process_memory()

    assert max(after.values()) - max(before.values()) >= 96 * MB
    assert len(held) == 128 * MB  # 재는 동안 쥐고 있다


def test_log_line_names_the_point_and_each_size_in_megabytes(monkeypatch, caplog) -> None:
    """메모리 줄은 "프로세스 메모리 [시점] 이름 값MB · …" 형식이고 로거 이름은 app.system.process_memory여야 한다.

    읽기를 {개인 작업 집합: 812.4 MiB, 커밋: 1536 MiB}로 고정, 시점 "조회 끝"
    -> "프로세스 메모리 [조회 끝] 개인 작업 집합 812.4MB · 커밋 1,536.0MB"
    """
    monkeypatch.setattr(
        process_memory,
        "read_process_memory",
        lambda: {"개인 작업 집합": int(812.4 * MB), "커밋": 1536 * MB},
    )
    caplog.set_level(logging.INFO)

    log_process_memory("조회 끝")

    (record,) = caplog.records
    assert record.name == "app.system.process_memory"
    assert (
        record.getMessage() == "프로세스 메모리 [조회 끝] 개인 작업 집합 812.4MB · 커밋 1,536.0MB"
    )


def test_nothing_is_logged_when_memory_cannot_be_read(monkeypatch, caplog) -> None:
    """메모리를 읽지 못하면 줄을 남기지 않고 예외도 내지 않아야 한다.

    읽기가 빈 dict를 돌려줄 때와 예외를 낼 때 -> 메모리 줄 0건, 표식 줄은 잡힌다
    """

    def broken() -> Never:
        raise OSError("읽기 실패(대역)")

    caplog.set_level(logging.INFO)
    logging.getLogger("app.system.process_memory").info("표식")  # 캡처가 살아 있음을 먼저 본다

    monkeypatch.setattr(process_memory, "read_process_memory", lambda: {})
    log_process_memory("다운로드 시작")
    monkeypatch.setattr(process_memory, "read_process_memory", broken)
    log_process_memory("다운로드 시작")

    assert [record.getMessage() for record in caplog.records] == ["표식"]


def test_a_failing_platform_reader_yields_nothing(monkeypatch) -> None:
    """OS별 읽기가 예외를 내면 read_process_memory는 빈 dict를 돌려줘야 한다.

    세 OS의 읽기를 모두 예외를 내게 바꿈 -> {}
    """

    def broken() -> Never:
        raise OSError("읽기 실패(대역)")

    for name in ("_read_windows", "_read_linux", "_read_macos"):
        monkeypatch.setattr(process_memory, name, broken)

    assert read_process_memory() == {}
