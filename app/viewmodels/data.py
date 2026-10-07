"""목록 카드 하나의 데이터(ContentItem)를 정의한다."""

from core.api.representations import file_tag, is_original, shown_frame_rate
from core.models.download_state import DownloadState

# ContentItem.section_check의 값 — 해상도를 바꾼 뒤 새 해상도의 길이를 확인하는 조회의 상태
SECTION_CHECK_PENDING = "pending"  # 조회 중 — 구간은 선언 프레임률로만 맞춰져 있다
SECTION_CHECK_UNVERIFIED = "unverified"  # 조회 실패 — 길이를 확인하지 못했다

# 세그먼트 단위로 받는 타입 — 전체 크기를 미리 알 수 없어 진행률·표시가
# 세그먼트 수 기반이다 (m3u8: 라이브 다시보기, hls_aes: AES 암호화 VOD #57)
SEGMENT_BASED_TYPES = ("m3u8", "hls_aes")


class ContentItem:
    """목록 카드 하나가 보여 주는 영상의 메타데이터와 다운로드 표시 상태를 담는다.

    조회 결과로 만들고, 다운로드가 도는 동안 뷰모델이 진행 값을 옮겨 적는다. 구간
    다운로드의 구간 목록과 완료 · 실패 구간 수도 여기에 둔다 (#309).
    """

    def __init__(
        self,
        vod_url,
        metadata,
        unique_reps,
        resolution,
        base_url,
        download_path,
        content_type,
        liveRewindPlaybackJson,
    ):
        self.vod_url = vod_url

        self.default_title = metadata.get("title", "Unknown Title")
        self.title = self.default_title
        self.thumbnail_url = metadata.get("thumbnailImageUrl", "")
        self.category = metadata.get("category", "Unknown Category")
        self.channel_name = metadata.get("channelName", "Unknown Channel")
        self.channel_image_url = metadata.get("channelImageUrl", "")
        self.live_open_date = metadata.get("createdDate", "Unknown Date")
        self.duration = metadata.get("duration", 0)

        self.content_type = content_type
        self.liveRewindPlaybackJson = liveRewindPlaybackJson
        self.post_process = False
        # 받을 구간 목록 (#309) — core.models.plan.TimeRange의 튜플. 비어 있으면 전체 다운로드다
        self.selections = ()
        self.sections_done = 0  # 잘라서 파일로 만든 구간 수 — 엔진의 구간 상태에서 옮긴 값
        self.sections_failed = 0  # 자르지 못한 구간 수
        # 일부 구간의 컷만 실패하고 끝난 다운로드가 남긴 것 (#309) — (그때의 스트림 · 저장 폴더 ·
        # 제목을 가리키는 값, core.models.section_resume.SectionResume). 다음 다운로드가 끝나지
        # 않은 구간만 다시 처리하는 데 쓴다. 없으면 None이다
        self.section_retry = None
        # 지금의 구간이 맞춰져 있는 프레임률(Fraction). 조회한 값이거나, 해상도를 바꾼 직후
        # 조회가 끝나기 전에는 목록 항목의 선언값이다. 구간이 없으면 None
        self.section_frame_rate = None
        # 사용자가 마지막으로 확정한 (구간 목록, 그때의 app.section_basis.SectionBasis) — 원래 값.
        # 구간 편집 창이 확인할 때만 바뀐다. 해상도를 바꿀 때마다 이 값을 새 프레임률 · 길이에
        # 다시 맞춘다(맞춘 것을 또 맞추지 않는다 — 원래 프레임률로 돌아오면 이 값이 그대로 나온다).
        # 저장 · 다운로드에는 쓰지 않는다 — 엔진에 넘기는 것은 맞춘 값(selections)이다. 구간이 없으면 None
        self.section_verified = None
        # 해상도를 바꾼 뒤 새 해상도의 길이를 확인했는지 — ""(확인함 · 바꾼 적 없음) ·
        # SECTION_CHECK_PENDING(조회 중) · SECTION_CHECK_UNVERIFIED(조회 실패)
        self.section_check = ""
        # 구간 요약 뒤에 붙이는 알림의 재료 — 문구는 카드가 만든다. 구간을 다시 편집하면 비운다
        self.section_refit_fps = None  # 구간을 이 프레임률(Fraction)의 프레임에 다시 맞췄다
        self.section_end_pulled = False  # 새 길이를 넘는 구간의 끝을 새 영상의 끝으로 당겼다
        self.section_end_extended = False  # 영상 끝에 닿아 있던 구간의 끝을 더 긴 새 끝으로 늘렸다
        # 새 영상의 끝 이후에서 시작해 당길 수 없는 구간의 번호(0부터). 구간은 그대로 두고
        # 카드에 경고를 붙이며, 받을 때 이 구간만 엔진에 넘기지 않는다
        self.section_unfit = frozenset()
        # 구간을 정하며 받은 moov — (그때의 주소, 받은 것). 받은 것은 moov의 바이트
        # (core.models.mp4_index.Mp4Raw)이고, 구간을 확인한 뒤 받을 크기를 세면서 해석한 색인
        # (Mp4Head)으로 바뀐다. 다운로드를 시작할 때 주소가 같으면 엔진에 넘겨 다시 받지 않게
        # 하고, 같은 카드의 편집 창을 다시 열 때도 다시 받지 않는다. 긴 영상의 색인은 100MB를
        # 넘어 앱 전체에서 한 카드의 것만 든다(section_edit_viewmodel.keep_section_head).
        # 인코딩 완료 VOD에만 있다. 없으면 None
        self.section_head = None
        # 구간 다운로드가 받을 바이트의 합 — 인코딩 완료 VOD(mp4)만. 구간을 확인할 때 moov로
        # 계산하고, 받기 시작하면 엔진이 정한 값(이어받기 · 뺀 구간이 반영된다)으로 바뀐다.
        # 모르면 None — 카드는 크기를 적지 않는다
        self.section_bytes = None
        # 받을 구간의 합을 백그라운드에서 세는 중이다(SectionSizer) — 대기 카드가 크기 자리에
        # "확인 중..."을 적는다. 다 세면(성공 · 실패 모두) False로 돌아간다
        self.section_sizing = False
        # 받기 시작 때 엔진이 정한 받을 크기(바이트) — 인코딩 완료 VOD만. 카드의 크기 조회가
        # 끝나지 않아 파일 크기를 모를 때 "받은 크기 / 받을 크기"의 분모로 쓴다. 받기 전에는 None
        self.transfer_bytes = None
        # 다운로드 준비 중(엔진이 받을 것을 정하는 중)임을 카드에 보일지 — 준비가 잠깐이면
        # 켜지 않는다(DownloadViewModel)
        self.preparing = False
        # 마지막 다운로드가 엔진에 넘긴 구간 파일 경로 — 번호가 작은 것부터. 완료 카드의 폴더
        # 열기가 실제로 만들어진 구간 파일을 찾는 데 쓴다. 구간 다운로드를 한 적이 없으면 빈 튜플
        self.section_paths = ()
        # 고른 해상도 항목의 선언 프레임률(소수). 선언이 없으면 None
        self.selected_frame_rate = None

        self.unique_reps = unique_reps

        self.resolution = resolution
        self.total_size = ""
        self.base_url = base_url
        # 고른 항목이 가리키는 스트림 (#318). 해상도가 같은 두 스트림을 가르는 값이다 —
        # stream은 다운로드 때 같은 변형을 다시 찾는 값, resolution_tag는 파일명의
        # `{해상도}p` 뒤에 붙는 표시("(원본)" 또는 빈 문자열)
        self.stream = None
        self.resolution_tag = ""
        self.select_rep(self._auto_rep(resolution, base_url))

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

    def _auto_rep(self, resolution, base_url):
        """기본 선택(목록의 마지막 항목)이 생성자가 받은 해상도·주소와 같으면 그 항목."""
        if not self.unique_reps:
            return None
        last = self.unique_reps[-1]
        return last if (last[0], last[1]) == (resolution, base_url) else None

    def select_rep(self, rep) -> None:
        """목록의 한 항목을 고른다 — 해상도·주소와 함께 그 스트림의 정체를 기억한다 (#318)."""
        if rep is None:
            return
        self.resolution = rep[0]
        self.base_url = rep[1]
        self.stream = getattr(rep, "stream", None)
        self.selected_frame_rate = getattr(rep, "frame_rate", None)
        self.resolution_tag = file_tag(self.unique_reps, rep)

    @staticmethod
    def rep_is_original(rep) -> bool:
        """그 항목이 원본 스트림인지 — 버튼에 원본 표시를 붙일지 정한다."""
        return is_original(rep)

    @staticmethod
    def rep_frame_rate(rep) -> int | None:
        """그 항목에 표시할 프레임률(50fps 이상일 때만). 없으면 None."""
        return shown_frame_rate(rep)

    @property
    def is_segment_based(self) -> bool:
        """세그먼트 단위 다운로드 타입인지 (전체 크기 미상 → 표시 방식이 다르다)."""
        return self.content_type in SEGMENT_BASED_TYPES

    def setDownloadState(self, state: DownloadState):
        if self.downloadState != state:
            self.downloadState = state
