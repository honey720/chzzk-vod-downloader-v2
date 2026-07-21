"""공통 logging 설정 모듈 (#30).

앱 전역에서 사용할 표준 logging 핸들러(콘솔 + 회전 파일)를 루트 로거에 연결한다.
각 모듈은 `logging.getLogger(__name__)`로 로거를 얻어 쓰기만 하면 되고,
핸들러·포맷 구성은 이 모듈에서만 관리한다.
"""

import logging
import logging.handlers
import os

import config.config as config

# 포맷: 시간·레벨·모듈·메시지
LOG_FORMAT = "%(asctime)s - %(levelname)s - [%(name)s] - %(message)s"
LOG_FILE_NAME = "app.log"

# 파일 회전 설정: 1MB × 5개 백업
MAX_BYTES = 1024 * 1024
BACKUP_COUNT = 5

# 중복 초기화 방지 플래그
_configured = False


def setup_logging(log_level: int = logging.DEBUG) -> None:
    """루트 로거에 콘솔·회전 파일 핸들러를 설정한다.

    앱 시작 시 한 번만 호출한다. 재호출은 무시된다(핸들러 중복 방지).

    Args:
        log_level: 루트 로거에 적용할 로깅 레벨 (기본값: DEBUG)
    """
    global _configured
    if _configured:
        return

    # 로그 파일 위치는 기존 규칙(CONFIG_DIR/logs)을 그대로 따른다
    log_dir = os.path.join(config.CONFIG_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)

    formatter = logging.Formatter(LOG_FORMAT)

    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, LOG_FILE_NAME),
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    # 서드파티 라이브러리의 과도한 DEBUG 출력은 억제한다
    # (다운로드 시 요청 1건마다 urllib3 로그가 쌓여 로그가 불어나는 것 방지)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    _configured = True
