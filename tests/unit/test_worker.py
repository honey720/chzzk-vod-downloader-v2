"""ContentWorker.fetchVideo의 권한 분기 검증 (#55).

- 멤버십 전용(MEMBER_ONLY) + inKey null이면 raw 401 대신 안내 에러를 던져야 한다.
- 성인 VOD(adult=True, videoId 없음)의 기존 "Invalid cookies value" 동작은 유지돼야 한다.
- 정상 경로에서는 등록된 쿠키가 매니페스트 요청까지 전달돼야 한다.
"""

import pytest

import content.network as network
from content.network import NetworkManager
from content.worker import ContentWorker
from tests.mocks.mock_http import MockResponse

COOKIES = {"NID_AUT": "REDACTED", "NID_SES": "REDACTED"}
VOD_URL = "https://chzzk.naver.com/video/13714380"


def _make_worker() -> ContentWorker:
    """테스트용 ContentWorker를 생성한다 (QApplication 불필요)."""
    return ContentWorker(VOD_URL, COOKIES, "downloads")


def test_member_only_without_in_key_raises_membership_error(monkeypatch, load_mock_response):
    """멤버십 전용 VOD 응답(inKey null)이면 멤버십 안내 에러가 발생해야 한다.

    실제 응답 픽스처를 network 파싱까지 그대로 통과시켜 전체 경로를 검증한다.
    """
    body = load_mock_response("video_member_only_13714380.json")
    monkeypatch.setattr(network._session, "get", lambda url, **kwargs: MockResponse(text=body))

    worker = _make_worker()

    with pytest.raises(ValueError, match="Channel membership required"):
        worker.fetchVideo("13714380")


def test_adult_without_video_id_keeps_invalid_cookies_error(monkeypatch):
    """성인 VOD(adult=True, videoId 없음)의 기존 에러 동작이 유지돼야 한다 (회귀 방지)."""

    def fake_get_video_info(video_no, cookies):
        return None, None, True, "ABR_HLS", None, None, {}

    monkeypatch.setattr(NetworkManager, "get_video_info", fake_get_video_info)

    worker = _make_worker()

    with pytest.raises(ValueError, match="Invalid cookies value"):
        worker.fetchVideo("13714380")


def test_dash_path_passes_cookies_to_manifest_request(monkeypatch):
    """inKey가 있는 정상 경로에서 쿠키가 매니페스트 요청까지 전달돼야 한다 (#55)."""
    manifest_calls = []

    def fake_get_video_info(video_no, cookies):
        metadata = {"title": "t", "duration": 1}
        return "video-id", "in-key", False, "ABR_HLS", None, "MEMBER_ONLY", metadata

    def fake_get_dash_manifest(video_id, in_key, cookies=None):
        manifest_calls.append((video_id, in_key, cookies))
        return [[1080, "https://example.invalid/1080"]], 1080, "https://example.invalid/1080"

    monkeypatch.setattr(NetworkManager, "get_video_info", fake_get_video_info)
    monkeypatch.setattr(NetworkManager, "get_video_dash_manifest", fake_get_dash_manifest)

    worker = _make_worker()
    result = worker.fetchVideo("13714380")

    assert manifest_calls == [("video-id", "in-key", COOKIES)]
    # 반환 tuple 형식 유지: (vod_url, metadata, reps, resolution, base_url, path, liveRewindPlaybackJson)
    assert result[0] == VOD_URL
    assert result[3] == 1080
    assert result[6] is None
