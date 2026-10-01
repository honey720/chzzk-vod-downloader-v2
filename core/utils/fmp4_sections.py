"""구간 → 받을 세그먼트와 잘라 낼 프레임 (HLS fMP4) (#309).

인코딩 전 다시보기는 세그먼트로 나뉘어 있다. 구간마다 어느 세그먼트를 받을지와, 그
구간의 첫·끝 프레임이 어느 것인지를 세그먼트를 받기 전에 정한다. 플레이리스트의
세그먼트 길이(``#EXTINF``)로 세그먼트를 고르고, 구간의 양 끝이 든 세그먼트의 moof만
읽어 실제 프레임을 고른다.

시각의 기준은 플레이리스트의 시각이다 — VOD의 0초는 첫 세그먼트의 가장 이른 PTS다.
플레이리스트에 ``#EXT-X-DISCONTINUITY``가 있으면 그 뒤의 타임스탬프는 앞과 이어지지
않으므로, 끊긴 자리 뒤의 세그먼트는 그 자리의 첫 세그먼트를 기준으로 삼는다
(그 세그먼트의 가장 이른 PTS = 플레이리스트에서 그 세그먼트가 시작하는 시각).
끊긴 자리를 넘는 구간은 받지 않는다.

플레이리스트의 누적 시각과 세그먼트 안의 실제 PTS는 조금 다를 수 있다. 그래서
고른 세그먼트에 실제로 필요한 것이 들어 있는지 moof로 확인하고, 모자라면 세그먼트를
하나씩 더 넣는다.

- 시작 쪽: 입력에 (첫 프레임 − ``SOURCE_LEAD_SECONDS``)를 덮는 키프레임이 있어야 하고,
  오디오도 그만큼 앞에서 시작해야 한다(컷이 구간 시작보다 앞에서 읽기 시작한다)
- 끝 쪽: 끝 프레임이 있어야 하고, 오디오가 끝 프레임이 끝나는 시각까지 있어야 한다

세그먼트의 프레임 정보는 주입받은 함수로 읽는다 — 이 모듈은 네트워크를 모른다.
"""

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from itertools import accumulate

from core.api.fmp4 import build_fmp4_index, fmp4_origin
from core.api.hls import HlsPlaylist
from core.models.cut import CutFrames
from core.models.fmp4_index import Fmp4Init, Fmp4Segment
from core.models.plan import TimeRange
from core.utils.hls_ranges import selection_segments
from core.utils.hybrid_cut import SOURCE_LEAD_SECONDS, cut_frames_from_fmp4
from core.utils.selections import SELECTION_CROSSES_BREAK, SelectionError, validate_selections
from core.utils.timecode import snap_to_frame

# 양 끝에서 세그먼트를 더 넣어 보는 최대 횟수 — 플레이리스트의 시각과 실제 PTS의 차이는
# 세그먼트 하나를 넘지 않는 것이 정상이다. 넘으면 끝없이 넓히지 않고 있는 것으로 정한다
_MAX_WIDEN_STEPS = 3

# 오디오의 시작을 키프레임의 DTS와 견줄 때의 여유(초) — 오디오 샘플의 경계가 DTS와 같은
# 시각에 놓였을 때 float 오차로 "뒤"라고 판정하지 않게 한다
_AUDIO_SEEK_SLACK = 1e-6


@dataclass(frozen=True)
class Fmp4Section:
    """구간 하나를 받는 데 필요한 세그먼트와, 잘라 낼 프레임의 시각을 담는다."""

    selection: TimeRange  # 요청한 구간(초)
    first_segment: int  # 받을 첫 세그먼트의 인덱스
    last_segment: int  # 받을 마지막 세그먼트의 인덱스 (포함)
    first_pts: float  # 구간 첫 프레임의 PTS(플레이리스트 시각, 초)
    last_pts: float  # 구간 끝 프레임의 PTS(플레이리스트 시각, 초)
    # 시각의 기준 — (세그먼트의 원래 시각 − origin)이 플레이리스트 시각이다.
    # 받은 세그먼트로 색인을 다시 만들 때 build_fmp4_index에 넘긴다
    origin: Fraction

    @property
    def segment_count(self) -> int:
        """받을 세그먼트 수."""
        return self.last_segment - self.first_segment + 1


def frame_rate_of(init: Fmp4Init, segments: Sequence[Fmp4Segment]) -> Fraction:
    """세그먼트들의 영상 샘플 길이에서 프레임률을 구한다 — timescale ÷ 가장 많은 샘플 길이."""
    durations = Counter(d for segment in segments for d in segment.video.durations)
    return Fraction(init.video.timescale, durations.most_common(1)[0][0])


def plan_fmp4_sections(
    playlist: HlsPlaylist,
    init: Fmp4Init,
    selections: Sequence[TimeRange],
    segment_at: Callable[[int], Fmp4Segment],
) -> tuple[Fmp4Section, ...]:
    """구간마다 받을 세그먼트 범위와 첫·끝 프레임을 정한다.

    Args:
        playlist: 미디어 플레이리스트 — 세그먼트 길이(#EXTINF)가 있어야 한다
        init: 초기화 세그먼트의 해석 결과
        selections: 구간 목록. 순서가 구간 번호다
        segment_at: 세그먼트 인덱스를 받아 그 세그먼트의 프레임 정보(moof 해석)를
            돌려주는 함수. 같은 인덱스로 여러 번 불릴 수 있다 — 주는 쪽이 보관한다

    Raises:
        SelectionError: 구간이 검증을 통과하지 못했거나(``validate_selections``의 키),
            녹화가 끊긴 자리를 넘는 경우(``SELECTION_CROSSES_BREAK``)
        ValueError: 플레이리스트에 세그먼트 길이가 없는 경우
    """
    starts = [
        0.0,
        *accumulate(playlist.durations),
    ]  # starts[i] = 세그먼트 i의 플레이리스트 시작 시각
    fps = frame_rate_of(init, [segment_at(0)])
    violations = validate_selections(selections, playlist.duration, fps)
    if violations:
        raise SelectionError(violations)

    sections = []
    for number, selection in enumerate(selections):
        span = selection_segments(playlist, selection)
        group = _group_start(playlist, span.cover_first)
        # 끊긴 자리 뒤의 세그먼트는 그 자리의 첫 세그먼트를 기준으로 시각을 센다
        origin = fmp4_origin(init, segment_at(group)) - Fraction(starts[group])
        group_end = _group_end(playlist, group)

        def frames_of(first: int, last: int, origin: Fraction = origin) -> CutFrames:
            segments = [segment_at(index) for index in range(first, last + 1)]
            return cut_frames_from_fmp4(init, segments, build_fmp4_index(init, segments, origin))

        first_segment, cover = max(span.first, group), span.cover_first
        lead = frames_of(first_segment, cover)
        for _ in range(_MAX_WIDEN_STEPS):
            first_frame = snap_to_frame(selection.start, lead.frame_pts, fps, "start")
            if selection.start > lead.frame_pts[-1] + lead.frame_duration / 2 and cover < group_end:
                cover += 1  # 첫 프레임이 다음 세그먼트에 있다
            elif not _has_lead(lead, first_frame) and first_segment > group:
                first_segment -= 1  # 앞 키프레임이나 오디오가 더 앞 세그먼트에 있다
            else:
                break
            lead = frames_of(first_segment, cover)
        first_frame = snap_to_frame(selection.start, lead.frame_pts, fps, "start")

        low = high = span.cover_last
        tail = frames_of(low, high)
        for _ in range(_MAX_WIDEN_STEPS):
            last_frame = snap_to_frame(selection.end, tail.frame_pts, fps, "end")
            if selection.end < tail.frame_pts[0] - tail.frame_duration / 2 and low > first_segment:
                low -= 1  # 끝 프레임이 앞 세그먼트에 있다
            elif not _has_tail(tail, last_frame, selection.end) and high < group_end:
                high += 1  # 끝 프레임이나 그 프레임의 오디오가 다음 세그먼트에 있다
            else:
                break
            tail = frames_of(low, high)
        last_frame = snap_to_frame(selection.end, tail.frame_pts, fps, "end")

        last_segment = max(high, cover)
        if span.last > group_end or last_segment > group_end or first_segment < group:
            raise SelectionError({number: (SELECTION_CROSSES_BREAK,)})
        sections.append(
            Fmp4Section(
                selection=selection,
                first_segment=first_segment,
                last_segment=last_segment,
                first_pts=lead.frame_pts[first_frame],
                last_pts=max(tail.frame_pts[last_frame], lead.frame_pts[first_frame]),
                origin=origin,
            )
        )
    return tuple(sections)


def _group_start(playlist: HlsPlaylist, segment: int) -> int:
    """segment가 든 연속 구간의 첫 세그먼트 — 그 앞에서 마지막으로 끊긴 자리. 없으면 0."""
    before = [index for index in playlist.discontinuities if index <= segment]
    return before[-1] if before else 0


def _group_end(playlist: HlsPlaylist, group: int) -> int:
    """group에서 시작하는 연속 구간의 마지막 세그먼트."""
    after = [index for index in playlist.discontinuities if index > group]
    return (after[0] if after else len(playlist.segments)) - 1


def _has_lead(frames: CutFrames, first_frame: int) -> bool:
    """입력이 구간의 첫 프레임보다 충분히 앞에서 시작하는지 — 앞 키프레임과 오디오.

    컷은 (첫 프레임 − SOURCE_LEAD_SECONDS)의 앞 키프레임으로 가고, ffmpeg는 오디오를 그
    키프레임의 DTS에 맞춘다. 입력의 오디오가 그 DTS보다 뒤에서 시작하면 세그먼트 전부를
    이은 입력과 디코드를 시작하는 패킷이 달라진다 — 디코드 결과가 시작 위치에 따라 달라지는
    오디오가 있어(잡음 대체) 잘라 낸 오디오까지 달라진다.
    """
    wanted = frames.frame_pts[first_frame] - SOURCE_LEAD_SECONDS
    before = [key for key in frames.keyframes if frames.frame_pts[key] <= wanted]
    if not before:
        return False
    if frames.audio_start is None:
        return True
    return frames.audio_start <= frames.frame_dts[before[-1]] + _AUDIO_SEEK_SLACK


def _has_tail(frames: CutFrames, last_frame: int, end: float) -> bool:
    """입력에 구간의 끝 프레임과 그 프레임이 끝날 때까지의 오디오가 있는지."""
    if end > frames.frame_pts[-1] + frames.frame_duration / 2:
        return False  # 끝 프레임이 이 뒤에 있다
    frame_end = frames.frame_pts[last_frame] + frames.frame_duration
    return frames.audio_end is None or frames.audio_end >= frame_end
