"""fMP4 세그먼트에서 뽑은 색인 모델 (#309).

라이브 다시보기(m3u8 경로)는 HLS fMP4로 내려온다. 플레이리스트의 ``EXT-X-MAP``이
가리키는 초기화 세그먼트 하나와, 샘플이 든 미디어 세그먼트 여럿이다.

- 초기화 세그먼트에는 샘플이 없다. 트랙의 timescale·코덱·편집 목록과, 미디어
  세그먼트가 값을 생략했을 때 쓰는 기본값(trex)이 있다 → ``Fmp4Init``
- 미디어 세그먼트의 moof에 샘플별 시각·크기·키프레임 여부가 있다 → ``Fmp4Segment``
- 여러 세그먼트를 이어 초 단위로 바꾼 것 → ``Fmp4Index``

해석은 ``core.api.fmp4``가 한다.
"""

from dataclasses import dataclass, field
from fractions import Fraction

from core.api.hls import HlsPlaylist


@dataclass(frozen=True)
class Fmp4Track:
    """초기화 세그먼트가 말하는 트랙 하나의 정보를 담는다."""

    track_id: int  # 트랙 번호 (tkhd). 미디어 세그먼트의 traf가 이 번호로 트랙을 가리킨다
    handler: str  # "vide" 또는 "soun"
    codec: str  # 샘플 엔트리의 4글자 코드 — "avc1" · "hvc1" · "mp4a" 등
    timescale: int  # 이 트랙의 초당 틱 수 (mdhd)
    empty_edit: Fraction  # 편집 목록 앞의 빈 편집 길이(초)
    media_time: int  # 편집 목록 구간의 시작(틱) — 이 PTS가 표시 시각 0이다
    default_duration: int  # trex의 기본 샘플 길이(틱)
    default_size: int  # trex의 기본 샘플 크기(바이트)
    default_flags: int  # trex의 기본 샘플 플래그
    # 오디오 샘플 엔트리가 선언한 비트레이트(bit/s) — esds·btrt. 영상 트랙이거나 적혀 있지
    # 않으면 None이다. 어느 세그먼트를 받았는지와 무관하게 스트림에 하나다
    declared_bitrate: int | None = None


@dataclass(frozen=True)
class Fmp4Init:
    """초기화 세그먼트의 해석 결과를 담는다. 영상·오디오 트랙을 각각 첫 번째 것만 든다."""

    video: Fmp4Track  # 영상 트랙
    audio: Fmp4Track | None  # 오디오 트랙. 없으면 None


@dataclass(frozen=True)
class Fmp4Samples:
    """미디어 세그먼트 안의 한 트랙 샘플을 담는다.

    모든 튜플은 길이가 같고 **디코드 순서**다. 시각은 그 트랙 timescale의 틱이고
    편집 목록을 적용하기 전의 값이다.
    """

    decode_times: tuple[int, ...]  # 샘플별 DTS(틱)
    presentation_times: tuple[int, ...]  # 샘플별 PTS(틱) — DTS + composition offset
    durations: tuple[int, ...]  # 샘플별 길이(틱)
    sizes: tuple[int, ...]  # 샘플별 크기(바이트)
    sync_samples: tuple[int, ...]  # 단독으로 디코드를 시작할 수 있는 샘플의 인덱스(오름차순)


@dataclass(frozen=True)
class Fmp4Segment:
    """미디어 세그먼트 하나(또는 그 앞부분)의 해석 결과를 담는다."""

    video: Fmp4Samples  # 영상 트랙의 샘플. 이 세그먼트에 없으면 빈 튜플들이다
    audio: Fmp4Samples  # 오디오 트랙의 샘플. 없으면 빈 튜플들이다
    fragments: int  # 읽은 moof의 수


@dataclass(frozen=True)
class MoofScan:
    """세그먼트 앞부분에서 moof가 어디까지인지 훑은 결과를 담는다."""

    complete: bool  # 받은 bytes에 첫 mdat 앞의 moof가 끝까지 들어 있다
    moof_end: int  # 첫 mdat 앞의 moof가 끝나는 위치(바이트). 아직 모르면 지금까지 확인한 위치
    next_offset: int | None  # 첫 mdat 다음 상자의 위치 — moof가 더 있을 수 있는 자리. 모르면 None


@dataclass
class Fmp4Head:
    """구간을 정하려고 받은 것을 담는다 — 플레이리스트, 초기화 세그먼트, 세그먼트의 moof (#309).

    구간 다운로드는 받기 전에 프레임 시각을 알아야 한다. 그때 받은 것을 들고 있다가
    엔진이 그대로 쓴다 — 같은 것을 두 번 받지 않는다. ``segments``는 받는 대로 채워지는
    보관함이라 이 객체는 불변이 아니다.
    """

    playlist: HlsPlaylist  # 미디어 플레이리스트
    init_data: bytes  # 초기화 세그먼트(EXT-X-MAP)의 bytes — 구간마다의 임시 파일 맨 앞에 쓴다
    init: Fmp4Init  # init_data를 해석한 결과
    # 세그먼트 인덱스 → 그 세그먼트의 moof만 읽어 해석한 결과. 읽은 것만 들어 있다
    segments: dict[int, Fmp4Segment] = field(default_factory=dict)
    # 구간을 해석한 쪽이 정한 프레임률(core.utils.fmp4_sections.choose_frame_rate). 구간의
    # 시각을 이 값으로 만들었다 — 엔진이 같은 값으로 검증한다. None이면 엔진이 정한다
    frame_rate: Fraction | None = None


@dataclass(frozen=True)
class Fmp4Index:
    """fMP4 세그먼트(들)의 프레임 시각·키프레임을 담는다.

    시각의 기준은 VOD 시작 = 0이다 — 편집 목록을 적용한 표시 시각에서 첫 세그먼트의
    가장 이른 시각을 뺀 값(초). ``frame_pts``는 오름차순이라
    ``core.utils.timecode.snap_to_frame``에 그대로 넣는다.
    """

    frame_pts: tuple[float, ...]  # 표시되는 영상 프레임의 PTS(초), 표시 순서
    frame_samples: tuple[int, ...]  # frame_pts와 같은 순서로, 그 프레임의 디코드 순서 인덱스
    keyframes: tuple[int, ...]  # 키프레임인 프레임의 번호(frame_pts의 인덱스, 오름차순)
    decode_times: tuple[float, ...]  # 영상 샘플의 DTS(초), 디코드 순서
    audio_pts: tuple[float, ...]  # 오디오 샘플의 PTS(초), 나온 순서
