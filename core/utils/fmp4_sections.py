"""구간 → 받을 세그먼트와 잘라 낼 프레임 (HLS fMP4) (#309).

인코딩 전 다시보기는 세그먼트로 나뉘어 있다. 구간마다 어느 세그먼트를 받을지와, 그
구간의 첫·끝 프레임이 어느 것인지를 정한다. 구간의 양 끝이 든 세그먼트를 읽어(moof)
실제 프레임을 고른다.

시각의 기준은 세그먼트의 실제 시각이다 — VOD의 0초는 첫 세그먼트의 가장 이른 PTS이고,
영상 길이는 마지막 영상 프레임이 끝나는 시각이다(``fmp4_timeline``). 플레이리스트에
``#EXT-X-DISCONTINUITY``가 있으면 그 뒤의 타임스탬프는 앞과 이어지지 않으므로, 끊긴 자리
뒤의 묶음은 앞 묶음이 끝난 VOD 시각에서 시작하는 것으로 놓는다. 끊긴 자리를 넘는 구간은
받지 않는다.

플레이리스트가 말하는 시각(``#EXTINF``의 누적)은 실제 시각과 벌어질 수 있다. 그래서
플레이리스트로는 세그먼트를 **추정**만 하고, 추정한 세그먼트를 읽어 실제 시각으로 확인한
뒤 어긋나 있으면 다시 추정해 찾아간다(``_Locator``). 정해진 횟수 안에 못 찾으면 실패한다 —
틀린 프레임을 조용히 고르지 않는다. 찾은 뒤에도 고른 세그먼트에 필요한 것이 다 들어
있는지 확인하고, 모자라면 세그먼트를 하나씩 더 넣는다.

- 시작 쪽: 입력에 (첫 프레임 − ``SOURCE_LEAD_SECONDS``)를 덮는 키프레임이 있어야 하고,
  오디오도 그만큼 앞에서 시작해야 한다(컷이 구간 시작보다 앞에서 읽기 시작한다)
- 끝 쪽: 끝 프레임이 있어야 하고, 오디오가 끝 프레임이 끝나는 시각까지 있어야 한다

구간의 끝이 영상 길이와 같으면(``reaches_end``) 끝 프레임은 시각으로 고르지 않고 마지막
세그먼트의 마지막 프레임이다. 플레이리스트의 길이가 마지막 프레임의 PTS보다 짧은 영상이
있어, 시각으로 고르면 그 프레임에 닿지 못한다.

프레임률(``choose_frame_rate``)은 타임코드의 FF 칸과 구간 검증의 프레임 단위를 정한다.
마스터 플레이리스트가 선언한 값이 있으면 그것을, 없으면 읽은 프레임의 평균 간격을 쓴다.
가장 많은 샘플 길이로 정하지 않는다 — timescale이 1000인 60fps 영상은 프레임 간격이
17 · 17 · 16ms로 돌아, 그렇게 정하면 1000/17(58.8fps)이 되고 FF 59가 다음 초로 넘어간다.

세그먼트의 프레임 정보는 주입받은 함수로 읽는다 — 이 모듈은 네트워크를 모른다.
"""

import math
from bisect import bisect_right
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from itertools import accumulate

from core.api.fmp4 import _seconds, build_fmp4_index, fmp4_origin
from core.api.hls import HlsPlaylist
from core.api.mp4 import MP4_INVALID, Mp4Error
from core.models.cut import CutFrames
from core.models.fmp4_index import Fmp4Init, Fmp4Segment
from core.models.plan import TimeRange
from core.utils.hybrid_cut import SOURCE_LEAD_SECONDS, cut_frames_from_fmp4
from core.utils.selections import (
    SELECTION_CROSSES_BREAK,
    SELECTION_NOT_LOCATED,
    SelectionError,
    reaches_end,
    validate_selections,
)
from core.utils.timecode import snap_to_frame

# 양 끝에서 세그먼트를 더 넣어 보는 최대 횟수 — 플레이리스트의 시각과 실제 PTS의 차이는
# 세그먼트 하나를 넘지 않는 것이 정상이다. 넘으면 끝없이 넓히지 않고 있는 것으로 정한다
_MAX_WIDEN_STEPS = 3

# 구간의 시각이 놓인 세그먼트를 찾아가는 최대 걸음 수. 걸음마다 세그먼트 하나를 통째로
# 받는다. 추정이 세그먼트 여럿만큼 어긋나도 그 세그먼트의 실제 시작과 길이로 다시
# 추정하면 한두 걸음에 닿는다 — 이 횟수로 못 찾으면 플레이리스트와 세그먼트가 맞지 않는
# 것이라 실패로 끝낸다
_MAX_LOCATE_STEPS = 6

# 세그먼트의 실제 길이로 나눌 때의 하한(초) — 프레임이 하나뿐인 세그먼트에서 0으로 나누지 않게 한다
_MIN_SPAN_SECONDS = 0.001

# 프레임률을 정한 경로
FPS_DECLARED = "declared"  # 마스터 플레이리스트의 FRAME-RATE
FPS_STANDARD = "standard"  # 잰 평균 간격이 표준 비율과 맞았다
FPS_MEASURED = "measured"  # 잰 평균 간격 그대로

# 잰 프레임률을 견주는 표준 비율
_STANDARD_FRAME_RATES = (
    Fraction(24000, 1001),
    Fraction(24),
    Fraction(25),
    Fraction(30000, 1001),
    Fraction(30),
    Fraction(50),
    Fraction(60000, 1001),
    Fraction(60),
)

# 잰 프레임률을 표준 비율로 보는 상대 오차의 상한 — 0.1%. 60과 60000/1001의 차이가 꼭
# 이만큼이라, 둘 다 범위에 들면 더 가까운 쪽을 고른다
_STANDARD_TOLERANCE = Fraction(1, 1000)

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


@dataclass(frozen=True)
class FrameRateChoice:
    """정한 프레임률과, 그것을 어느 경로로 정했는지를 담는다."""

    rate: Fraction  # 프레임률
    source: str  # 정한 경로 — FPS_DECLARED · FPS_STANDARD · FPS_MEASURED


def choose_frame_rate(
    init: Fmp4Init, segments: Sequence[Fmp4Segment], declared: Fraction | None = None
) -> FrameRateChoice:
    """프레임률을 정한다 — 선언값, 없으면 읽은 프레임의 평균 간격.

    1. ``declared``가 있으면 그 값 그대로다 (``FPS_DECLARED``)
    2. 없으면 segments의 영상 프레임 전체에서 (마지막 PTS − 첫 PTS) ÷ (프레임 수 − 1)로
       평균 간격을 재고, 그 프레임률이 표준 비율(24000/1001 · 24 · 25 · 30000/1001 · 30 ·
       50 · 60000/1001 · 60) 가운데 가장 가까운 것과 상대 오차 0.1% 안이면 그 표준
       비율이다 (``FPS_STANDARD``)
    3. 어느 표준 비율과도 맞지 않으면 잰 값 그대로다 (``FPS_MEASURED``)

    프레임이 하나뿐이면 간격을 잴 수 없어 그 프레임의 샘플 길이로 정한다(``FPS_MEASURED``).

    Args:
        init: 초기화 세그먼트
        segments: 프레임 간격을 잴 세그먼트. 보통 첫 세그먼트 하나다
        declared: 마스터 플레이리스트의 FRAME-RATE(선택한 해상도). 없으면 None

    Raises:
        ValueError: 선언값도 없고 segments에 영상 프레임도 없는 경우
    """
    if declared is not None and declared > 0:
        return FrameRateChoice(declared, FPS_DECLARED)
    timescale = init.video.timescale
    times = [pts for segment in segments for pts in segment.video.presentation_times]
    if len(times) >= 2 and max(times) > min(times):
        measured = Fraction((len(times) - 1) * timescale, max(times) - min(times))
        nearest = min(_STANDARD_FRAME_RATES, key=lambda rate: abs(measured - rate) / rate)
        if abs(measured - nearest) / nearest <= _STANDARD_TOLERANCE:
            return FrameRateChoice(nearest, FPS_STANDARD)
        return FrameRateChoice(measured, FPS_MEASURED)
    durations = Counter(d for segment in segments for d in segment.video.durations if d > 0)
    if not durations:
        raise ValueError("프레임률을 정할 수 없다 — 영상 프레임이 없다")
    return FrameRateChoice(Fraction(timescale, durations.most_common(1)[0][0]), FPS_MEASURED)


@dataclass(frozen=True)
class Fmp4Group:
    """플레이리스트에서 끊긴 자리 없이 이어지는 세그먼트 묶음 하나와, 그것이 놓인 VOD 시각을 담는다."""

    first: int  # 첫 세그먼트의 인덱스
    last: int  # 마지막 세그먼트의 인덱스 (포함)
    start: Fraction  # 이 묶음이 시작하는 VOD 시각(초) — 앞 묶음들의 실제 길이의 합
    end: Fraction  # 이 묶음의 마지막 영상 프레임이 끝나는 VOD 시각(초)
    # 시각의 기준 — (세그먼트의 원래 시각 − origin)이 VOD 시각이다
    origin: Fraction


@dataclass(frozen=True)
class Fmp4Timeline:
    """VOD의 시각 축 — 묶음마다의 시각과 영상 길이를 담는다. 세그먼트의 실제 시각으로 잰 값이다."""

    groups: tuple[Fmp4Group, ...]  # 플레이리스트 순서대로의 묶음
    duration: float  # 영상 길이(초) — 마지막 영상 프레임이 끝나는 VOD 시각


def fmp4_timeline(
    playlist: HlsPlaylist, init: Fmp4Init, segment_at: Callable[[int], Fmp4Segment]
) -> Fmp4Timeline:
    """세그먼트의 실제 시각으로 VOD의 시각 축과 영상 길이를 정한다.

    묶음(끊긴 자리 사이)마다 첫 세그먼트와 마지막 세그먼트를 읽는다. 묶음의 길이는
    (마지막 영상 프레임이 끝나는 시각 − 첫 세그먼트의 가장 이른 PTS)이고, 묶음은 앞
    묶음이 끝난 VOD 시각에서 시작한다. 영상 길이는 마지막 묶음이 끝나는 시각이다 —
    mp4의 ``Mp4Index.duration``(마지막 영상 프레임이 끝나는 시각)과 같은 뜻이다.

    ``#EXTINF``의 합을 길이로 쓰지 않는다. 실제 다시보기는 세그먼트마다 #EXTINF가 실제
    간격보다 1.3ms 길어, 11시간짜리에서 합이 실제 길이보다 26초 길었다.

    Raises:
        Mp4Error: 세그먼트를 해석하지 못했거나 표시되는 샘플이 없는 경우
        ValueError: 플레이리스트에 세그먼트가 없는 경우
    """
    if not playlist.segments:
        raise ValueError("플레이리스트에 세그먼트가 없다")
    bounds = [0, *playlist.discontinuities, len(playlist.segments)]
    groups = []
    start = Fraction(0)
    for first, after in zip(bounds, bounds[1:]):
        last = after - 1
        origin = fmp4_origin(init, segment_at(first)) - start
        tail = segment_at(_last_segment_with_video(segment_at, first, last))
        end = _original_span(init, tail)[1] - origin
        groups.append(Fmp4Group(first=first, last=last, start=start, end=end, origin=origin))
        start = end
    return Fmp4Timeline(groups=tuple(groups), duration=float(groups[-1].end))


def plan_fmp4_sections(
    playlist: HlsPlaylist,
    init: Fmp4Init,
    selections: Sequence[TimeRange],
    segment_at: Callable[[int], Fmp4Segment],
    fps: Fraction | None = None,
) -> tuple[Fmp4Section, ...]:
    """구간마다 받을 세그먼트 범위와 첫·끝 프레임을 정한다.

    Args:
        playlist: 미디어 플레이리스트 — 세그먼트 길이(#EXTINF)가 있어야 한다
        init: 초기화 세그먼트의 해석 결과
        selections: 구간 목록. 순서가 구간 번호다
        segment_at: 세그먼트 인덱스를 받아 그 세그먼트의 프레임 정보(moof 해석)를
            돌려주는 함수. 같은 인덱스로 여러 번 불릴 수 있다 — 주는 쪽이 보관한다
        fps: 구간을 해석한 쪽이 이미 정한 프레임률(``choose_frame_rate``). 구간의 시각을
            만든 프레임률과 같아야 검증이 같은 프레임 단위로 된다. None이면 첫
            세그먼트로 여기서 정한다

    Raises:
        SelectionError: 구간이 검증을 통과하지 못했거나(``validate_selections``의 키),
            녹화가 끊긴 자리를 넘거나(``SELECTION_CROSSES_BREAK``), 구간의 시각이 놓인
            세그먼트를 찾지 못한 경우(``SELECTION_NOT_LOCATED``)
        ValueError: 플레이리스트에 세그먼트 길이가 없는 경우
    """
    if not playlist.durations or len(playlist.durations) != len(playlist.segments):
        raise ValueError("플레이리스트에 세그먼트 길이(#EXTINF)가 없다")
    if not all(math.isfinite(duration) and duration > 0 for duration in playlist.durations):
        raise ValueError("길이를 알 수 없는 세그먼트가 있다 — #EXTINF가 없거나 0 이하다")
    if fps is None:
        fps = choose_frame_rate(init, [segment_at(0)]).rate
    timeline = fmp4_timeline(playlist, init, segment_at)
    violations = validate_selections(selections, timeline.duration, fps)
    if violations:
        raise SelectionError(violations)
    half_frame = float(1 / fps) / 2

    sections = []
    for number, selection in enumerate(selections):
        group = next(
            (g for g in timeline.groups if selection.start < float(g.end)), timeline.groups[-1]
        )
        to_end = reaches_end(selection.end, timeline.duration, fps)
        # 끊긴 자리를 넘는 구간은 그 구간의 세그먼트를 읽기 전에 거부한다
        crosses = selection.end > float(group.end) + half_frame
        if crosses or (to_end and group is not timeline.groups[-1]):
            raise SelectionError({number: (SELECTION_CROSSES_BREAK,)})
        origin = group.origin
        locate = _Locator(number, playlist, init, group, segment_at)

        def frames_of(first: int, last: int, origin: Fraction = origin) -> CutFrames:
            segments = [segment_at(index) for index in range(first, last + 1)]
            return cut_frames_from_fmp4(init, segments, build_fmp4_index(init, segments, origin))

        cover = locate(selection.start)
        first_segment = max(cover - 1, group.first)  # 앞 키프레임이 앞 세그먼트에 있을 수 있다
        lead = frames_of(first_segment, cover)
        for _ in range(_MAX_WIDEN_STEPS):
            first_frame = snap_to_frame(selection.start, lead.frame_pts, fps, "start")
            if (
                selection.start > lead.frame_pts[-1] + lead.frame_duration / 2
                and cover < group.last
            ):
                cover += 1  # 첫 프레임이 다음 세그먼트에 있다
            elif not _has_lead(lead, first_frame) and first_segment > group.first:
                first_segment -= 1  # 앞 키프레임이나 오디오가 더 앞 세그먼트에 있다
            else:
                break
            lead = frames_of(first_segment, cover)
        first_frame = snap_to_frame(selection.start, lead.frame_pts, fps, "start")

        if to_end:
            # 끝이 영상 길이와 같다 — 시각으로 고르지 않고 마지막 세그먼트의 마지막 프레임을 쓴다
            high = group.last
            low = _last_segment_with_video(segment_at, first_segment, high)
            tail = frames_of(low, high)
            last_frame = len(tail.frame_pts) - 1
        else:
            low = high = max(locate(selection.end), first_segment)
            tail = frames_of(low, high)
            for _ in range(_MAX_WIDEN_STEPS):
                last_frame = snap_to_frame(selection.end, tail.frame_pts, fps, "end")
                starts_later = selection.end < tail.frame_pts[0] - tail.frame_duration / 2
                if starts_later and low > first_segment:
                    low -= 1  # 끝 프레임이 앞 세그먼트에 있다
                elif not _has_tail(tail, last_frame, selection.end) and high < group.last:
                    high += 1  # 끝 프레임이나 그 프레임의 오디오가 다음 세그먼트에 있다
                else:
                    break
                tail = frames_of(low, high)
            last_frame = snap_to_frame(selection.end, tail.frame_pts, fps, "end")

        # 양 끝을 넓히는 걸음은 끊긴 자리에서 멈춘다(first_segment > group.first ·
        # cover < group.last · high < group.last) — 여기까지 온 범위는 끊긴 자리를 넘지 않는다
        sections.append(
            Fmp4Section(
                selection=selection,
                first_segment=first_segment,
                last_segment=max(high, cover),
                first_pts=lead.frame_pts[first_frame],
                last_pts=max(tail.frame_pts[last_frame], lead.frame_pts[first_frame]),
                origin=origin,
            )
        )
    return tuple(sections)


class _Locator:
    """한 묶음 안에서 VOD 시각이 놓인 세그먼트를 찾는다 — 추정한 뒤 실제 시각으로 확인한다.

    처음 추정은 플레이리스트로 한다: 묶음의 모든 세그먼트에 ``#EXT-X-PROGRAM-DATE-TIME``이
    있으면 첫 세그먼트와의 차이, 아니면 ``#EXTINF``의 누적. 추정한 세그먼트를 읽어 그
    세그먼트의 실제 시각 범위에 구하는 시각이 들어 있는지 본다. 없으면 그 세그먼트의 실제
    시작과 길이로 몇 세그먼트 떨어져 있는지 다시 추정해 옮겨 간다.
    """

    def __init__(
        self,
        number: int,
        playlist: HlsPlaylist,
        init: Fmp4Init,
        group: Fmp4Group,
        segment_at: Callable[[int], Fmp4Segment],
    ):
        self._number = number  # 구간 번호 — 실패를 어느 구간의 것으로 알릴지
        self._init = init
        self._group = group
        self._segment_at = segment_at
        times = playlist.program_times[group.first : group.last + 1]
        if len(times) == group.last - group.first + 1 and all(t is not None for t in times):
            offsets = [t - times[0] for t in times]
        else:
            offsets = [0.0, *accumulate(playlist.durations[group.first : group.last])]
        # 묶음 안 세그먼트마다 추정한 시작 VOD 시각 — 인덱스 0이 group.first다
        self._estimates = [float(group.start) + offset for offset in offsets]

    def __call__(self, seconds: float) -> int:
        """seconds가 놓인 세그먼트의 인덱스. 세그먼트 사이의 빈 자리면 그 뒤의 세그먼트다.

        Raises:
            SelectionError: ``_MAX_LOCATE_STEPS``번 안에 찾지 못한 경우(``SELECTION_NOT_LOCATED``)
        """
        group = self._group
        index = self._clamp(group.first + bisect_right(self._estimates, seconds) - 1)
        visited: set[int] = set()
        for _ in range(_MAX_LOCATE_STEPS):
            visited.add(index)
            begin, end = self._span(index)
            if seconds < begin and index > group.first:
                jump = min(int((seconds - begin) / max(end - begin, _MIN_SPAN_SECONDS)) - 1, -1)
            elif seconds >= end and index < group.last:
                jump = max(int((seconds - begin) / max(end - begin, _MIN_SPAN_SECONDS)), 1)
            else:
                return index
            moved = self._clamp(index + jump)
            if moved in visited:
                # 이웃을 오가고 있다 — 두 세그먼트 사이의 빈 자리다. 뒤의 것을 고른다
                return max(index, moved)
            index = moved
        raise SelectionError({self._number: (SELECTION_NOT_LOCATED,)})

    def _clamp(self, index: int) -> int:
        return min(max(index, self._group.first), self._group.last)

    def _span(self, index: int) -> tuple[float, float]:
        """세그먼트 index가 실제로 차지하는 VOD 시각 [시작, 끝)."""
        begin, end = _original_span(self._init, self._segment_at(index))
        return float(begin - self._group.origin), float(end - self._group.origin)


def _original_span(init: Fmp4Init, segment: Fmp4Segment) -> tuple[Fraction, Fraction]:
    """세그먼트의 영상이 차지하는 시각 [첫 PTS, 마지막 프레임이 끝나는 시각) — 원래 시각(초).

    영상 샘플이 없는 세그먼트는 오디오로 잰다.

    Raises:
        Mp4Error: 샘플이 하나도 없는 경우(``MP4_INVALID``)
    """
    for track, samples in ((init.video, segment.video), (init.audio, segment.audio)):
        if track is not None and samples.presentation_times:
            ends = (p + d for p, d in zip(samples.presentation_times, samples.durations))
            return (
                _seconds(track, min(samples.presentation_times)),
                _seconds(track, max(ends)),
            )
    raise Mp4Error(MP4_INVALID, "세그먼트에 샘플이 없다")


def _last_segment_with_video(
    segment_at: Callable[[int], Fmp4Segment], lowest: int, last: int
) -> int:
    """last에서 앞으로 가며 영상 샘플이 든 첫 세그먼트를 찾는다. lowest보다 앞으로는 가지 않는다.

    마지막 세그먼트에 오디오만 든 영상이 있다 — 그러면 마지막 프레임은 그 앞 세그먼트에 있다.
    """
    index = last
    while index > lowest and not segment_at(index).video.presentation_times:
        index -= 1
    return index


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
