"""TS 세그먼트에서 뽑은 색인 모델과 구간 → 세그먼트 범위 모델 (#309).

HLS로 내려오는 VOD는 세그먼트 파일의 목록이다. 구간만 받으려면 플레이리스트의
세그먼트 길이로 받을 세그먼트를 고르고(``SegmentSpan``), 받은 TS 세그먼트에서
프레임 시각과 키프레임을 읽어야 한다(``TsStreams`` → ``TsIndex``).

해석은 ``core.api.mpegts``, 구간 → 세그먼트 계산은 ``core.utils.hls_ranges``가 한다.
"""

from dataclasses import dataclass


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


@dataclass(frozen=True)
class SegmentSpan:
    """구간 하나를 받는 데 필요한 세그먼트 인덱스 범위를 담는다. 양 끝 포함이다."""

    first: int  # 받을 첫 세그먼트 — cover_first보다 하나 앞이다(앞에 세그먼트가 있으면)
    last: int  # 받을 마지막 세그먼트
    cover_first: int  # 구간의 시작 시각이 놓인 세그먼트
    cover_last: int  # 구간의 끝 시각이 놓인 세그먼트
