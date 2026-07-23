"""컨텐츠 도메인 모델 — 메타데이터 데이터클래스와 컨텐츠 타입 (#61, SPEC §4.1).

서브클래스 없는 단일 데이터클래스로 정의한다 — 타입 간 차이는 도메인 행동이
아니라 전달 방식이므로, 타입별 분기는 다운로더가 담당한다.
ERROR는 컨텐츠 타입이 아니다 — 파싱·조회 실패는 예외로 다루고, 실패를 가짜
Content로 만들지 않는다.
"""

from dataclasses import dataclass, field
from enum import Enum
from uuid import uuid4


class ContentType(Enum):
    """컨텐츠 타입 (SPEC §4.1). 값은 현행 코드가 쓰는 문자열과 동일하다."""

    CHZZK_VIDEO = "video"
    CHZZK_VIDEO_M3U8 = "m3u8"
    CHZZK_CLIP = "clip"
    CHZZK_LIVE = "live"


@dataclass
class Content:
    """컨텐츠 메타데이터 (SPEC §4.1). 서브클래스 없는 단일 데이터클래스.

    필드는 현행 코드가 실제로 사용하는 것만 둔다. created_at·completed_at 등
    SPEC의 미사용 필드는 실제 소비처가 생길 때 추가한다.
    """

    content_type: ContentType
    url: str
    id: str = field(default_factory=lambda: uuid4().hex)
    download_path: str = ""
    output_path: str = ""
    channel_name: str | None = None
    title: str | None = None
    resolution: int | None = None
    # 재생·암호화 관련 필드
    base_url: str | None = None
    video_id: str | None = None
    in_key: str | None = None
    encryption_type: str | None = None
    live_rewind_playback_json: str | None = None


@dataclass(frozen=True)
class VideoInfo:
    """get_video_info의 결과 객체 (#61).

    기존 8-tuple 반환을 대체한다 — tuple은 항목이 늘어날 때마다 모든 호출부가
    위치 기반으로 깨질 위험이 있고 필드 의미가 코드에 드러나지 않는다.
    필드의 값·의미는 기존 tuple 항목과 1:1 대응한다.
    """

    video_id: str | None
    in_key: str | None
    adult: bool | None
    vod_status: str | None
    live_rewind_playback_json: str | None
    membership_benefit_type: str | None
    encryption_type: str | None
    metadata: dict
