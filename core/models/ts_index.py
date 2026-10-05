"""TS 세그먼트에서 뽑은 색인 모델과 구간 → 세그먼트 범위 모델 (#309).

HLS로 내려오는 VOD는 세그먼트 파일의 목록이다. 구간만 받으려면 플레이리스트의
세그먼트 길이로 받을 세그먼트를 고르고(``SegmentSpan``), 받은 세그먼트에서 프레임
시각과 키프레임을 읽어야 한다.

세그먼트의 형식은 경로마다 다르다. 암호화 VOD(hls_aes 경로)는 MPEG-TS이고, 라이브
다시보기(m3u8 경로)는 fMP4다. ``TsStreams`` · ``TsIndex``는 MPEG-TS, 곧 암호화 VOD
경로의 것이다. ``SegmentSpan``은 플레이리스트만 보므로 두 경로에 다 쓴다.

해석은 ``core.api.mpegts``, 구간 → 세그먼트 계산은 ``core.utils.hls_ranges``가 한다.
"""

from dataclasses import dataclass, field
from fractions import Fraction

from core.api.hls import HlsPlaylist


@dataclass(frozen=True)
class TsStreams:
    """TS bytes에서 읽은 그대로의 타임스탬프를 담는다.

    값은 90kHz 틱의 33비트 원시 값이다 — 0으로 맞추지도, 랩어라운드를 풀지도 않았다.
    영상의 튜플은 길이가 같고 **디코드 순서**(파일에 나온 PES 순서)다.
    """

    video_pts: tuple[int, ...]  # 영상 프레임별 PTS
    video_dts: tuple[int, ...]  # 영상 프레임별 DTS. PES에 DTS가 없으면 PTS와 같다
    video_keyframes: tuple[int, ...]  # 키프레임인 프레임의 인덱스(오름차순)
    audio_pts: tuple[int, ...]  # 오디오 PES별 PTS. PES 하나에 오디오 프레임이 여럿 들 수 있다
    # 오디오 PES마다 든 AAC 프레임 수 — audio_pts와 같은 순서·같은 개수다. ADTS가 아닌
    # 오디오이거나 PES 본문이 ADTS 머리로 시작하지 않으면 그 PES는 0이다. 직접 만든 객체는
    # 빈 튜플일 수 있다
    audio_frames: tuple[int, ...] = ()
    # 오디오 표본화율(Hz) — 프레임을 센 마지막 오디오 PES의 ADTS 머리에서 읽은 값. 모르면 None
    audio_sample_rate: int | None = None


@dataclass(frozen=True)
class TsIndex:
    """TS 세그먼트(들)의 프레임 시각·키프레임을 담는다.

    시각의 기준은 VOD 시작 = 0이다 — 첫 세그먼트의 가장 이른 PTS를 뺀 값(초).
    ``frame_pts``는 오름차순이라 ``core.utils.timecode.snap_to_frame``에 그대로 넣는다.
    """

    frame_pts: tuple[float, ...]  # 영상 프레임의 PTS(초), 표시 순서
    frame_samples: tuple[int, ...]  # frame_pts와 같은 순서로, 그 프레임의 디코드 순서 인덱스
    keyframes: tuple[int, ...]  # 키프레임인 프레임의 번호(frame_pts의 인덱스, 오름차순)
    decode_times: tuple[float, ...]  # 영상 프레임의 DTS(초), 디코드 순서
    audio_pts: tuple[float, ...]  # 오디오 PES의 PTS(초), 나온 순서
    # 프레임 하나의 길이(초) — 표시 순서로 이웃한 프레임의 PTS 간격 가운데 가장 많은 값.
    # TS에는 프레임 길이가 적혀 있지 않다. 프레임이 하나뿐이면 None
    frame_duration: float | None = None
    # 오디오가 끝나는 시각(초) — 마지막 오디오 PES의 PTS + 그 PES에 든 프레임의 길이.
    # 오디오가 없거나 마지막 PES의 프레임 수를 읽지 못했으면 None
    audio_end: float | None = None


@dataclass(frozen=True)
class SegmentSpan:
    """구간 하나를 받는 데 필요한 세그먼트 인덱스 범위를 담는다. 양 끝 포함이다."""

    first: int  # 받을 첫 세그먼트 — cover_first보다 하나 앞이다(앞에 세그먼트가 있으면)
    last: int  # 받을 마지막 세그먼트
    cover_first: int  # 구간의 시작 시각이 놓인 세그먼트
    cover_last: int  # 구간의 끝 시각이 놓인 세그먼트


@dataclass
class TsHead:
    """구간을 정하려고 받은 것을 담는다 — 플레이리스트와 세그먼트의 프레임 정보 (#309).

    암호화 VOD의 구간 다운로드는 받기 전에 프레임 시각을 알아야 한다. 그때 받은 것을 들고
    있다가 엔진이 그대로 쓴다 — 같은 것을 두 번 받지 않는다(``Fmp4Head``와 같은 역할이다).
    ``segments``는 받는 대로 채워지는 보관함이라 이 객체는 불변이 아니다.

    프레임 정보를 읽으려고 받은 세그먼트의 **복호화한** 본문은 메모리에 두지 않고
    ``segment_dir``에 파일로 둔다. 엔진은 그 폴더를 세그먼트 임시 폴더로 쓰고, ``stored``에 든
    세그먼트는 다시 받지 않는다.

    받아 둔 세그먼트는 그것을 받은 플레이리스트(해상도마다 다르다)에 묶인다 — ``playlist_ref``.
    구간을 해석한 뒤 해상도가 바뀌면 엔진은 받아 둔 것을 쓰지 않는다.

    **복호화 키는 담지 않는다.** 엔진은 키를 다시 받는다 — 키 값이 이 객체를 따라
    ``Content`` · repr · 로그로 나가지 않는다.
    """

    playlist: HlsPlaylist  # 미디어 플레이리스트
    # 세그먼트 인덱스 → 복호화한 그 세그먼트를 parse_ts로 읽은 결과. 읽은 것만 들어 있다
    segments: dict[int, TsStreams] = field(default_factory=dict)
    # 구간을 해석한 쪽이 정한 프레임률(core.utils.ts_sections.choose_ts_frame_rate). 구간의
    # 시각을 이 값으로 만들었다 — 엔진이 같은 값으로 계획한다. None이면 엔진이 정한다
    frame_rate: Fraction | None = None
    # 받은 세그먼트(복호화한 것)를 두는 폴더 — 엔진의 세그먼트 임시 폴더다. None이면 받은 본문을 버린다
    segment_dir: str | None = None
    # 이 객체가 segment_dir에 온전하게 받아 둔 세그먼트의 인덱스
    stored: set[int] = field(default_factory=set)
    # 이 플레이리스트를 가리키는 값 — 받은 주소에서 쿼리·프래그먼트를 뺀 것
    # (core.api.hls_ts.playlist_ref). 받아 둔 세그먼트는 이 플레이리스트의 것이다. 엔진은 자기가
    # 받을 플레이리스트와 이 값이 다르면 받아 둔 것을 쓰지 않는다. None이면 어느
    # 플레이리스트의 것인지 모른다 — 엔진이 쓰지 않는다
    playlist_ref: str | None = None
