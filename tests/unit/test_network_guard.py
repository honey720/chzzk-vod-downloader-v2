"""테스트 네트워크 가드(#275) 자체가 작동하는지 잰다 — 일부러 시도하면 막히고, 기록되고, 꺼내진다.

실제 외부로는 나가지 않는다: 가드가 소켓 연결·requests 어댑터 전송을 그 자리에서
예외로 끊고, 대상 호스트도 예약된 무효 도메인(`.invalid`)이다. 기록을 `take()`로
꺼내는 것은 이 테스트가 자기 시도를 정리하기 위해서다 — 꺼내지 않으면 테스트 끝
단언이 이 테스트를 실패시킨다(그것이 가드의 정상 동작이다).
"""

import socket
import threading

import pytest
import requests

BLOCKED = RuntimeError  # 가드의 NetworkAttemptBlocked는 RuntimeError 하위 — conftest 모듈 객체 이중 로드를 피해 기반 타입과 메시지로 잡는다


def test_requests_send_is_blocked_and_recorded(network_guard, request):
    with pytest.raises(BLOCKED, match="네트워크 가드"):
        requests.get("http://example.invalid/", timeout=1)

    assert network_guard.take() == [(request.node.nodeid, "GET http://example.invalid/")]


def test_socket_connect_is_blocked_and_recorded(network_guard, request):
    with pytest.raises(BLOCKED, match="네트워크 가드"):
        socket.create_connection(("example.invalid", 80), timeout=1)
    with pytest.raises(BLOCKED, match="네트워크 가드"):
        socket.socket().connect(("example.invalid", 443))

    assert network_guard.take() == [
        (request.node.nodeid, "socket example.invalid:80"),
        (request.node.nodeid, "socket example.invalid:443"),
    ]


def test_attempt_swallowed_in_a_worker_thread_is_still_recorded(network_guard, request):
    """예외를 삼키는 스레드 — 실측에서 테스트를 초록으로 남긴 바로 그 형태 — 도 기록에 남는다."""

    def swallow():
        try:
            requests.get("http://example.invalid/thumbnail.png", timeout=1)
        except Exception:
            pass  # 카드의 썸네일·크기 조회 스레드가 하는 일

    worker = threading.Thread(target=swallow)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()

    assert network_guard.take() == [
        (request.node.nodeid, "GET http://example.invalid/thumbnail.png")
    ]
