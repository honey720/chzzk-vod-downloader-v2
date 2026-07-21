"""content.network의 Session 도입(연결 재사용) 단위 테스트 (#31)."""

import threading

import requests

import content.network as network


def test_module_session_is_a_requests_session():
    """API 호출용 모듈 수준 공유 세션이 requests.Session이어야 한다."""
    assert isinstance(network._session, requests.Session)


def test_thread_session_is_reused_within_same_thread():
    """같은 스레드에서 get_thread_session은 항상 같은 Session을 돌려준다."""
    first = network.get_thread_session()
    second = network.get_thread_session()

    assert isinstance(first, requests.Session)
    assert first is second


def test_thread_session_is_distinct_across_threads():
    """다른 스레드에서는 별도의 Session이 생성된다 (스레드 안전성 확보 방식)."""
    main_session = network.get_thread_session()
    other_sessions = []

    def worker():
        other_sessions.append(network.get_thread_session())

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

    policy = network._make_session().cookies.get_policy()

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
