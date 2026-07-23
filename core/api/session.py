"""requests.Session 관리 모듈 (#62, 원본 #31).

공유 세션과 스레드로컬 세션 헬퍼는 Qt와 무관한 인프라 코드이므로 core로 이주했다.
쿠키 저장 차단 정책과 스레드로컬 방식은 #35에서 검증된 동작을 그대로 유지한다.
"""

import threading
from http.cookiejar import DefaultCookiePolicy

import requests


def _make_session() -> requests.Session:
    """연결 재사용용 Session을 만든다 (#31).

    기존에는 매 호출 requests.get()이 독립적이라 응답의 Set-Cookie가 다음 요청으로
    이어지지 않았다. 이 이슈는 연결 재사용만 도입하므로, 응답 쿠키 저장을 차단해
    쿠키 동작을 기존과 동일하게 유지한다. 요청별 cookies= 인자는 그대로 전송된다.
    """
    session = requests.Session()
    session.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
    return session


# API 호출용 모듈 수준 공유 세션.
# 응답 쿠키를 저장하지 않으므로 여러 스레드에서 호출해도 상태 공유 문제가 없다.
_session = _make_session()

# 다운로드 워커용 스레드로컬 세션 저장소
_thread_local = threading.local()


def get_thread_session() -> requests.Session:
    """호출한 스레드 전용 Session을 반환한다 (없으면 생성).

    requests.Session은 스레드 간 완전한 안전이 보장되지 않으므로, 다운로드 워커처럼
    동시 요청이 많은 경로는 스레드마다 별도 Session을 사용해 연결 재사용만 취한다.
    """
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = _make_session()
        _thread_local.session = session
    return session
