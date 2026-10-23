"""충돌 진단 — 충돌 순간의 스택과 처리되지 않은 예외를 로그 폴더에 남긴다 (#427).

앱이 스스로 꺼졌다는 제보를 가를 자료를 남긴다.

- 네이티브 충돌(접근 위반 등): ``faulthandler``가 로그 폴더의 ``crash.log``에 모든 스레드의
  스택을 쓴다. 신호 처리기 안에서 쓰므로 로그 포매터를 거치지 않는다 — 다음 시작 때 그
  내용을 가려(홈 폴더 · 주소의 토큰) 앱 로그로 옮기고 파일을 비운다. 파일에는 마지막
  실행의 것만 남는다.
- 처리되지 않은 예외(메인 스레드 · 다른 스레드): ``sys.excepthook`` · ``threading.excepthook``이
  앱 로그에 남긴다 — 로그 포매터의 가리기를 거친다. Qt 이벤트 처리 중(시그널 · 슬롯 · 오버라이드한
  가상 메서드) 난 예외도 PySide6가 ``sys.excepthook``으로 넘긴다.

릴리즈 빌드는 콘솔이 없어 표준 오류가 없을 수 있다(``sys.stderr``가 None) — 충돌 기록은 늘
파일로 연다.
"""

import faulthandler
import logging
import os
import sys
import threading
from types import TracebackType

from core.utils.logging_setup import redact_home, redact_urls

logger = logging.getLogger(__name__)

CRASH_FILE_NAME = "crash.log"  # 로그 폴더 안의 충돌 기록 파일 — 실행마다 비운다

# 앞 실행의 충돌 기록에서 앱 로그로 옮길 최대 글자 수 — faulthandler의 덤프는 스레드 100개 ·
# 스레드마다 프레임 100개까지라 이보다 짧다. 다른 것이 파일에 쓰였을 때 앱 로그가 불어나지 않게
_CARRIED_CHARS = 64 * 1024

# 충돌 기록 파일 — faulthandler가 파일 디스크립터를 기억하므로 프로세스가 끝날 때까지 붙잡아
# 둔다. 닫히거나 디스크립터가 다른 파일로 다시 쓰이면 엉뚱한 곳에 쓴다
_crash_file = None


def install_crash_diagnostics(log_dir: str) -> None:
    """충돌 진단을 켠다 — 앞 실행의 충돌 기록을 앱 로그로 옮기고, faulthandler와 예외 훅을 건다.

    앱 시작 때 앱 로그 설정(``setup_logging``) 뒤에 한 번 부른다. 다시 부르면 충돌 기록 파일을
    새로 열어 그 파일로 옮긴다.

    Args:
        log_dir: 충돌 기록 파일을 둘 로그 폴더 (없으면 만든다)
    """
    global _crash_file
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, CRASH_FILE_NAME)
    carry_over_previous_crash(path)
    previous = _crash_file
    # 비우고 연다 — 정상으로 끝난 실행은 빈 파일을 남긴다
    _crash_file = open(path, "w", encoding="ascii", errors="replace")
    faulthandler.enable(file=_crash_file, all_threads=True)
    if previous is not None:
        previous.close()
    install_exception_hooks()


def carry_over_previous_crash(path: str) -> bool:
    """앞 실행의 충돌 기록이 비어 있지 않으면 가려서 앱 로그에 남긴다.

    faulthandler가 쓴 원본에는 소스 · 앱이 놓인 경로가 그대로 들어 있다 — 사용자 이름이 든 홈
    폴더가 찍힐 수 있다. 앱 로그의 다른 줄과 같이 홈 폴더를 ``~``로, 주소의 토큰 값을 가린다.
    파일은 지우지 않는다 — 이어서 ``install_crash_diagnostics``가 비우고 연다.

    Returns:
        옮긴 것이 있으면 True
    """
    try:
        with open(path, encoding="ascii", errors="replace") as f:
            dump = f.read(_CARRIED_CHARS)
    except FileNotFoundError:
        return False
    except OSError:
        logger.warning("Could not read the previous crash record", exc_info=True)
        return False
    if not dump.strip():
        return False
    logger.error(
        "Previous run ended abnormally — crash record:\n%s",
        redact_home(redact_urls(dump.rstrip())),
    )
    return True


def install_exception_hooks() -> None:
    """처리되지 않은 예외를 앱 로그에 남기는 훅을 건다 — 메인 스레드와 다른 스레드.

    앞의 훅(기본은 표준 오류에 찍기)도 그대로 부른다 — 소스 실행의 콘솔 출력은 바뀌지 않는다.
    """
    previous_hook = sys.excepthook
    previous_thread_hook = threading.excepthook

    def log_unhandled(
        kind: type[BaseException], value: BaseException, traceback: TracebackType | None
    ) -> None:
        if not issubclass(kind, KeyboardInterrupt):
            logger.critical("Unhandled exception", exc_info=(kind, value, traceback))
        previous_hook(kind, value, traceback)

    def log_unhandled_in_thread(args: threading.ExceptHookArgs) -> None:
        if not issubclass(args.exc_type, SystemExit):  # 스레드의 SystemExit는 기본 훅도 조용하다
            name = args.thread.name if args.thread is not None else "?"
            logger.critical(
                "Unhandled exception in thread %s",
                name,
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
        previous_thread_hook(args)

    sys.excepthook = log_unhandled
    threading.excepthook = log_unhandled_in_thread
