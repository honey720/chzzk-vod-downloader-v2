"""core.api.session의 Session 관리(연결 재사용·쿠키 차단) 단위 테스트 (#62, 원본 #31).

tests/unit/test_network_session.py에서 이주했다. 검증 내용·기대 동작 무변경.
"""

import threading

import requests

import content.network as network
import core.api.session as session


def test_module_session_is_a_requests_session():
    """API 호출용 모듈 수준 공유 세션이 requests.Session이어야 한다."""
    assert isinstance(session._session, requests.Session)


def test_thread_session_is_reused_within_same_thread():
    """같은 스레드에서 get_thread_session은 항상 같은 Session을 돌려준다."""
    first = session.get_thread_session()
    second = session.get_thread_session()

    assert isinstance(first, requests.Session)
    assert first is second


def test_thread_session_is_distinct_across_threads():
    """다른 스레드에서는 별도의 Session이 생성된다 (스레드 안전성 확보 방식)."""
    main_session = session.get_thread_session()
    other_sessions = []

    def worker():
        other_sessions.append(session.get_thread_session())

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert len(other_sessions) == 1
    assert isinstance(other_sessions[0], requests.Session)
    assert other_sessions[0] is not main_session


def test_session_does_not_store_response_cookies():
    """응답의 Set-Cookie가 세션에 저장되지 않아야 한다 (기존 동작 유지).

    쿠키 차단 정책이 적용된 세션은 어떤 도메인의 쿠키도 저장(set_ok)하지 않는다.
    """
    from http.cookiejar import Cookie
    from urllib.request import Request

    policy = session._make_session().cookies.get_policy()

    cookie = Cookie(
        version=0,
        name="NID_AUT",
        value="dummy",
        port=None,
        port_specified=False,
        domain="api.chzzk.naver.com",
        domain_specified=False,
        domain_initial_dot=False,
        path="/",
        path_specified=True,
        secure=False,
        expires=None,
        discard=True,
        comment=None,
        comment_url=None,
        rest={},
    )
    request = Request("https://api.chzzk.naver.com/service")

    assert policy.set_ok(cookie, request) is False


def test_content_network_reexports_core_session():
    """content.network의 하위 호환 re-export가 core 구현과 동일 객체여야 한다 (#62).

    기존 테스트·호출부는 network._session 객체의 get을 monkeypatch하므로,
    두 모듈이 같은 세션 객체를 공유해야 목킹·동작이 기존과 동일하게 유지된다.
    """
    assert network._session is session._session
    assert network.get_thread_session is session.get_thread_session
    assert network._make_session is session._make_session
