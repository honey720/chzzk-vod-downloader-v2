"""앱 프로세스의 메모리를 읽어 로그로 남기는 것 (#309) — ``app/process_memory.py``."""

import logging
import sys

import app.process_memory as process_memory
from app.process_memory import log_process_memory, read_process_memory

MB = 1024 * 1024


def test_reading_returns_positive_sizes_on_this_platform():
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


def test_reading_follows_memory_the_process_takes():
    """프로세스가 메모리를 더 쥐면 읽은 값이 그만큼 늘어야 한다.

    64 MiB를 0이 아닌 값으로 채워 쥠 -> 읽은 값(가장 큰 것) 증가 >= 48 MiB.
    읽지 못하는 OS에서는 재지 않는다
    """
    before = read_process_memory()
    if not before:
        return
    held = bytearray(b"") * (64 * MB)

    after = read_process_memory()

    assert max(after.values()) - max(before.values()) >= 48 * MB
    assert len(held) == 64 * MB  # 재는 동안 쥐고 있다


def test_log_line_names_the_point_and_each_size_in_megabytes(monkeypatch, caplog):
    """메모리 줄은 "프로세스 메모리 [시점] 이름 값MB · …" 형식이고 로거 이름은 app.process_memory여야 한다.

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
    assert record.name == "app.process_memory"
    assert (
        record.getMessage() == "프로세스 메모리 [조회 끝] 개인 작업 집합 812.4MB · 커밋 1,536.0MB"
    )


def test_nothing_is_logged_when_memory_cannot_be_read(monkeypatch, caplog):
    """메모리를 읽지 못하면 줄을 남기지 않고 예외도 내지 않아야 한다.

    읽기가 빈 dict를 돌려줄 때와 예외를 낼 때 -> 메모리 줄 0건, 표식 줄은 잡힌다
    """

    def broken():
        raise OSError("읽기 실패(대역)")

    caplog.set_level(logging.INFO)
    logging.getLogger("app.process_memory").info("표식")  # 캡처가 살아 있음을 먼저 본다

    monkeypatch.setattr(process_memory, "read_process_memory", lambda: {})
    log_process_memory("다운로드 시작")
    monkeypatch.setattr(process_memory, "read_process_memory", broken)
    log_process_memory("다운로드 시작")

    assert [record.getMessage() for record in caplog.records] == ["표식"]


def test_a_failing_platform_reader_yields_nothing(monkeypatch):
    """OS별 읽기가 예외를 내면 read_process_memory는 빈 dict를 돌려줘야 한다.

    세 OS의 읽기를 모두 예외를 내게 바꿈 -> {}
    """

    def broken():
        raise OSError("읽기 실패(대역)")

    for name in ("_read_windows", "_read_linux", "_read_macos"):
        monkeypatch.setattr(process_memory, name, broken)

    assert read_process_memory() == {}
