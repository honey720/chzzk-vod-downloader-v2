"""ContentWorker.fetchVideo의 권한·암호화 분기 검증 (#55) + 어댑터 계약 검증 (#72).

조회 로직은 core/services/metadata_service.py로 이동했다(#72). 이 테스트는
어댑터를 통과한 끝단 동작(기존 ValueError 메시지·시그널 형식)이 유지되는지 본다.
분기 자체의 단위 테스트는 tests/unit/core/test_metadata_service.py에 있다.

- 암호화(encryptionType != null) VOD는 권한 유무와 무관하게 매니페스트 요청 전에
  "Encrypted content is not supported" 안내로 조기 실패해야 한다 (1차 방어).
- 멤버십 전용(MEMBER_ONLY) + inKey null이면 raw 401 대신 멤버십 안내 에러를 던져야 한다.
- 성인 VOD(adult=True, videoId 없음)의 기존 "Invalid cookies value" 동작은 유지돼야 한다.
- 정상 경로에서는 등록된 쿠키가 매니페스트 요청까지 전달돼야 한다.
"""

import pytest

import content.network as network
from content.network import NetworkManager
from content.worker import ContentWorker
from core.models.content import VideoInfo
from tests.mocks.mock_http import MockResponse

COOKIES = {"NID_AUT": "REDACTED", "NID_SES": "REDACTED"}
VOD_URL = "https://chzzk.naver.com/video/13714380"


def _make_worker() -> ContentWorker:
    """테스트용 ContentWorker를 생성한다 (QApplication 불필요)."""
    return ContentWorker(VOD_URL, COOKIES, "downloads")


@pytest.mark.parametrize(
    "fixture_name",
    [
        # 멤버십 권한 있는 상태(inKey 발급됨)의 실응답 박제 — 그래도 암호화라 다운로드 불가
        "video_encrypted_member_13714380.json",
        "video_encrypted_member_14283698.json",
        # 권한 없는 상태(inKey null)의 실응답 박제 — 암호화 안내가 멤버십 안내보다 우선
        "video_member_only_13714380.json",
    ],
)
def test_encrypted_vod_raises_early_encryption_error(monkeypatch, load_mock_response, fixture_name):
    """encryptionType이 AES인 실응답이면 권한 유무와 무관하게 조기 안내해야 한다 (#55).

    실제 응답 픽스처를 network 파싱까지 그대로 통과시켜 전체 경로를 검증한다.
    매니페스트 요청 없이 실패해야 하므로 get_video_dash_manifest 호출도 감시한다.
    """
    body = load_mock_response(fixture_name)
    monkeypatch.setattr(network._session, "get", lambda url, **kwargs: MockResponse(text=body))
    manifest_calls = []
    monkeypatch.setattr(
        NetworkManager,
        "get_video_dash_manifest",
        lambda *args, **kwargs: manifest_calls.append(args),
    )

    worker = _make_worker()

    with pytest.raises(ValueError, match="Encrypted content is not supported"):
        worker.fetchVideo("13714380")
    # 조기 감지: 매니페스트 요청까지 가지 않아야 한다
    assert manifest_calls == []


def test_member_only_without_in_key_raises_membership_error(monkeypatch):
    """비암호화 멤버십 전용 VOD(inKey null)이면 멤버십 안내 에러가 발생해야 한다."""

    def fake_get_video_info(video_no, cookies):
        return VideoInfo(
            video_id="video-id",
            in_key=None,
            adult=False,
            vod_status="ABR_HLS",
            live_rewind_playback_json=None,
            membership_benefit_type="MEMBER_ONLY",
            encryption_type=None,
            metadata={},
        )

    monkeypatch.setattr(NetworkManager, "get_video_info", fake_get_video_info)

    worker = _make_worker()

    with pytest.raises(ValueError, match="Channel membership required"):
        worker.fetchVideo("13714380")


def test_adult_without_video_id_keeps_invalid_cookies_error(monkeypatch):
    """성인 VOD(adult=True, videoId 없음)의 기존 에러 동작이 유지돼야 한다 (회귀 방지)."""

    def fake_get_video_info(video_no, cookies):
        return VideoInfo(
            video_id=None,
            in_key=None,
            adult=True,
            vod_status="ABR_HLS",
            live_rewind_playback_json=None,
            membership_benefit_type=None,
            encryption_type=None,
            metadata={},
        )

    monkeypatch.setattr(NetworkManager, "get_video_info", fake_get_video_info)

    worker = _make_worker()

    with pytest.raises(ValueError, match="Invalid cookies value"):
        worker.fetchVideo("13714380")


def test_dash_path_passes_cookies_to_manifest_request(monkeypatch):
    """inKey가 있는 비암호화 정상 경로에서 쿠키가 매니페스트 요청까지 전달돼야 한다 (#55)."""
    manifest_calls = []

    def fake_get_video_info(video_no, cookies):
        return VideoInfo(
            video_id="video-id",
            in_key="in-key",
            adult=False,
            vod_status="ABR_HLS",
            live_rewind_playback_json=None,
            membership_benefit_type="MEMBER_ONLY",
            encryption_type=None,
            metadata={"title": "t", "duration": 1},
        )

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


def test_run_emits_error_signal_in_legacy_format():
    """run() 실패 시 error 시그널이 기존 "<url>\\n<메시지>" 형식을 유지해야 한다 (#72).

    번역기가 로드되지 않은 테스트 환경에서 tr()은 원문 키를 그대로 돌려주므로,
    i18n 키 원문이 메시지에 그대로 실려야 한다.
    """
    bad_url = "https://example.com/video/1"
    worker = ContentWorker(bad_url, COOKIES, "downloads")
    captured: list[str] = []
    worker.error.connect(captured.append)

    worker.run()

    assert captured == [f"{bad_url}\nInvalid VOD URL"]
