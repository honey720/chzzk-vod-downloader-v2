"""TS 세그먼트로 받은 구간을 잘라 파일로 만든다 — mp4로 다시 싼 뒤 하이브리드 컷에 넘긴다 (#309).

하이브리드 컷(``core/utils/hybrid_cut.py``)의 입력은 mp4다. MPEG-TS에는 색인이 없어 ffmpeg의
탐색이 정확하지 않으므로, 구간의 세그먼트(복호화한 것)를 전체 다운로드의 후처리와 같은
방식(``remux_stream`` — 스트림 복사)으로 mp4로 다시 싸고 그 파일을 자른다. 컷과 판정은
mp4 경로의 것을 그대로 쓴다.

**구간 계획과 컷은 시각이 아니라 프레임 번호로 잇는다.** 구간 계획은 TS의 PTS로 첫·끝
프레임을 정했는데, 다시 싼 mp4의 시각은 TS와 조금 다르다 — ffmpeg는 영상이 오디오보다
늦게 시작하는 만큼을 mp4의 빈 편집 구간으로 적고, 그 길이를 1/1000초 단위로 내림한다.
그래서 mp4의 영상 시각이 TS보다 1ms 미만으로 앞선다. 시각을 등호로 견주면 프레임을 찾지
못한다. 이은 세그먼트의 n번째 영상 프레임(표시 순서)이 다시 싼 mp4의 n번째 프레임이므로
번호로 잇는다.

번호로 이으려면 두 쪽의 프레임이 같은 것이어야 한다. 다시 싼 mp4의 프레임 수 · 키프레임
목록이 TS와 같은지, 프레임마다의 시각 차이가 일정한지 확인하고, 다르면 자르지 않고
실패한다 — 다른 프레임을 조용히 자르지 않는다.
"""

import os
from bisect import bisect_left
from collections.abc import Sequence

from core.api.mp4 import Mp4Error, read_mp4_index
from core.models.cut import CutFrames, CutResult
from core.utils.ffmpeg import FFmpegError, read_in_chunks, remux_stream
from core.utils.hybrid_cut import CUT_FAILED, CutError, cut_frames_from_mp4, hybrid_cut

# 구간 계획이 정한 PTS를 같은 세그먼트에서 다시 읽은 PTS와 견줄 때 허용하는 차이(초) — 같은
# 계산을 두 번 한 값이라 같아야 하고, float 오차만 흡수한다
_PTS_TOLERANCE = 1e-6

# 다시 싼 mp4와 TS의 프레임마다 시각 차이가 서로 벗어나도 되는 폭(초) — 90kHz 2틱.
# 두 쪽 모두 틱 단위라 차이는 프레임마다 같아야 한다
_OFFSET_SPREAD = 2 / 90_000


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
    segment_paths: Sequence[str],
    ts_frames: CutFrames,
    first_pts: float,
    last_pts: float,
    output_path: str,
    joined_path: str,
    *,
    inspect: bool = False,
) -> tuple[CutResult, CutFrames]:
    """받아 둔 TS 세그먼트에서 구간 하나를 잘라 output_path에 mp4로 쓴다.

    세그먼트를 순서대로 이어 joined_path에 mp4로 다시 싸고(스트림 복사), 그 파일의 프레임
    [첫 프레임 번호, 끝 프레임 번호]를 ``hybrid_cut``으로 자른다. 프레임 번호는
    ``ts_frames``에서 first_pts · last_pts를 찾은 번호다. 다시 싼 파일은 성공하든 실패하든
    지운다.

    Args:
        segment_paths: 구간의 세그먼트 파일(복호화한 MPEG-TS) — 이어지는 순서대로
        ts_frames: 그 세그먼트들을 읽은 프레임 정보
            (``core.utils.ts_sections.TsSectionSource.frames_of``). 시각은 VOD 시각이다
        first_pts: 구간 첫 프레임의 PTS(VOD 시각, 초) — 구간 계획이 정한 값
        last_pts: 구간 끝 프레임의 PTS(VOD 시각, 초)
        output_path: 만들 파일. 이미 있으면 덮어쓴다
        joined_path: 다시 싼 mp4를 둘 임시 경로
        inspect: True면 조각마다 파라미터·패킷 수를 읽어 결과에 싣는다
            (``core.utils.cut_check.check_cut``이 쓴다)

    Returns:
        (컷 결과, 다시 싼 mp4의 프레임 정보). 판정(``check_cut``)에는 뒤의 것을 넘긴다 —
        컷이 그 파일을 원본으로 삼았다

    Raises:
        CutError: 첫·끝 프레임이 받은 세그먼트에 없거나, 다시 싸지 못했거나, 다시 싼 mp4의
            프레임이 TS와 다르거나, 컷이 실패한 경우
    """
    first = ts_frame_number(ts_frames, first_pts)
    last = ts_frame_number(ts_frames, last_pts)
    try:
        frames = _remux(segment_paths, joined_path)
        _require_same_frames(ts_frames, frames)
        result = hybrid_cut(joined_path, frames, first, last, output_path, inspect=inspect)
    finally:
        if os.path.exists(joined_path):
            os.remove(joined_path)
    return result, frames


def _remux(segment_paths: Sequence[str], joined_path: str) -> CutFrames:
    """세그먼트를 순서대로 ffmpeg에 흘려 mp4로 다시 싸고, 그 파일의 프레임 정보를 읽는다.

    Raises:
        CutError: 다시 싸지 못했거나 다시 싼 파일의 색인을 읽지 못한 경우(``CUT_FAILED``)
    """

    def feed():
        for path in segment_paths:
            yield from read_in_chunks(path)

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
