"""ContentWorker의 권한·암호화 분기 검증 (#55) + 어댑터 계약 검증 (#72).

조회 로직은 core/services/metadata_service.py로 이동했다(#72). 이 테스트는
어댑터를 통과한 끝단 동작(시그널 페이로드·에러 메시지 형식)이 유지되는지 본다.
분기 자체의 단위 테스트는 tests/unit/core/test_metadata_service.py에 있다.

죽은 동기 API(fetchVideo/fetchClip)가 제거되어(#168) 모든 경로가 프로덕션과
동일한 run() → finished/error Signal로 검증된다 — 단언하는 메시지 키·result
tuple 형식은 종전 그대로다.

- 암호화(encryptionType != null) VOD는 권한이 있으면 SEA 경로로 조회된다 (#57).
- 멤버십 전용(MEMBER_ONLY) + inKey null이면 raw 401 대신 멤버십 안내 에러가 나와야 한다.
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


def _run_and_capture(worker: ContentWorker) -> tuple[list, list]:
    """run()을 실행해 finished/error Signal 페이로드를 수집한다.

    같은 스레드 emit은 direct 배달이라 QApplication 없이 동작한다.
    """
    results: list[tuple] = []
    errors: list[str] = []
    worker.finished.connect(lambda result, content_type: results.append((result, content_type)))
    worker.error.connect(errors.append)
    worker.run()
    return results, errors


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

    #55에서는 조기 거부했지만 SEA는 유저 본인 쿠키로 키를 받아 복호화할 수
    있는 표준 세그먼트 암호화라 지원 대상이 됐다. 어댑터의 반환 계약(result
    tuple)은 그대로여야 한다.
    """
    body = load_mock_response(fixture_name)
    monkeypatch.setattr(network._session, "get", lambda url, **kwargs: MockResponse(text=body))
    monkeypatch.setattr(
        NetworkManager,
        "get_video_sea_manifest",
        lambda *args, **kwargs: (
            [[144, "https://example.invalid/media.m3u8"]],
            144,
            "https://example.invalid/media.m3u8",
        ),
    )

    results, errors = _run_and_capture(_make_worker())

    assert errors == []
    ((result, content_type),) = results
    assert result[0] == VOD_URL
    assert result[4] == "https://example.invalid/media.m3u8"
    assert content_type == "hls_aes"  # AES(SEA) 경로로 분기됐다 (#57)


def test_encrypted_vod_without_entitlement_raises_membership_error(
    monkeypatch, load_mock_response
):
    """암호화 VOD라도 권한이 없으면(inKey null) 멤버십 안내가 나와야 한다 (#57)."""
    body = load_mock_response("video_member_only_13714380.json")
    monkeypatch.setattr(network._session, "get", lambda url, **kwargs: MockResponse(text=body))

    results, errors = _run_and_capture(_make_worker())

    assert results == []
    assert errors == [f"{VOD_URL}\nChannel membership required"]


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

    results, errors = _run_and_capture(_make_worker())

    assert results == []
    assert errors == [f"{VOD_URL}\nChannel membership required"]


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

    results, errors = _run_and_capture(_make_worker())

    assert results == []
    assert errors == [f"{VOD_URL}\nInvalid cookies value"]


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

    results, errors = _run_and_capture(_make_worker())

    assert errors == []
    ((result, content_type),) = results
    assert manifest_calls == [("video-id", "in-key", COOKIES)]
    # 반환 tuple 형식 유지: (vod_url, metadata, reps, resolution, base_url, path, liveRewindPlaybackJson)
    assert result[0] == VOD_URL
    assert result[3] == 1080
    assert result[6] is None
    assert content_type == "video"


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


def test_run_error_hides_raw_exception_details(monkeypatch):
    """MetadataError가 아닌 예외의 원시 문자열은 노출되지 않아야 한다 (#126).

    내부 API URL 등이 섞인 str(e) 대신 일반 안내 키가 실려야 하고,
    형식은 기존 "<url>\n<메시지>"를 유지한다.
    """
    from core.services import metadata_service

    def boom(*args, **kwargs):
        raise RuntimeError("secret detail https://api.chzzk.naver.com/internal")

    monkeypatch.setattr(metadata_service, "fetch_content", boom)
    worker = _make_worker()
    captured: list[str] = []
    worker.error.connect(captured.append)

    worker.run()

    assert captured == [f"{VOD_URL}\nFailed to fetch video information"]
    assert "secret detail" not in captured[0]
