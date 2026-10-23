"""NetworkManager 조회 요청의 타임아웃 검증 (#129).

타임아웃이 없으면 네트워크 단절 시 OS TCP 타임아웃(~47초+)까지 조회가 갇힌다.
모든 조회 호출이 REQUEST_TIMEOUT(connect·read)을 싣는지 가짜 세션으로 확인한다.
다운로드 경로(세그먼트·파트)는 기존 timeout=30이 이미 있어 이 테스트 범위 밖.
"""

import json

import pytest
import requests

import app.network as network
from app.network import NetworkManager, REQUEST_TIMEOUT
from tests.mocks.mock_http import MockResponse

COOKIES = {"NID_AUT": "REDACTED", "NID_SES": "REDACTED"}


class RecordingSession:
    """호출 kwargs를 기록하고 준비된 응답을 돌려주는 가짜 세션."""

    def __init__(self, response):
        self._response = response
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._response


class BytesResponse(MockResponse):
    """content 바이트가 필요한 호출(get_aes_key)용 응답."""

    def __init__(self, content: bytes = b"key", status_code: int = 200):
        super().__init__(status_code=status_code)
        self.content = content


def _install(monkeypatch, response):
    session = RecordingSession(response)
    monkeypatch.setattr(network, "_session", session)
    return session


M3U8_PLAYLIST = "\n".join(
    [
        "#EXTM3U",
        '#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=1920x1080',
        "1080/playlist.m3u8",
    ]
)


@pytest.mark.parametrize(
    ("name", "call", "response"),
    [
        (
            "get_video_info",
            lambda: NetworkManager.get_video_info("123", COOKIES),
            MockResponse(text=json.dumps({"content": {}})),
        ),
        (
            "get_clip_info",
            lambda: NetworkManager.get_clip_info("clipUID", COOKIES),
            MockResponse(text=json.dumps({"content": {}})),
        ),
        (
            "get_clip_manifest",
            lambda: NetworkManager.get_clip_manifest("clip-id", COOKIES),
            MockResponse(
                text=json.dumps({"card": {"content": {"error": {"errorCode": "X"}}}})
            ),
        ),
        (
            "get_video_m3u8_base_url",
            lambda: NetworkManager.get_video_m3u8_base_url(
                json.dumps({"media": [{"path": "https://example.invalid/master.m3u8"}]}),
                1080,
                COOKIES,
            ),
            MockResponse(text=M3U8_PLAYLIST),
        ),
    ],
)
def test_metadata_calls_carry_request_timeout(monkeypatch, name, call, response):
    """조회 호출은 모두 REQUEST_TIMEOUT을 실어야 한다."""
    session = _install(monkeypatch, response)

    call()

    assert session.calls, name
    for _url, kwargs in session.calls:
        assert kwargs.get("timeout") == REQUEST_TIMEOUT, name


@pytest.mark.parametrize("method", ["get_video_dash_manifest", "get_video_sea_manifest"])
def test_manifest_calls_carry_request_timeout(monkeypatch, load_mock_response, method):
    """매니페스트 조회도 REQUEST_TIMEOUT을 실어야 한다 (실픽스처 XML 파싱 통과)."""
    xml = load_mock_response("dash_manifest.xml")
    session = _install(monkeypatch, MockResponse(text=xml))

    getattr(NetworkManager, method)("video-id", "in-key", COOKIES)

    assert session.calls
    for _url, kwargs in session.calls:
        assert kwargs.get("timeout") == REQUEST_TIMEOUT


def test_aes_key_keeps_dedicated_timeout(monkeypatch):
    """복호화 키 요청의 기존 timeout=30은 유지된다 (#57 경로, 이 이슈 범위 밖)."""
    session = _install(monkeypatch, BytesResponse())

    NetworkManager.get_aes_key("https://api.chzzk.naver.com/service/v1/encryption/videos/VID/aes_key", COOKIES)

    assert session.calls
    _url, kwargs = session.calls[0]
    assert kwargs.get("timeout") == 30


def test_request_timeout_is_connect_read_pair():
    """connect·read를 모두 지정한다 — 단일 값이면 connect만 걸리는 실수 방지."""
    assert isinstance(REQUEST_TIMEOUT, tuple)
    assert len(REQUEST_TIMEOUT) == 2
    connect, read = REQUEST_TIMEOUT
    assert 0 < connect <= read


# ================================================================ 제한 시간에 걸리면 한 번 더 (#331)

VIDEO_URL = "https://chzzk.naver.com/video/123"


class ScriptedSession:
    """정해 둔 순서대로 예외를 던지거나 응답을 돌려주는 가짜 세션 — 받은 요청 수를 센다."""

    def __init__(self, *outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _video_info_response(title: str = "다시 받은 제목") -> MockResponse:
    return MockResponse(text=json.dumps({"content": {"videoTitle": title}}))


@pytest.mark.parametrize(
    "timeout",
    [requests.ReadTimeout("read"), requests.ConnectTimeout("connect")],
    ids=["read", "connect"],
)
def test_video_info_is_requested_again_after_a_timeout(monkeypatch, timeout):
    """영상 정보 요청이 제한 시간(읽기 · 연결)에 걸리면 한 번 더 보내 그 응답을 써야 한다.

    첫 요청은 ReadTimeout(또는 ConnectTimeout), 둘째 요청은 제목 "다시 받은 제목"인 응답
    -> 요청 2번, 돌려준 정보의 제목 == "다시 받은 제목"
    """
    session = ScriptedSession(timeout, _video_info_response())
    monkeypatch.setattr(network, "_session", session)

    info = NetworkManager.get_video_info("123", COOKIES)

    assert session.calls == 2
    assert info.metadata["title"] == "다시 받은 제목"


def test_video_info_gives_up_after_one_retry(monkeypatch):
    """영상 정보 요청이 다시 보낸 뒤에도 제한 시간에 걸리면 더 보내지 않고 Timeout을 올려야 한다.

    요청마다 ReadTimeout(세 번째 응답도 준비해 둠)
    -> requests.Timeout, 요청 2번
    """
    session = ScriptedSession(
        requests.ReadTimeout("1"), requests.ReadTimeout("2"), _video_info_response()
    )
    monkeypatch.setattr(network, "_session", session)

    with pytest.raises(requests.Timeout):
        NetworkManager.get_video_info("123", COOKIES)

    assert session.calls == 2


@pytest.mark.parametrize(
    "outcome",
    [requests.ConnectionError("refused"), MockResponse(status_code=500)],
    ids=["connection-error", "http-500"],
)
def test_video_info_is_not_requested_again_after_a_non_timeout_failure(monkeypatch, outcome):
    """영상 정보 요청이 제한 시간이 아닌 이유(연결 실패 · HTTP 오류)로 실패하면 다시 보내지 않아야 한다.

    첫 요청이 ConnectionError(또는 500 응답), 둘째 응답은 정상으로 준비해 둠
    -> requests.RequestException, 요청 1번
    """
    session = ScriptedSession(outcome, _video_info_response())
    monkeypatch.setattr(network, "_session", session)

    with pytest.raises(requests.RequestException):
        NetworkManager.get_video_info("123", COOKIES)

    assert session.calls == 1


@pytest.mark.parametrize(
    ("outcomes", "expected_key"),
    [
        ((requests.ReadTimeout("1"), requests.ReadTimeout("2")), "Server response is slow"),
        ((requests.ConnectTimeout("1"), requests.ConnectTimeout("2")), "Server response is slow"),
        ((requests.ConnectionError("refused"),), "Network connection error"),
    ],
    ids=["read-timeout-twice", "connect-timeout-twice", "connection-error"],
)
def test_lookup_tells_a_slow_response_apart_from_a_connection_failure(
    monkeypatch, outcomes, expected_key
):
    """영상 정보 조회가 두 번 다 제한 시간에 걸리면 응답 지연 키, 연결 실패면 연결 오류 키로 실패해야 한다.

    NetworkManager로 fetch_content — 세션이 outcomes 순서대로 실패
    -> MetadataError.message_key == expected_key
    """
    from core.services.metadata_service import MetadataError, fetch_content

    monkeypatch.setattr(network, "_session", ScriptedSession(*outcomes))

    with pytest.raises(MetadataError) as info:
        fetch_content(VIDEO_URL, COOKIES, "downloads", api=NetworkManager)

    assert info.value.message_key == expected_key
