"""공통 logging 설정 모듈 (#62, 원본 #30).

앱 전역에서 사용할 표준 logging 핸들러(콘솔 + 회전 파일)를 루트 로거에 연결한다.
각 모듈은 `logging.getLogger(__name__)`로 로거를 얻어 쓰기만 하면 되고,
핸들러·포맷 구성은 이 모듈에서만 관리한다.

로그 저장 위치(CONFIG_DIR/logs)는 app 영역 지식이므로 core는 경로를 정하지 않고
log_dir 인자로 주입받는다 — 기존 진입점인 app/log_setup.py가 주입한다.
"""

import logging
import logging.handlers
import os
import re

# 포맷: 시간·레벨·모듈·메시지
LOG_FORMAT = "%(asctime)s - %(levelname)s - [%(name)s] - %(message)s"
REDACTED = "***"  # 로그에서 가린 값 자리에 남기는 글자

# 주소로 읽는 토큰 — 스킴이 붙은 주소, 그리고 requests · urllib3 예외가 "url: " 뒤에 적는 경로
# (연결 실패는 스킴 없이 "/경로?쿼리"만 적는다). 따옴표 · 괄호 · 공백에서 끝난다 (#328)
_URL_TOKEN = re.compile(r"(?:[A-Za-z][A-Za-z0-9+.-]*://|(?<=url: )|(?<=URL: ))[^\s'\"<>()]+")
_TRAILING_PUNCTUATION = ".,:;!"  # 주소 끝에 붙어 온 문장 부호 — 주소가 아니라 글의 것이다
# 주소로 읽지 못한 토큰에 붙은 "?이름=값" 쿼리 — 플레이리스트의 상대 주소 등 (#328)
_BARE_QUERY = re.compile(r"(?<=[^\s?])\?([^\s'\"<>()#]*=[^\s'\"<>()#]*)")


def _redact_query(query: str) -> str:
    """쿼리 문자열의 값을 가린다 — 이름은 남긴다. 이름 없는 조각은 통째로 가린다."""
    parts = []
    for part in re.split(r"([&;])", query):
        if part in ("&", ";", ""):
            parts.append(part)
        elif "=" in part:
            parts.append(part.split("=", 1)[0] + "=" + REDACTED)
        else:
            parts.append(REDACTED)
    return "".join(parts)


def _redact_url(url: str) -> str:
    """주소 하나의 서명 · 토큰 값을 가린다 — 스킴 · 호스트 · 경로의 나머지는 남긴다.

    경로 조각 가운데 ``이름=값`` 꼴(CDN의 경로 인증 토큰, 예: ``hdntl=…``)은 값을 가리고, 쿼리는
    값마다 가린다. 조각(#) 뒤는 그대로 둔다.
    """
    head, hash_mark, fragment = url.partition("#")
    path, question, query = head.partition("?")
    scheme, sep, rest = path.partition("://")
    if not sep:
        scheme, rest = "", path
    segments = [
        segment.split("=", 1)[0] + "=" + REDACTED if "=" in segment else segment
        for segment in rest.split("/")
    ]
    redacted = scheme + sep + "/".join(segments)
    if question:
        redacted += "?" + _redact_query(query)
    return redacted + hash_mark + fragment


def _redact_url_match(match: re.Match) -> str:
    """찾은 주소 토큰을 가린다 — 끝에 붙어 온 문장 부호는 주소에서 떼어 그대로 둔다."""
    token = match.group(0)
    url = token.rstrip(_TRAILING_PUNCTUATION)
    return _redact_url(url) + token[len(url) :]


def _redact_bare_query_match(match: re.Match) -> str:
    """찾은 ``?쿼리``를 가린다 — 끝에 붙어 온 문장 부호는 쿼리에서 떼어 그대로 둔다."""
    token = match.group(1)
    query = token.rstrip(_TRAILING_PUNCTUATION)
    return "?" + _redact_query(query) + token[len(query) :]


def redact_urls(text: str) -> str:
    """글 속 주소의 서명 · 토큰 값을 가린다 (#328).

    다운로드 로그와 앱 로그의 모든 줄이 이 함수를 거친다(``RedactingFormatter``) — 예외 문자열과
    트레이스백도 같다. requests의 예외 글에는 요청 주소가 쿼리까지 들어 있다. 호스트와 경로는
    남겨 어느 요청이었는지는 읽을 수 있게 한다.
    """
    text = _URL_TOKEN.sub(_redact_url_match, text)
    return _BARE_QUERY.sub(_redact_bare_query_match, text)


class RedactingFormatter(logging.Formatter):
    """완성된 로그 줄(메시지 · 예외 · 스택)에서 주소의 서명 · 토큰 값을 가리는 포매터 (#328).

    줄을 적는 곳마다 가리지 않고 핸들러의 포매터 한 곳에서 가린다 — 새로 생기는 로그 줄에도
    그대로 적용된다. 가리는 것은 주소의 값뿐이라 다른 줄의 형식은 바뀌지 않는다.
    """

    def format(self, record: logging.LogRecord) -> str:
        return redact_urls(super().format(record))


LOG_FILE_NAME = "app.log"

# 파일 회전 설정: 1MB × 5개 백업
MAX_BYTES = 1024 * 1024
BACKUP_COUNT = 5

# 중복 초기화 방지 플래그
_configured = False


def setup_logging(log_dir: str, log_level: int = logging.DEBUG) -> None:
    """루트 로거에 콘솔·회전 파일 핸들러를 설정한다.

    앱 시작 시 한 번만 호출한다. 재호출은 무시된다(핸들러 중복 방지).

    Args:
        log_dir: 로그 파일을 저장할 디렉토리 (없으면 생성)
        log_level: 루트 로거에 적용할 로깅 레벨 (기본값: DEBUG)
    """
    global _configured
    if _configured:
        return

    os.makedirs(log_dir, exist_ok=True)

    formatter = RedactingFormatter(LOG_FORMAT)

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
    # charset_normalizer도 응답 본문 인코딩 추정 때마다 DEBUG를 쏟는다 (#148)
    logging.getLogger("charset_normalizer").setLevel(logging.WARNING)

    _configured = True
