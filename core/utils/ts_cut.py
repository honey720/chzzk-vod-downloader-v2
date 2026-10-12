"""TS 세그먼트로 받은 구간을 구간 파일로 만든다 — 경계만 다시 인코딩하고 가운데는 그대로 흘린다 (#309).

암호화 VOD는 MPEG-TS 세그먼트(복호화한 것)로 받는다. 구간 파일을 쓰는 절차는 fMP4 구간과 같다
(``core/utils/fmp4_cut.py``의 ``cut_from_segments``): 영상은 구간 경계가 걸린 GOP만 다시
인코딩하고, 경계 세그먼트의 나머지 GOP는 복사, 가운데 세그먼트는 변환 ffmpeg를 거쳐 잇는
ffmpeg로 넘긴다. 오디오는 구간의 세그먼트를 한 번 흘려 복사로 뽑은 뒤 구간 시작 ~ 끝으로 잘라
넣는다. 여기에는 TS라서 다른 것만 있다.

- **경계 세그먼트는 작은 mp4로 다시 싼다.** MPEG-TS에는 색인이 없어 ffmpeg의 탐색이 정확하지
  않다. 경계 조각을 만들 세그먼트 하나만 스트림 복사로 mp4로 다시 싸고 그 파일에서 자른다
- **구간 계획과 컷은 시각이 아니라 프레임 번호로 잇는다.** 구간 계획은 TS의 PTS로 첫 · 끝
  프레임을 정했는데, 다시 싼 mp4의 시각은 TS와 조금 다르다 — ffmpeg는 영상이 오디오보다 늦게
  시작하는 만큼을 mp4의 빈 편집 구간으로 적고, 그 길이를 1/1000초 단위로 내림한다. 세그먼트의
  n번째 영상 프레임(표시 순서)이 다시 싼 mp4의 n번째 프레임이므로 번호로 잇는다. 그러려면 두
  쪽의 프레임이 같은 것이어야 한다 — 다시 싼 mp4의 프레임 수 · 키프레임 목록이 TS와 같은지,
  프레임마다의 시각 차이가 일정한지 확인하고, 다르면 자르지 않고 실패한다
- **뽑아 둔 오디오의 기준은 첫 오디오 덩어리다.** 오디오만 뽑은 파일의 0초는 넘긴 첫
  세그먼트의 첫 오디오 덩어리이고, 영상은 그보다 조금 늦게 시작할 수 있다(실제 영상에서 최대
  10.7ms). 오디오를 자르는 시각은 TS의 PTS에서 센다 — 다시 싼 mp4의 시각은 내림돼 있다

같은 구간을 세그먼트 전부를 이어 다시 싼 뒤 하이브리드 컷으로 자른 것과 영상 패킷 · DTS · PTS ·
디코드한 프레임이 같음을 실제 세그먼트로 확인했다. 다른 것은 구간 끝이 세그먼트의 끝과 맞을
때의 마지막 GOP뿐이다(원본 그대로다).
"""

import dataclasses
import os
import time
from bisect import bisect_left
from collections.abc import Callable, Sequence
from itertools import accumulate

from core.api.mp4 import Mp4Error, read_mp4_index
from core.models.cut import CutFrames, CutResult
from core.models.ts_index import TsStreams
from core.utils import hybrid_cut as hc
from core.utils.ffmpeg import FFmpegError, read_in_chunks, remux_stream
from core.utils.fmp4_cut import SegmentSource, cut_from_segments
from core.utils.hybrid_cut import (
    CUT_FAILED,
    CutCancelled,
    CutError,
    cut_frames_from_mp4,
)

# 구간 계획이 정한 PTS를 같은 세그먼트에서 다시 읽은 PTS와 견줄 때 허용하는 차이(초) — 같은
# 계산을 두 번 한 값이라 같아야 하고, float 오차만 흡수한다
_PTS_TOLERANCE = 1e-6

# 다시 싼 mp4와 TS의 프레임마다 시각 차이가 서로 벗어나도 되는 폭(초) — 90kHz 2틱.
# 두 쪽 모두 틱 단위라 차이는 프레임마다 같아야 한다
_OFFSET_SPREAD = 2 / 90_000


# 다시 싸는 동안 일시정지가 풀렸는지 확인하는 간격(초) — ffmpeg 실행 쪽의 확인 간격과 같은 크기다
_PAUSE_POLL_SECONDS = 0.05

_AAC_FRAME_SAMPLES = 1024  # AAC 프레임 하나의 표본 수 — 오디오 덩어리 하나의 길이를 센다


def ts_frame_number(frames: CutFrames, pts: float) -> int:
    """PTS가 pts인 프레임의 번호(``frames.frame_pts``의 인덱스)를 찾는다.

    구간 계획이 정한 첫·끝 프레임의 PTS를, 같은 세그먼트를 읽은 프레임 정보에서 번호로
    바꾼다. 두 값은 같은 TS에서 읽은 것이라 같아야 한다.

    Raises:
        CutError: 그 PTS의 프레임이 없는 경우(``CUT_FAILED``)
    """
    position = bisect_left(frames.frame_pts, pts - _PTS_TOLERANCE)
    if position < len(frames.frame_pts) and abs(frames.frame_pts[position] - pts) <= _PTS_TOLERANCE:
        return position
    raise CutError(CUT_FAILED, f"받은 세그먼트에 PTS {pts:.6f}초인 프레임이 없다")


def cut_ts_section(
    work_dir: str,
    segments: Sequence[TsStreams],
    ts_frames: CutFrames,
    segment_paths: Sequence[str],
    output_path: str,
    first_pts: float,
    last_pts: float,
    *,
    on_fed: Callable[[int], None] | None = None,
    inspect: bool = False,
    on_stage: Callable[[str, float], None] | None = None,
    on_progress: Callable[[float], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    should_pause: Callable[[], bool] | None = None,
) -> tuple[CutResult, CutFrames]:
    """받아 둔 TS 세그먼트에서 구간 하나를 output_path에 mp4로 쓴다.

    Args:
        work_dir: 조각 파일 · 오디오 임시 파일 · 다시 싼 경계 세그먼트를 둘 폴더. 비우고 쓰고,
            끝나면 지운다
        segments: 구간의 세그먼트마다의 ``parse_ts`` 결과 — segment_paths와 같은 순서 · 같은 개수
        ts_frames: 그 세그먼트들을 읽은 프레임 정보
            (``core.utils.ts_sections.TsSectionSource.frames_of``). 시각은 VOD 시각이다
        segment_paths: 구간의 세그먼트 파일(복호화한 MPEG-TS) — 이어지는 순서대로
        output_path: 만들 구간 파일. 실패하면 남기지 않는다
        first_pts: 구간 첫 프레임의 PTS(VOD 시각, 초) — 구간 계획이 정한 값
        last_pts: 구간 끝 프레임의 PTS(VOD 시각, 초)
        on_fed: 영상을 잇는 통과에서 세그먼트 하나를 다 넘길 때마다 그 자리(segment_paths의
            번호)로 부른다 — 부르는 쪽이 더 쓰이지 않는 세그먼트를 지운다
        inspect · on_stage · on_progress · should_stop · should_pause:
            ``core.utils.fmp4_cut.cut_fmp4_section``과 같다

    Returns:
        (컷 결과, ts_frames). 결과의 프레임 번호는 넘긴 세그먼트들 기준이다

    Raises:
        CutError: 첫 · 끝 프레임이 받은 세그먼트에 없거나, 이어지는 세그먼트가 키프레임으로
            시작하지 않거나(``CUT_UNSUPPORTED``), 경계 세그먼트를 다시 싸지 못했거나, 다시 싼
            mp4의 프레임이 TS와 다르거나, ffmpeg가 실패한 경우
        CutCancelled: should_stop이 참을 돌려줘 그만둔 경우
    """
    paths = list(segment_paths)
    if len(segments) != len(paths):
        raise ValueError(f"세그먼트 파일 {len(paths)}개에 읽은 정보 {len(segments)}개")
    counts = [len(streams.video_pts) for streams in segments]
    offsets = [0, *accumulate(counts)]

    def boundary(name: str, position: int) -> tuple[str, CutFrames]:
        # 경계 세그먼트 하나만 다시 싼다 — 그 파일의 n번째 프레임이 세그먼트의 n번째 프레임이다
        path = os.path.join(work_dir, name)
        frames = _remux([paths[position]], path, hc._stop_check.get(), hc._pause_check.get())
        _require_same_frames(
            _frames_of_segment(ts_frames, offsets[position], counts[position]), frames
        )
        return path, frames

    def load() -> SegmentSource:
        rate = next((s.audio_sample_rate for s in reversed(segments) if s.audio_sample_rate), None)
        return SegmentSource(
            frames=ts_frames,
            frame_counts=counts,
            key_starts=[
                not streams.video_pts
                or bool(streams.video_keyframes and streams.video_keyframes[0] == 0)
                for streams in segments
            ],
            prefix=b"",
            boundary=boundary,
            # 오디오만 뽑은 파일의 0초 = 넘긴 세그먼트들의 첫 오디오 덩어리
            audio_origin=ts_frames.audio_start,
            # 표본화율을 읽지 못한 오디오(ADTS가 아닌 것)는 덩어리의 길이를 모른다 — 0으로 둔다
            audio_overhang=_AAC_FRAME_SAMPLES / rate if rate else 0.0,
        )

    return cut_from_segments(
        work_dir,
        load,
        first_pts,
        last_pts,
        paths,
        output_path,
        on_fed=on_fed,
        inspect=inspect,
        on_stage=on_stage,
        on_progress=on_progress,
        should_stop=should_stop,
        should_pause=should_pause,
    )


def _frames_of_segment(ts_frames: CutFrames, first: int, count: int) -> CutFrames:
    """ts_frames에서 세그먼트 하나의 몫 — 프레임 번호 [first, first + count)를 0부터로 옮긴다."""
    return dataclasses.replace(
        ts_frames,
        frame_pts=tuple(ts_frames.frame_pts[first : first + count]),
        frame_dts=tuple(ts_frames.frame_dts[first : first + count]),
        keyframes=tuple(key - first for key in ts_frames.keyframes if first <= key < first + count),
    )


def _remux(
    segment_paths: Sequence[str],
    joined_path: str,
    should_stop: Callable[[], bool] | None = None,
    should_pause: Callable[[], bool] | None = None,
) -> CutFrames:
    """세그먼트를 순서대로 ffmpeg에 흘려 mp4로 다시 싸고, 그 파일의 프레임 정보를 읽는다.

    Raises:
        CutError: 다시 싸지 못했거나 다시 싼 파일의 색인을 읽지 못한 경우(``CUT_FAILED``)
        CutCancelled: 흘려 넣는 도중 멈추라는 요청이 온 경우 — ffmpeg는 끝내고 쓰다 만 파일은 지운다
    """

    def feed():
        for path in segment_paths:
            for chunk in read_in_chunks(path):
                while should_pause is not None and should_pause():
                    if should_stop is not None and should_stop():
                        break
                    time.sleep(_PAUSE_POLL_SECONDS)  # 일시정지 — 흘려 넣기를 멈추고 기다린다
                if should_stop is not None and should_stop():
                    raise CutCancelled("멈추라는 요청으로 다시 싸기를 그만뒀다")
                yield chunk

    try:
        remux_stream(feed(), joined_path)
    except (FFmpegError, OSError) as e:
        raise CutError(CUT_FAILED, f"세그먼트를 mp4로 다시 싸지 못했다: {e}") from e

    def read(offset: int, size: int) -> bytes:
        with open(joined_path, "rb") as f:
            f.seek(offset)
            return f.read(size)

    try:
        return cut_frames_from_mp4(read_mp4_index(read))
    except Mp4Error as e:
        raise CutError(CUT_FAILED, f"다시 싼 mp4의 색인을 읽지 못했다: {e}") from e


def _require_same_frames(ts_frames: CutFrames, frames: CutFrames) -> None:
    """다시 싼 mp4의 프레임이 TS에서 읽은 프레임과 같은 것인지 확인한다.

    프레임 수와 키프레임 목록이 같아야 하고, 프레임마다의 (TS 시각 − mp4 시각)이 모두 같아야
    한다. 차이의 크기는 묻지 않는다 — TS 쪽은 VOD 시각이고 mp4 쪽은 그 파일의 시각이다.

    Raises:
        CutError: 다른 경우(``CUT_FAILED``) — 프레임 번호로 이을 수 없다
    """
    if len(frames.frame_pts) != len(ts_frames.frame_pts):
        raise CutError(
            CUT_FAILED,
            f"다시 싼 mp4의 프레임 수 {len(frames.frame_pts)}가 TS의 {len(ts_frames.frame_pts)}와 다르다",
        )
    if tuple(frames.keyframes) != tuple(ts_frames.keyframes):
        raise CutError(CUT_FAILED, "다시 싼 mp4의 키프레임 목록이 TS와 다르다")
    offsets = [ts - mp4 for ts, mp4 in zip(ts_frames.frame_pts, frames.frame_pts)]
    if max(offsets) - min(offsets) > _OFFSET_SPREAD:
        raise CutError(CUT_FAILED, "다시 싼 mp4의 프레임 시각이 TS와 같은 간격이 아니다")
