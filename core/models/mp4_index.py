"""mp4 샘플 표에서 뽑은 색인 모델 (#178).

인코딩 완료 VOD는 mp4 파일 하나로 내려온다. 그 파일의 moov에는 샘플마다
시각·크기·파일 위치가 적혀 있어, 받기 전에 프레임 시각과 받을 바이트 범위를
정확히 계산할 수 있다. 이 모듈은 그 계산의 입력과 출력을 담는 불변 모델이다 —
해석은 ``core.api.mp4``, 구간 → 바이트 범위 계산은 ``core.utils.mp4_ranges``가 한다.

시각의 기준: 편집 목록(elst)을 적용한 표시 시각이고, 영상·오디오를 통틀어 가장
먼저 표시되는 샘플의 시각이 0이다.
"""

from dataclasses import dataclass
from fractions import Fraction


@dataclass(frozen=True)
class Mp4Track:
    """트랙 하나의 샘플 표를 담는다.

    모든 튜플은 길이가 같고 **디코드 순서**(파일에 적힌 샘플 순서)다.
    """

    timescale: int  # 이 트랙의 초당 틱 수 (mdhd)
    times: tuple[float, ...]  # 샘플별 표시 시각(초). 편집 목록이 가린 샘플은 음수다
    decode_times: tuple[float, ...]  # 샘플별 DTS(초). times와 같은 기준(VOD 시작 = 0)이다
    durations: tuple[float, ...]  # 샘플별 길이(초)
    offsets: tuple[int, ...]  # 샘플별 파일 안 시작 위치(바이트)
    sizes: tuple[int, ...]  # 샘플별 크기(바이트)
    # 청크마다의 첫 샘플 인덱스(오름차순). 청크는 파일 안에 이어 붙어 놓인 샘플 묶음이고
    # stco/co64는 청크의 위치만 적는다 — 받을 범위를 청크 단위로 맞출 때 쓴다
    chunk_starts: tuple[int, ...]
    sync_samples: tuple[int, ...]  # 단독으로 디코드를 시작할 수 있는 샘플의 인덱스(오름차순)


@dataclass(frozen=True)
class Mp4Index:
    """mp4 한 파일의 프레임 시각·키프레임·길이와 트랙별 샘플 표를 담는다.

    ``frame_pts``는 오름차순이라 ``core.utils.timecode.snap_to_frame``에 그대로 넣는다.
    """

    frame_pts: tuple[float, ...]  # 표시되는 영상 프레임의 PTS(초), 표시 순서
    frame_samples: tuple[int, ...]  # frame_pts와 같은 순서로, 그 프레임의 video 샘플 인덱스
    keyframes: tuple[int, ...]  # 키프레임인 프레임의 번호(frame_pts의 인덱스, 오름차순)
    duration: float  # 영상 길이(초) — 마지막으로 표시되는 영상 프레임이 끝나는 시각
    fps: Fraction  # 샘플 표가 선언한 프레임률 — timescale ÷ 가장 많은 샘플 길이
    video: Mp4Track  # 영상 트랙
    audio: Mp4Track | None  # 오디오 트랙. 없으면 None
    # 파일 안에서 moov가 놓인 (시작, 끝) 바이트 — 양 끝 포함. 파일에서 찾아 읽었을 때만
    # 채운다(read_mp4_index · fetch_mp4_index). moov bytes만 해석했으면 None이다
    moov_range: tuple[int, int] | None = None


@dataclass(frozen=True)
class SelectionBytes:
    """구간 하나를 받는 데 필요한 파일 바이트 범위를 담는다."""

    # 받을 바이트 범위 목록 — (시작, 끝) 양 끝 포함. file 다운로더의 items와 같은 표현이다
    ranges: tuple[tuple[int, int], ...]
    total_size: int  # ranges의 바이트 수 합
    keyframe: int  # 범위가 시작하는 키프레임의 프레임 번호
    first_frame: int  # 구간의 첫 프레임 번호
    last_frame: int  # 구간의 끝 프레임 번호 (포함)
