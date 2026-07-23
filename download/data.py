"""다운로드 한 건의 공유 데이터 — core 모델로 통합 중인 어댑터 (#61).

기존에는 컨텐츠 정보·상태·진행률 필드가 이 클래스에 평면적으로 흩어져 있었다.
이제 소유는 core 모델이 한다:

- 컨텐츠 식별·재생 정보(vod_url, base_url, output_path, resolution, content_type)
  → core/models/content.py의 Content
- 상태·진행률(total_size, threads_progress, start/end_time, _pause_event)
  → core/models/download_task.py의 DownloadTaskModel (#60에서 신설, 단일 소유처)

다운로드 엔진(worker/monitor)은 무변경 유지가 원칙이므로(Phase 3 소관),
기존 속성 이름을 위임 프로퍼티로 제공한다. 엔진 내부 구현 변수
(future_count, remaining_ranges 등)는 도메인 데이터가 아니므로 이곳에 남는다.
"""

from core.models.content import Content, ContentType
from core.models.download_task import DownloadTaskModel


class DownloadData:
    """다운로드 스레드·모니터가 공유하는 데이터 홀더 (core 모델 위임 어댑터)."""

    def __init__(self, base_url, vod_url, output_path, resolution, content_type):
        # 컨텐츠 식별·재생 정보의 소유처 (#61)
        self.content = Content(
            content_type=ContentType(content_type),
            url=vod_url,
            output_path=output_path,
            resolution=resolution,
            base_url=base_url,
        )
        # 상태·진행률의 소유처. DownloadTask 어댑터가 이 모델로 상태 전이를 수행한다
        self.model = DownloadTaskModel()

        self.total_downloaded_size = 0

        # CPU 개수를 바탕으로 초깃값 지정
        self.adjust_threads = 4
        self.max_threads = self.adjust_threads

        self.future_count = 0
        self.total_ranges = 0
        self.remaining_ranges = []

        self.speed_mb = 0
        self.merged_segments = 0

        # 진행도/실패/재시작/스레드 관련 변수
        self.completed_threads = 0
        self.failed_threads = 0
        self.restart_threads = 0
        self.completed_progress = 0

        # MonitorThread 내부 변수
        self.prev_size = 0

    # ============ 컨텐츠 필드 위임 (Content 소유) ============

    @property
    def base_url(self):
        return self.content.base_url

    @base_url.setter
    def base_url(self, value):
        self.content.base_url = value

    @property
    def vod_url(self):
        return self.content.url

    @vod_url.setter
    def vod_url(self, value):
        self.content.url = value

    @property
    def output_path(self):
        return self.content.output_path

    @output_path.setter
    def output_path(self, value):
        self.content.output_path = value

    @property
    def resolution(self):
        return self.content.resolution

    @resolution.setter
    def resolution(self, value):
        self.content.resolution = value

    @property
    def content_type(self) -> str:
        # 엔진은 기존 문자열('video'/'m3u8'/'clip')과 비교하므로 값을 그대로 돌려준다
        return self.content.content_type.value

    @content_type.setter
    def content_type(self, value: str):
        self.content.content_type = ContentType(value)

    # ============ 상태·진행률 필드 위임 (DownloadTaskModel 소유) ============

    @property
    def total_size(self):
        return self.model.total_size

    @total_size.setter
    def total_size(self, value):
        self.model.total_size = value

    @property
    def threads_progress(self):
        return self.model.threads_progress

    @threads_progress.setter
    def threads_progress(self, value):
        self.model.threads_progress = value

    @property
    def start_time(self):
        return self.model.start_time

    @start_time.setter
    def start_time(self, value):
        self.model.start_time = value

    @property
    def end_time(self):
        return self.model.end_time

    @end_time.setter
    def end_time(self, value):
        self.model.end_time = value

    @property
    def _pause_event(self):
        return self.model.pause_event
