"""공통 logging 설정 진입점 — 구현은 core/utils/logging_setup.py로 이주 (#62, 원본 #30).

로그 저장 위치(CONFIG_DIR/logs)는 app 영역 지식이므로 core에 두지 않고 여기서 주입한다.
기존 호출부(main.py, scripts/headless_download.py)의 import 경로와 시그니처는 그대로다.
"""

import logging
import os

import config.config as config
from core.utils import logging_setup as _logging_setup
from core.utils.logging_setup import (  # noqa: F401 — 하위 호환 re-export
    BACKUP_COUNT,
    LOG_FILE_NAME,
    LOG_FORMAT,
    MAX_BYTES,
)


def setup_logging(log_level: int = logging.DEBUG) -> None:
    """루트 로거 설정을 core 구현에 위임한다.

    로그 파일 위치는 기존 규칙(CONFIG_DIR/logs)을 그대로 따른다.

    Args:
        log_level: 루트 로거에 적용할 로깅 레벨 (기본값: DEBUG)
    """
    _logging_setup.setup_logging(os.path.join(config.CONFIG_DIR, "logs"), log_level)
