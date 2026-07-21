"""config.log_setup 공통 로깅 설정 단위 테스트 (#30)."""

import logging
import logging.handlers

import pytest

import config.config
import config.log_setup as log_setup


@pytest.fixture
def isolated_logging(monkeypatch, tmp_path):
    """로그 저장 위치를 임시 경로로 돌리고, 테스트 후 루트 로거 상태를 복원한다."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level

    monkeypatch.setattr(config.config, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(log_setup, "_configured", False)
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    yield tmp_path

    # setup_logging이 추가한 핸들러를 닫고 원래 핸들러·레벨을 복원한다
    for handler in root.handlers[:]:
        handler.close()
        root.removeHandler(handler)
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)


def test_setup_adds_console_and_rotating_file_handlers(isolated_logging):
    """루트 로거에 콘솔 핸들러와 회전 파일 핸들러가 각각 1개씩 연결된다."""
    # pytest 로깅 플러그인이 끼워 넣는 캡처 핸들러를 제외하기 위해 전후 차집합으로 비교한다
    before = set(logging.getLogger().handlers)

    log_setup.setup_logging()

    added = [h for h in logging.getLogger().handlers if h not in before]
    rotating = [
        h for h in added if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    consoles = [
        h
        for h in added
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
    ]
    assert len(rotating) == 1
    assert len(consoles) == 1


def test_log_message_is_written_to_logs_file(isolated_logging):
    """모듈 로거로 남긴 메시지가 logs/app.log 파일에 기록된다."""
    log_setup.setup_logging()

    logging.getLogger("smoke.test").info("hello log file")

    log_file = isolated_logging / "logs" / log_setup.LOG_FILE_NAME
    assert log_file.exists()
    assert "hello log file" in log_file.read_text(encoding="utf-8")


def test_setup_logging_is_idempotent(isolated_logging):
    """setup_logging을 여러 번 호출해도 핸들러가 중복 추가되지 않는다."""
    log_setup.setup_logging()
    handler_count = len(logging.getLogger().handlers)

    log_setup.setup_logging()

    assert len(logging.getLogger().handlers) == handler_count
