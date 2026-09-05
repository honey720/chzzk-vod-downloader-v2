from core.models.download_state import DownloadState

# 세그먼트 단위로 받는 타입 — 전체 크기를 미리 알 수 없어 진행률·표시가
# 세그먼트 수 기반이다 (m3u8: 라이브 다시보기, hls_aes: AES 암호화 VOD #57)
SEGMENT_BASED_TYPES = ("m3u8", "hls_aes")


class ContentItem:
    # 메타데이터 카드 리스트 아이템 데이터(DTO)

    def __init__(self, vod_url, metadata, unique_reps, resolution, base_url, download_path, content_type, liveRewindPlaybackJson):
        self.vod_url = vod_url
        
        self.default_title = metadata.get('title', 'Unknown Title')
        self.title = self.default_title
        self.thumbnail_url = metadata.get('thumbnailImageUrl', '')
        self.category = metadata.get('category', 'Unknown Category')
        self.channel_name = metadata.get('channelName', 'Unknown Channel')
        self.channel_image_url = metadata.get('channelImageUrl', '')
        self.live_open_date = metadata.get('createdDate', 'Unknown Date')
        self.duration = metadata.get('duration', 0)

        self.content_type = content_type
        self.liveRewindPlaybackJson = liveRewindPlaybackJson
        self.post_process = False

        self.unique_reps = unique_reps
        
        self.resolution = resolution
        self.total_size = ""
        self.base_url = base_url

        self.download_path = download_path
        self.output_path = ""

        self.download_size = ""
        self.download_progress = 0  # 다운로드 진행률 (0~100)
        self.download_speed = ""  # 다운로드 속도 (예: "2.5 MB/s")
        self.download_remain_time = ""  # 남은 다운로드 예상 시간 (예: "00:00:01")
        self.download_time = ""

        self.downloadState = DownloadState.WAITING  # 초기 상태
        # 실패 사유 등 카드에 표시할 상태 메시지 (#134). 키 기반 매핑을 거친
        # 번역 문자열만 넣는다 — 원시 예외 문자열 금지
        self.stateMessage = ""

    @property
    def is_segment_based(self) -> bool:
        """세그먼트 단위 다운로드 타입인지 (전체 크기 미상 → 표시 방식이 다르다)."""
        return self.content_type in SEGMENT_BASED_TYPES

    def setDownloadState(self, state: DownloadState):
        if self.downloadState != state:
            self.downloadState = state