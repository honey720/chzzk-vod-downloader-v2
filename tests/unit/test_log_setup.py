"""config.log_setup 진입점(로그 위치 주입) 단위 테스트 (#62, 원본 #30).

핸들러 구성 자체의 테스트는 tests/unit/core/test_logging_setup.py로 이주했다.
여기서는 기존 시그니처 유지와 로그 위치(CONFIG_DIR/logs) 주입만 검증한다.
"""

import logging

import pytest

import config.config
import config.log_setup as log_setup
import core.utils.logging_setup as core_logging_setup


@pytest.fixture
def isolated_logging(monkeypatch, tmp_path):
    """로그 저장 위치를 임시 경로로 돌리고, 테스트 후 루트 로거 상태를 복원한다."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level

    monkeypatch.setattr(config.config, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(core_logging_setup, "_configured", False)
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


def test_setup_logging_writes_to_config_dir_logs(isolated_logging):
    """기존 진입점 호출 시 CONFIG_DIR/logs/app.log에 기록된다 (동작 무변경)."""
    log_setup.setup_logging()

    logging.getLogger("smoke.test").info("hello via entrypoint")

    log_file = isolated_logging / "logs" / core_logging_setup.LOG_FILE_NAME
    assert log_file.exists()
    assert "hello via entrypoint" in log_file.read_text(encoding="utf-8")
