"""ContentWorkerWeb 어댑터 계약 검증 (#220, Phase B1) — content/worker.py의 웹 버전.

조회 분기 자체(권한·암호화·멤버십 등)는 core/services/metadata_service.py가
이미 소유하고 tests/unit/core/test_metadata_service.py·tests/unit/test_worker.py가
검증해뒀다 — 여기서는 어댑터 계약(콜백 시그니처·에러 형식·i18n 주입·쿠키
비노출)만 본다. 문자열 그대로 이식했는지가 아니라, translate 콜러블이
실제로 호출되는지를 확인한다 — content/worker.py의 `.tr()` 리터럴
딕셔너리를 지웠으므로 매핑 누락은 이 테스트가 아니라 실행 자체가 증명한다.
"""

from app.viewmodels.content_worker_web import ContentWorkerWeb
from content.network import NetworkManager
from core.models.content import VideoInfo

VOD_URL = "https://chzzk.naver.com/video/13714380"
COOKIES = {"NID_AUT": "REDACTED", "NID_SES": "REDACTED"}


def _make_worker(translate=None) -> ContentWorkerWeb:
    return ContentWorkerWeb(VOD_URL, COOKIES, "downloads", translate=translate)


def _run_and_capture(worker: ContentWorkerWeb) -> tuple[list, list]:
    results: list[tuple] = []
    errors: list[str] = []
    worker.run(
        on_finished=lambda result, content_type: results.append((result, content_type)),
        on_error=errors.append,
    )
    return results, errors


def test_run_success_calls_on_finished_with_legacy_result_shape(monkeypatch):
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
        return [[1080, "https://example.invalid/1080"]], 1080, "https://example.invalid/1080"

    monkeypatch.setattr(NetworkManager, "get_video_info", fake_get_video_info)
    monkeypatch.setattr(NetworkManager, "get_video_dash_manifest", fake_get_dash_manifest)

    results, errors = _run_and_capture(_make_worker())

    assert errors == []
    ((result, content_type),) = results
    assert result[0] == VOD_URL
    assert result[3] == 1080
    assert content_type == "video"


def test_run_calls_on_error_in_legacy_format_without_translate():
    """translate 미주입(기본 항등 함수)이면 content/worker.py 무번역 상태와 동일한 문구."""
    bad_url = "https://example.com/video/1"
    worker = ContentWorkerWeb(bad_url, COOKIES, "downloads")
    _results, errors = _run_and_capture(worker)

    assert errors == [f"{bad_url}\nInvalid VOD URL"]


def test_translate_callable_is_actually_invoked():
    """주입한 translate가 .tr() 리터럴 딕셔너리를 대신해 실제로 호출된다."""
    bad_url = "https://example.com/video/1"
    calls = []

    def fake_translate(key: str) -> str:
        calls.append(key)
        return f"번역됨:{key}"

    worker = ContentWorkerWeb(bad_url, COOKIES, "downloads", translate=fake_translate)
    _results, errors = _run_and_capture(worker)

    assert calls == ["Invalid VOD URL"]
    assert errors == [f"{bad_url}\n번역됨:Invalid VOD URL"]


def test_run_error_hides_raw_exception_details(monkeypatch):
    """MetadataError가 아닌 예외의 원시 문자열은 노출되지 않아야 한다 (#126, content/worker.py와 동일 계약)."""
    from core.services import metadata_service

    def boom(*args, **kwargs):
        raise RuntimeError("secret detail https://api.chzzk.naver.com/internal")

    monkeypatch.setattr(metadata_service, "fetch_content", boom)
    worker = _make_worker()
    _results, errors = _run_and_capture(worker)

    assert errors == [f"{VOD_URL}\nFailed to fetch video information"]
    assert "secret detail" not in errors[0]


def test_run_failure_does_not_leak_cookie_values_to_log_or_error(monkeypatch, caplog):
    secret_cookies = {"NID_AUT": "SECRET-AUT-9f8e7d6c", "NID_SES": "SECRET-SES-1a2b3c4d"}
    from core.services import metadata_service

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(metadata_service, "fetch_content", boom)
    worker = ContentWorkerWeb(VOD_URL, secret_cookies, "downloads")

    with caplog.at_level("DEBUG"):
        _results, errors = _run_and_capture(worker)

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    for value in secret_cookies.values():
        assert value not in errors[0]
        assert value not in log_text
