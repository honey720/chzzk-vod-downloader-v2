"""core 도메인 모델 패키지 — UI와 무관한 순수 데이터·상태 모델 (#60, #61)."""

from core.models.content import Content, ContentType, VideoInfo
from core.models.download_state import DownloadState
from core.models.download_task import DownloadTaskModel, InvalidStateTransitionError
from core.models.plan import DownloadPlan, TimeRange

__all__ = [
    "Content",
    "ContentType",
    "VideoInfo",
    "DownloadPlan",
    "DownloadState",
    "DownloadTaskModel",
    "InvalidStateTransitionError",
    "TimeRange",
]
