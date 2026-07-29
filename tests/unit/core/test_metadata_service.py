"""core/services/metadata_service.py 단위 테스트 (#72).

ContentWorker에 있던 권한·암호화 분기 검증(#55)을 core 경로로 옮겨 수행한다.
실 API 호출 없이 픽스처(tests/fixtures/mock_responses)와 가짜 api 객체를 쓴다.

- 암호화 VOD: 실응답 픽스처를 NetworkManager 파싱까지 통과시켜 전체 경로 검증
- 멤버십·성인·클립 분기: 가짜 api 객체로 서비스 로직만 검증
- MetadataError: message_key(i18n 키 원문)·url·str() 형식 계약 검증
"""

import pytest
import requests

import content.network as network
from content.network import NetworkManager
from core.models.content import VideoInfo
from core.services.metadata_service import (
    MetadataError,
    fetch_clip,
    fetch_content,
    fetch_video,
)
from tests.mocks.mock_http import MockResponse

COOKIES = {"NID_AUT": "REDACTED", "NID_SES": "REDACTED"}
VOD_URL = "https://chzzk.naver.com/video/13714380"
CLIP_URL = "https://chzzk.naver.com/clips/clipUID"
DOWNLOAD_PATH = "downloads"


def _video_info(**overrides) -> VideoInfo:
    """테스트용 VideoInfo를 만든다. 기본값은 비암호화·정상 권한 상태."""
    values = dict(
        video_id="video-id",
        in_key="in-key",
        adult=False,
        vod_status="ABR_HLS",
        live_rewind_playback_json=None,
        membership_benefit_type=None,
        encryption_type=None,
        metadata={"title": "t", "duration": 1},
    )
    values.update(overrides)
    return VideoInfo(**values)


class FakeApi:
    """MetadataApi 프로토콜을 구현하는 가짜 api. 필요한 메서드만 주입해 쓴다."""

    def __init__(
        self,
        video_info=None,
        clip_info=None,
        clip_manifest=None,
        dash_manifest=None,
        sea_manifest=None,
    ):
        self._video_info = video_info
        self._clip_info = clip_info
        self._clip_manifest = clip_manifest
        self._dash_manifest = dash_manifest
        self._sea_manifest = sea_manifest
        self.dash_calls: list[tuple] = []
        self.sea_calls: list[tuple] = []

    def get_video_info(self, video_no, cookies):
        return self._video_info

    def get_video_dash_manifest(self, video_id, in_key, cookies=None):
        self.dash_calls.append((video_id, in_key, cookies))
        return self._dash_manifest

    def get_video_sea_manifest(self, video_id, in_key, cookies=None):
        self.sea_calls.append((video_id, in_key, cookies))
        return self._sea_manifest

    def get_video_m3u8_manifest(self, json_str):
        return [[720, None]], 720, None

    def get_clip_info(self, clip_no, cookies):
        return self._clip_info

    def get_clip_manifest(self, clip_id, cookies):
        return self._clip_manifest


# ---------------------------------------------------------------- MetadataError 계약


def test_metadata_error_carries_untranslated_key_and_url():
    """message_key는 i18n 키 원문, str()은 "<url>\\n<키>" 형식이어야 한다 (어댑터 계약)."""
    e = MetadataError("Channel membership required", VOD_URL)

    assert e.message_key == "Channel membership required"
    assert e.url == VOD_URL
    assert str(e) == f"{VOD_URL}\nChannel membership required"


# ---------------------------------------------------------------- fetch_content 분기


def test_fetch_content_rejects_invalid_url():
    """URL 파싱 실패는 API 호출 전에 Invalid VOD URL로 실패해야 한다."""
    with pytest.raises(MetadataError, match="Invalid VOD URL"):
        fetch_content("https://example.com/video/1", COOKIES, DOWNLOAD_PATH, api=FakeApi())


def test_fetch_content_maps_video_with_live_rewind_to_m3u8():
    """liveRewindPlaybackJson이 있으면 content_type이 m3u8로 바뀌어야 한다."""
    api = FakeApi(video_info=_video_info(live_rewind_playback_json='{"media": []}'))

    result, content_type = fetch_content(VOD_URL, COOKIES, DOWNLOAD_PATH, api=api)

    assert content_type == "m3u8"
    assert result[6] == '{"media": []}'


def test_fetch_content_maps_clips_to_clip():
    """clips URL은 content_type "clip"으로 정규화돼야 한다."""
    api = FakeApi(
        clip_info=("clip-video-id", "DONE", {"title": "c"}),
        clip_manifest=(
            [[720, "https://example.invalid/720"]],
            720,
            "https://example.invalid/720",
            None,
        ),
    )

    result, content_type = fetch_content(CLIP_URL, COOKIES, DOWNLOAD_PATH, api=api)

    assert content_type == "clip"
    # 반환 tuple 형식: (url, metadata, reps, resolution, base_url, path, liveRewindPlaybackJson)
    assert result[0] == CLIP_URL
    assert result[3] == 720
    assert result[6] is None


# ---------------------------------------------------------------- fetch_video 에러 분기 (#55)


@pytest.mark.parametrize(
    "fixture_name",
    [
        # 멤버십 권한 있는 상태(inKey 발급됨)의 실응답 박제
        "video_encrypted_member_13714380.json",
        "video_encrypted_member_14283698.json",
    ],
)
def test_encrypted_vod_with_entitlement_takes_sea_path(
    monkeypatch, load_mock_response, fixture_name
):
    """AES(SEA) 암호화 VOD는 권한이 있으면 SEA 경로로 조회된다 (#57).

    #55에서는 여기서 조기 거부했지만, SEA는 유저 본인 쿠키로 키를 받아
    복호화할 수 있는 표준 세그먼트 암호화이므로 지원 대상이 됐다.
    실제 응답 픽스처를 NetworkManager 파싱까지 통과시켜 전체 경로를 검증한다.
    """
    body = load_mock_response(fixture_name)
    monkeypatch.setattr(network._session, "get", lambda url, **kwargs: MockResponse(text=body))
    sea_calls = []
    monkeypatch.setattr(
        NetworkManager,
        "get_video_sea_manifest",
        lambda *args, **kwargs: (
            sea_calls.append(args) or ([[144, "https://example.invalid/media.m3u8"]], 144, "https://example.invalid/media.m3u8")
        ),
    )

    result, encrypted = fetch_video(VOD_URL, "13714380", COOKIES, DOWNLOAD_PATH, api=NetworkManager)

    assert encrypted is True
    assert len(sea_calls) == 1  # 평문 경로가 아니라 SEA 매니페스트를 조회한다
    assert result[4] == "https://example.invalid/media.m3u8"


def test_encrypted_vod_without_entitlement_raises_membership_error(
    monkeypatch, load_mock_response
):
    """암호화 VOD라도 권한이 없으면(inKey null) 멤버십 안내가 나와야 한다 (#57).

    #55에서는 암호화 안내가 우선이었으나, 이제 암호화 자체는 지원되므로
    실제 막고 있는 원인(권한 없음)을 안내하는 편이 정확하다.
    """
    body = load_mock_response("video_member_only_13714380.json")
    monkeypatch.setattr(network._session, "get", lambda url, **kwargs: MockResponse(text=body))

    with pytest.raises(MetadataError, match="Channel membership required"):
        fetch_video(VOD_URL, "13714380", COOKIES, DOWNLOAD_PATH, api=NetworkManager)


def test_unsupported_encryption_still_raises_encryption_error():
    """지원 대상이 아닌 암호화(라이선스 서버형 DRM 등)는 기존 안내로 실패해야 한다 (#57).

    NetworkManager.get_video_sea_manifest가 지원 판정에 실패하면 빈 결과를
    돌려주고, 그것이 "Encrypted content is not supported"로 이어진다 —
    DRM 우회는 구현하지 않는다.
    """
    api = FakeApi(
        video_info=_video_info(encryption_type="AES"),
        sea_manifest=([], None, None),
    )

    with pytest.raises(MetadataError, match="Encrypted content is not supported"):
        fetch_video(VOD_URL, "13714380", COOKIES, DOWNLOAD_PATH, api=api)


def test_fetch_content_maps_encrypted_video_to_hls_aes():
    """암호화 VOD는 content_type이 hls_aes로 매핑돼야 한다 (#57)."""
    api = FakeApi(
        video_info=_video_info(encryption_type="AES"),
        sea_manifest=(
            [[144, "https://example.invalid/media.m3u8"]],
            144,
            "https://example.invalid/media.m3u8",
        ),
    )

    result, content_type = fetch_content(VOD_URL, COOKIES, DOWNLOAD_PATH, api=api)

    assert content_type == "hls_aes"
    assert result[4] == "https://example.invalid/media.m3u8"
    assert result[6] is None  # 라이브 다시보기가 아니다


def test_member_only_without_in_key_raises_membership_error():
    """비암호화 멤버십 전용 VOD(inKey null)이면 멤버십 안내 에러가 발생해야 한다."""
    api = FakeApi(
        video_info=_video_info(in_key=None, membership_benefit_type="MEMBER_ONLY", metadata={})
    )

    with pytest.raises(MetadataError, match="Channel membership required"):
        fetch_video(VOD_URL, "13714380", COOKIES, DOWNLOAD_PATH, api=api)


def test_adult_without_video_id_raises_invalid_cookies_error():
    """성인 VOD(adult=True, videoId 없음)는 Invalid cookies value로 실패해야 한다."""
    api = FakeApi(video_info=_video_info(video_id=None, in_key=None, adult=True, metadata={}))

    with pytest.raises(MetadataError, match="Invalid cookies value"):
        fetch_video(VOD_URL, "13714380", COOKIES, DOWNLOAD_PATH, api=api)


def test_empty_manifest_raises_dash_error():
    """매니페스트가 비면 Failed to get DASH manifest로 실패해야 한다 (2차 방어선)."""
    api = FakeApi(video_info=_video_info(), dash_manifest=([], None, None))

    with pytest.raises(MetadataError, match="Failed to get DASH manifest"):
        fetch_video(VOD_URL, "13714380", COOKIES, DOWNLOAD_PATH, api=api)


def test_dash_path_passes_cookies_to_manifest_request():
    """inKey가 있는 비암호화 정상 경로에서 쿠키가 매니페스트 요청까지 전달돼야 한다."""
    api = FakeApi(
        video_info=_video_info(membership_benefit_type="MEMBER_ONLY"),
        dash_manifest=(
            [[1080, "https://example.invalid/1080"]],
            1080,
            "https://example.invalid/1080",
        ),
    )

    result, encrypted = fetch_video(VOD_URL, "13714380", COOKIES, DOWNLOAD_PATH, api=api)

    assert encrypted is False
    assert api.dash_calls == [("video-id", "in-key", COOKIES)]
    # 반환 tuple 형식 유지: (vod_url, metadata, reps, resolution, base_url, path, liveRewindPlaybackJson)
    assert result[0] == VOD_URL
    assert result[3] == 1080
    assert result[6] is None


# ---------------------------------------------------------------- fetch_clip 에러 분기


def test_unencoded_clip_raises_m3u8_error():
    """vodStatus NONE(미인코딩) 클립은 Unencoded Video(.m3u8)로 실패해야 한다."""
    api = FakeApi(clip_info=("clip-video-id", "NONE", {}))

    with pytest.raises(MetadataError) as exc_info:
        fetch_clip(CLIP_URL, "clipUID", COOKIES, DOWNLOAD_PATH, api=api)
    assert exc_info.value.message_key == "Unencoded Video(.m3u8)"


def test_clip_adult_auth_error_raises_invalid_cookies_error():
    """ADULT_AUTH_REQUIRED 에러 응답이면 Invalid cookies value로 실패해야 한다."""
    api = FakeApi(
        clip_info=("clip-video-id", "DONE", {}),
        clip_manifest=(None, None, None, {"errorCode": "ADULT_AUTH_REQUIRED"}),
    )

    with pytest.raises(MetadataError, match="Invalid cookies value"):
        fetch_clip(CLIP_URL, "clipUID", COOKIES, DOWNLOAD_PATH, api=api)


def test_clip_empty_manifest_raises_dash_error():
    """클립 매니페스트가 비면 Failed to get DASH manifest로 실패해야 한다."""
    api = FakeApi(
        clip_info=("clip-video-id", "DONE", {}),
        clip_manifest=([], None, None, None),
    )

    with pytest.raises(MetadataError, match="Failed to get DASH manifest"):
        fetch_clip(CLIP_URL, "clipUID", COOKIES, DOWNLOAD_PATH, api=api)


# ---------------------------------------------------------------- 전송 오류 매핑 (#126)


class RaisingApi(FakeApi):
    """get_video_info에서 지정한 예외를 던지는 가짜 api."""

    def __init__(self, exc: Exception):
        super().__init__()
        self._exc = exc

    def get_video_info(self, video_no, cookies):
        raise self._exc


def _http_error(status: int) -> requests.HTTPError:
    """raise_for_status()가 만드는 형태의 HTTPError (내부 API URL 포함)."""
    response = requests.Response()
    response.status_code = status
    response.url = "https://api.chzzk.naver.com/service/v2/videos/1"
    return requests.HTTPError(
        f"{status} Client Error: Not Found for url: {response.url}", response=response
    )


@pytest.mark.parametrize(
    ("exc", "expected_key"),
    [
        (_http_error(404), "Video not found"),
        (_http_error(401), "Viewing permission required"),
        (_http_error(403), "Viewing permission required"),
        (_http_error(500), "Failed to fetch video information"),
        (requests.ConnectionError("dns fail"), "Network connection error"),
        (requests.Timeout("timed out"), "Network connection error"),
        (requests.TooManyRedirects("redirect loop"), "Failed to fetch video information"),
    ],
)
def test_fetch_content_wraps_transport_errors(exc, expected_key):
    """원시 requests 예외는 유형별 MetadataError 키로 감싸져야 한다 (#126)."""
    with pytest.raises(MetadataError) as info:
        fetch_content(VOD_URL, COOKIES, DOWNLOAD_PATH, api=RaisingApi(exc))

    assert info.value.message_key == expected_key
    assert info.value.url == VOD_URL
    # 유저에게 갈 메시지에 내부 API 주소가 없어야 한다
    assert "api.chzzk.naver.com" not in str(info.value)
    # 진단용 원인 예외는 체인으로 보존된다 (어댑터 logger.exception의 traceback용)
    assert info.value.__cause__ is exc


def test_fetch_content_passes_domain_errors_through():
    """서비스가 스스로 판정한 MetadataError(#55 멤버십 등)는 전송 매핑을 거치지 않아야 한다."""
    api = FakeApi(video_info=_video_info(in_key=None, membership_benefit_type="MEMBER_ONLY"))

    with pytest.raises(MetadataError, match="Channel membership required"):
        fetch_content(VOD_URL, COOKIES, DOWNLOAD_PATH, api=api)
