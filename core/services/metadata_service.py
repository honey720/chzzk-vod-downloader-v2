"""컨텐츠 메타데이터 조회 서비스 (#72, SPEC §5 MetadataService).

ContentWorker(Qt 워커)에 있던 URL 파싱 → API 조회 → 에러 분기 로직을
식 그대로 옮긴 순수 함수들이다. GUI 없이(CLI·헤드리스·테스트) 재사용할 수 있다.

실패는 MetadataError로 던진다. message_key는 번역하지 않은 i18n 키 원문이며,
번역(tr)은 앱 계층 어댑터(ContentWorker)가 담당한다 — core는 Qt 번역기를 모른다.

api 매개변수: HTTP 호출 묶음(NetworkManager 호환 객체)을 주입받는다.
네트워크 계층이 아직 앱 영역(content/network.py)에 있어 core에서 직접
import할 수 없기 때문이다(core→app 의존 금지). 네트워크 계층이 core로
이주하면 기본 구현을 붙일 수 있다.
"""

from typing import Protocol

from core.api.url_parser import extract_content_no
from core.models.content import VideoInfo


class MetadataApi(Protocol):
    """메타데이터 조회에 필요한 HTTP 호출 묶음 (content.network.NetworkManager 호환)."""

    def get_video_info(self, video_no: str, cookies: dict) -> VideoInfo: ...

    def get_video_m3u8_manifest(self, json_str: str) -> tuple: ...

    def get_video_dash_manifest(
        self, video_id: str, in_key: str, cookies: dict | None = None
    ) -> tuple: ...

    def get_clip_info(self, clip_no: str, cookies: dict) -> tuple: ...

    def get_clip_manifest(self, clip_id: str, cookies: dict) -> tuple: ...


class MetadataError(Exception):
    """메타데이터 조회 실패.

    message_key는 번역하지 않은 i18n 키 원문이다(예: "Channel membership required").
    str()은 기존 ContentWorker의 ValueError와 같은 "<url>\\n<메시지>" 구조를 유지한다.
    """

    def __init__(self, message_key: str, url: str):
        super().__init__(f"{url}\n{message_key}")
        self.message_key = message_key
        self.url = url


def fetch_content(
    vod_url: str, cookies: dict, download_path: str, api: MetadataApi
) -> tuple[tuple, str]:
    """URL 파싱부터 매니페스트 조회까지 수행해 (result, content_type)을 반환한다.

    result는 기존 ContentWorker.finished 시그널 페이로드와 동일한 tuple이다:
    (vod_url, metadata, unique_reps, resolution, base_url, download_path,
    live_rewind_playback_json). content_type은 "video" / "m3u8" / "clip".

    Raises:
        MetadataError: URL 형식 오류·권한 부족·암호화·매니페스트 실패 등 조회 실패
    """
    content_type, content_no = extract_content_no(vod_url)
    if not content_type or not content_no:
        raise MetadataError("Invalid VOD URL", vod_url)

    if content_type == "video":
        result = fetch_video(vod_url, content_no, cookies, download_path, api)
        if result[6]:
            content_type = "m3u8"
    elif content_type == "clips":
        content_type = "clip"
        result = fetch_clip(vod_url, content_no, cookies, download_path, api)

    return result, content_type


def fetch_video(
    vod_url: str, video_no: str, cookies: dict, download_path: str, api: MetadataApi
) -> tuple:
    """VOD 메타데이터·매니페스트를 조회한다 (구 ContentWorker.fetchVideo와 동일 로직).

    Raises:
        MetadataError: 쿠키 무효(성인)·암호화·멤버십 필요·매니페스트 실패
    """
    info = api.get_video_info(video_no, cookies)
    if info.adult and not info.video_id:
        raise MetadataError("Invalid cookies value", vod_url)
    elif info.encryption_type:
        # 암호화(AES/SEA) VOD는 세그먼트를 복호화할 수 없어 다운로드 불가.
        # 권한(멤버십) 유무와 무관하므로 매니페스트 요청 전에 조기 안내한다 (#55)
        raise MetadataError("Encrypted content is not supported", vod_url)
    elif info.live_rewind_playback_json:
        unique_reps, resolution, base_url = api.get_video_m3u8_manifest(
            info.live_rewind_playback_json
        )
    else:
        # 멤버십 전용 VOD는 권한이 없으면 inKey가 null로 내려온다.
        # 이대로 매니페스트를 요청하면 원인을 알 수 없는 401이 노출되므로 먼저 안내한다 (#55)
        if not info.in_key and info.membership_benefit_type == "MEMBER_ONLY":
            raise MetadataError("Channel membership required", vod_url)
        unique_reps, resolution, base_url = api.get_video_dash_manifest(
            info.video_id, info.in_key, cookies
        )
    if not unique_reps:
        raise MetadataError("Failed to get DASH manifest", vod_url)

    # 네트워크 작업 결과를 tuple 형태로 묶어서 전달
    return (
        vod_url,
        info.metadata,
        unique_reps,
        resolution,
        base_url,
        download_path,
        info.live_rewind_playback_json,
    )


def fetch_clip(
    vod_url: str, clip_no: str, cookies: dict, download_path: str, api: MetadataApi
) -> tuple:
    """클립 메타데이터·매니페스트를 조회한다 (구 ContentWorker.fetchClip과 동일 로직).

    Raises:
        MetadataError: 미인코딩(.m3u8)·성인 인증 필요·매니페스트 실패
    """
    video_id, vodStatus, metadata = api.get_clip_info(clip_no, cookies)
    if vodStatus == "NONE":
        raise MetadataError("Unencoded Video(.m3u8)", vod_url)

    unique_reps, resolution, base_url, error = api.get_clip_manifest(video_id, cookies)
    if error and error.get("errorCode") == "ADULT_AUTH_REQUIRED":
        raise MetadataError("Invalid cookies value", vod_url)

    if not unique_reps:
        raise MetadataError("Failed to get DASH manifest", vod_url)

    return (vod_url, metadata, unique_reps, resolution, base_url, download_path, None)
