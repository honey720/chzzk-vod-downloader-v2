"""구간 → 받을 세그먼트와 잘라 낼 프레임 — 세그먼트의 형식과 무관한 부분 (#309).

HLS로 내려오는 VOD는 세그먼트의 목록이다. 구간마다 어느 세그먼트를 받을지와, 그 구간의
첫·끝 프레임이 어느 것인지를 정한다. 세그먼트가 fMP4든 MPEG-TS든 정하는 방법은 같다 —
다른 것은 세그먼트에서 시각을 읽는 방법뿐이다. 그 부분은 공급자(``SectionSource``)가 맡고,
이 모듈은 공급자가 준 시각으로 계획만 세운다.

- fMP4(인코딩 전 다시보기)의 공급자: ``core/utils/fmp4_sections.py``
- MPEG-TS(암호화 VOD)의 공급자: ``core/utils/ts_sections.py``

시각의 기준은 세그먼트의 실제 시각이다 — VOD의 0초는 첫 세그먼트의 가장 이른 시각이고,
영상 길이는 마지막 영상 프레임이 끝나는 시각이다(``timeline``). 플레이리스트에
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
- 끝 쪽: 끝 프레임이 있어야 하고, 오디오가 끝 프레임이 끝나는 시각까지 있어야 한다.
  오디오가 있는데 어디서 끝나는지 모르면(``CutFrames.audio_end``가 None) 끝 프레임이 든
  세그먼트 다음의 세그먼트까지 넣는다 — 오디오가 없는 것으로 보지 않는다

구간의 끝이 영상 길이와 같으면(``reaches_end``) 끝 프레임은 시각으로 고르지 않고 마지막
세그먼트의 마지막 프레임이다. 플레이리스트의 길이가 마지막 프레임의 PTS보다 짧은 영상이
있어, 시각으로 고르면 그 프레임에 닿지 못한다.

이 모듈은 네트워크를 모른다. 세그먼트를 받는 일은 공급자에 주입된 함수가 한다.
"""

import math
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from itertools import accumulate
from typing import Literal, Protocol

from core.api.hls import HlsPlaylist
from core.models.cut import CutFrames
from core.models.plan import TimeRange
from core.utils.hybrid_cut import SOURCE_LEAD_SECONDS
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
FPS_DECLARED = "declared"  # 선언값 — 마스터 플레이리스트의 FRAME-RATE · 매니페스트의 frameRate
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

# 세그먼트의 "원래 시각"(초) — 공급자가 정한 기준에서 잰 값이다. 분수(fMP4)일 수도
# 소수(TS)일 수도 있다. (원래 시각 − 묶음의 origin)이 VOD 시각이다
Seconds = Fraction | float


@dataclass(frozen=True)
class FrameRateChoice:
    """정한 프레임률과, 그것을 어느 경로로 정했는지를 담는다."""

    rate: Fraction  # 프레임률
    source: str  # 정한 경로 — FPS_DECLARED · FPS_STANDARD · FPS_MEASURED


def measured_frame_rate(times: Sequence[int], timescale: int) -> FrameRateChoice | None:
    """프레임의 PTS에서 평균 간격을 재어 프레임률을 정한다.

    (마지막 PTS − 첫 PTS) ÷ (프레임 수 − 1)이 평균 간격이다. 그 프레임률이 표준 비율
    (24000/1001 · 24 · 25 · 30000/1001 · 30 · 50 · 60000/1001 · 60) 가운데 가장 가까운 것과
    상대 오차 0.1% 안이면 그 표준 비율이고(``FPS_STANDARD``), 아니면 잰 값
    그대로다(``FPS_MEASURED``).

    가장 많은 간격으로 정하지 않는다 — 틱으로 나누어떨어지지 않는 프레임률은 간격이 두
    값으로 번갈아 나와, 그렇게 정하면 실제와 다른 프레임률이 된다.

    Args:
        times: 영상 프레임의 PTS(틱). 순서는 상관없다
        timescale: 초당 틱 수

    Returns:
        정한 프레임률. 프레임이 둘보다 적거나 PTS가 모두 같으면 None — 간격을 잴 수 없다
    """
    if len(times) < 2 or max(times) <= min(times):
        return None
    measured = Fraction((len(times) - 1) * timescale, max(times) - min(times))
    nearest = min(_STANDARD_FRAME_RATES, key=lambda rate: abs(measured - rate) / rate)
    if abs(measured - nearest) / nearest <= _STANDARD_TOLERANCE:
        return FrameRateChoice(nearest, FPS_STANDARD)
    return FrameRateChoice(measured, FPS_MEASURED)


class SectionSource(Protocol):
    """구간 계획에 세그먼트의 시각과 프레임 정보를 주는 공급자.

    세그먼트의 형식(fMP4 · MPEG-TS)을 아는 쪽이 구현한다. 같은 인덱스로 여러 번 불릴 수
    있다 — 세그먼트를 받아 보관하는 일은 공급자(또는 공급자에 주입된 함수)가 한다.

    "원래 시각"은 공급자가 정한 기준에서 잰 초다. 한 묶음(끊긴 자리 사이) 안에서는
    이어져야 한다. 묶음마다 기준이 달라도 된다 — 계획은 묶음의 첫 세그먼트에서 가장 이른
    시각을 그 묶음의 시작으로 놓는다.
    """

    def frame_rate(self) -> Fraction:
        """VOD의 프레임률 — 계획이 프레임률을 넘겨받지 않았을 때 쓴다."""

    def origin_of(self, index: int) -> Seconds:
        """세그먼트에서 영상·오디오를 통틀어 가장 이른 표시 시각(원래 시각)."""

    def span_of(self, index: int) -> tuple[Seconds, Seconds]:
        """세그먼트가 차지하는 원래 시각 [시작, 끝) — 영상으로 재고, 영상이 없으면 오디오로 잰다."""

    def has_video(self, index: int) -> bool:
        """세그먼트에 영상 프레임이 있는지."""

    def frames_of(self, first: int, last: int, origin: Seconds) -> CutFrames:
        """세그먼트 first~last(포함)의 프레임 정보 — 시각은 (원래 시각 − origin), 곧 VOD 시각이다."""


@dataclass(frozen=True)
class SegmentGroup:
    """플레이리스트에서 끊긴 자리 없이 이어지는 세그먼트 묶음 하나와, 그것이 놓인 VOD 시각을 담는다."""

    first: int  # 첫 세그먼트의 인덱스
    last: int  # 마지막 세그먼트의 인덱스 (포함)
    start: Seconds  # 이 묶음이 시작하는 VOD 시각(초) — 앞 묶음들의 실제 길이의 합
    end: Seconds  # 이 묶음의 마지막 영상 프레임이 끝나는 VOD 시각(초)
    # 시각의 기준 — (세그먼트의 원래 시각 − origin)이 VOD 시각이다
    origin: Seconds
    # 영상 프레임이 든 마지막 세그먼트의 인덱스 — 그 뒤에는 오디오만 든 세그먼트가 있을 수
    # 있다. 구간의 시각은 영상 길이 안이므로 그 뒤의 세그먼트에서는 찾지 않는다
    last_video: int


@dataclass(frozen=True)
class SectionTimeline:
    """VOD의 시각 축 — 묶음마다의 시각과 영상 길이를 담는다. 세그먼트의 실제 시각으로 잰 값이다."""

    groups: tuple[SegmentGroup, ...]  # 플레이리스트 순서대로의 묶음
    duration: float  # 영상 길이(초) — 마지막 영상 프레임이 끝나는 VOD 시각


@dataclass(frozen=True)
class PlannedSection:
    """구간 하나를 받는 데 필요한 세그먼트와, 잘라 낼 프레임의 시각을 담는다."""

    selection: TimeRange  # 요청한 구간(초)
    first_segment: int  # 받을 첫 세그먼트의 인덱스
    last_segment: int  # 받을 마지막 세그먼트의 인덱스 (포함)
    first_pts: float  # 구간 첫 프레임의 PTS(VOD 시각, 초)
    last_pts: float  # 구간 끝 프레임의 PTS(VOD 시각, 초)
    # 시각의 기준 — (세그먼트의 원래 시각 − origin)이 VOD 시각이다.
    # 받은 세그먼트로 프레임 정보를 다시 만들 때 공급자에 넘긴다
    origin: Seconds

    @property
    def segment_count(self) -> int:
        """받을 세그먼트 수."""
        return self.last_segment - self.first_segment + 1


def timeline(playlist: HlsPlaylist, source: SectionSource) -> SectionTimeline:
    """세그먼트의 실제 시각으로 VOD의 시각 축과 영상 길이를 정한다.

    묶음(끊긴 자리 사이)마다 첫 세그먼트와 마지막 세그먼트를 읽는다. 묶음의 길이는
    (마지막 영상 프레임이 끝나는 시각 − 첫 세그먼트의 가장 이른 시각)이고, 묶음은 앞
    묶음이 끝난 VOD 시각에서 시작한다. 영상 길이는 마지막 묶음이 끝나는 시각이다.

    ``#EXTINF``의 합을 길이로 쓰지 않는다 — 실제 길이와 다를 수 있다.

    Raises:
        ValueError: 플레이리스트에 세그먼트가 없는 경우
    """
    if not playlist.segments:
        raise ValueError("플레이리스트에 세그먼트가 없다")
    bounds = [0, *playlist.discontinuities, len(playlist.segments)]
    groups = []
    start: Seconds = Fraction(0)
    for first, after in zip(bounds, bounds[1:]):
        last = after - 1
        origin = source.origin_of(first) - start
        last_video = _last_segment_with_video(source, first, last)
        end = source.span_of(last_video)[1] - origin
        groups.append(
            SegmentGroup(
                first=first, last=last, start=start, end=end, origin=origin, last_video=last_video
            )
        )
        start = end
    return SectionTimeline(groups=tuple(groups), duration=float(groups[-1].end))


def plan_sections(
    playlist: HlsPlaylist,
    source: SectionSource,
    selections: Sequence[TimeRange],
    fps: Fraction | None = None,
    *,
    locate_steps: int | None = None,
) -> tuple[PlannedSection, ...]:
    """구간마다 받을 세그먼트 범위와 첫·끝 프레임을 정한다.

    Args:
        playlist: 미디어 플레이리스트 — 세그먼트 길이(#EXTINF)가 있어야 한다
        source: 세그먼트의 시각과 프레임 정보를 주는 공급자
        selections: 구간 목록. 순서가 구간 번호다
        fps: 구간을 해석한 쪽이 이미 정한 프레임률. 구간의 시각을 만든 프레임률과 같아야
            검증이 같은 프레임 단위로 된다. None이면 공급자가 정한다
        locate_steps: 구간의 시각이 놓인 세그먼트를 찾아가는 최대 걸음 수. None이면
            ``_MAX_LOCATE_STEPS``

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
        fps = source.frame_rate()
    steps = _MAX_LOCATE_STEPS if locate_steps is None else locate_steps
    axis = timeline(playlist, source)
    violations = validate_selections(selections, axis.duration, fps)
    if violations:
        raise SelectionError(violations)
    half_frame = float(1 / fps) / 2

    sections = []
    for number, selection in enumerate(selections):
        group = next((g for g in axis.groups if selection.start < float(g.end)), axis.groups[-1])
        to_end = reaches_end(selection.end, axis.duration, fps)
        # 끊긴 자리를 넘는 구간은 그 구간의 세그먼트를 읽기 전에 거부한다
        crosses = selection.end > float(group.end) + half_frame
        if crosses or (to_end and group is not axis.groups[-1]):
            raise SelectionError({number: (SELECTION_CROSSES_BREAK,)})
        origin = group.origin
        locate = _Locator(number, playlist, source, group, steps)

        cover = locate(selection.start)
        first_segment = max(cover - 1, group.first)  # 앞 키프레임이 앞 세그먼트에 있을 수 있다
        lead = source.frames_of(first_segment, cover, origin)
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
            lead = source.frames_of(first_segment, cover, origin)
        first_frame = snap_to_frame(selection.start, lead.frame_pts, fps, "start")

        if to_end:
            # 끝이 영상 길이와 같다 — 시각으로 고르지 않고 마지막 세그먼트의 마지막 프레임을 쓴다
            high = group.last
            low = _last_segment_with_video(source, first_segment, high)
            tail = source.frames_of(low, high, origin)
            last_frame = len(tail.frame_pts) - 1
        else:
            low = high = max(locate(selection.end), first_segment)
            tail = source.frames_of(low, high, origin)
            padded = False  # 오디오의 끝을 몰라 다음 세그먼트를 이미 넣었는지
            for _ in range(_MAX_WIDEN_STEPS):
                last_frame = snap_to_frame(selection.end, tail.frame_pts, fps, "end")
                starts_later = selection.end < tail.frame_pts[0] - tail.frame_duration / 2
                missing = _tail_gap(tail, last_frame, selection.end)
                if starts_later and low > first_segment:
                    low -= 1  # 끝 프레임이 앞 세그먼트에 있다
                elif missing in ("frame", "audio") and high < group.last:
                    high += 1  # 끝 프레임이나 그 프레임의 오디오가 다음 세그먼트에 있다
                elif missing == "unknown" and not padded and high < group.last:
                    # 오디오가 어디서 끝나는지 모른다 — 끝 프레임이 든 세그먼트의 다음
                    # 세그먼트까지 넣어 그 프레임의 오디오가 들어 있게 한다
                    high += 1
                    padded = True
                else:
                    break
                tail = source.frames_of(low, high, origin)
            last_frame = snap_to_frame(selection.end, tail.frame_pts, fps, "end")

        # 양 끝을 넓히는 걸음은 끊긴 자리에서 멈춘다(first_segment > group.first ·
        # cover < group.last · high < group.last) — 여기까지 온 범위는 끊긴 자리를 넘지 않는다
        sections.append(
            PlannedSection(
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

    영상 프레임이 든 마지막 세그먼트(``SegmentGroup.last_video``)까지만 본다. 그 뒤의
    오디오만 든 세그먼트는 읽지 않는다 — 구간의 시각은 영상 길이 안이라 거기 놓일 수 없고,
    그런 세그먼트는 시각 범위를 구하지 못할 수 있다(오디오가 끝나는 시각을 모르는 입력).
    """

    def __init__(
        self,
        number: int,
        playlist: HlsPlaylist,
        source: SectionSource,
        group: SegmentGroup,
        steps: int,
    ):
        self._number = number  # 구간 번호 — 실패를 어느 구간의 것으로 알릴지
        self._source = source
        self._group = group
        self._steps = steps  # 찾아가는 최대 걸음 수
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
            SelectionError: 정해진 걸음 수 안에 찾지 못한 경우(``SELECTION_NOT_LOCATED``)
        """
        group = self._group
        index = self._clamp(group.first + bisect_right(self._estimates, seconds) - 1)
        visited: set[int] = set()
        for _ in range(self._steps):
            visited.add(index)
            begin, end = self._span(index)
            if seconds < begin and index > group.first:
                jump = min(int((seconds - begin) / max(end - begin, _MIN_SPAN_SECONDS)) - 1, -1)
            elif seconds >= end and index < group.last_video:
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
        return min(max(index, self._group.first), self._group.last_video)

    def _span(self, index: int) -> tuple[float, float]:
        """세그먼트 index가 실제로 차지하는 VOD 시각 [시작, 끝)."""
        begin, end = self._source.span_of(index)
        return float(begin - self._group.origin), float(end - self._group.origin)


def _last_segment_with_video(source: SectionSource, lowest: int, last: int) -> int:
    """last에서 앞으로 가며 영상 프레임이 든 첫 세그먼트를 찾는다. lowest보다 앞으로는 가지 않는다.

    마지막 세그먼트에 오디오만 든 영상이 있다 — 그러면 마지막 프레임은 그 앞 세그먼트에 있다.
    """
    index = last
    while index > lowest and not source.has_video(index):
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


def _tail_gap(
    frames: CutFrames, last_frame: int, end: float
) -> Literal["none", "frame", "audio", "unknown"]:
    """입력의 끝 쪽에 모자란 것을 가린다 — 구간의 끝 프레임과, 그 프레임이 끝날 때까지의 오디오.

    Returns:
        "frame" — 끝 프레임이 이 입력 뒤에 있다 / "audio" — 오디오가 끝 프레임이 끝나기
        전에 끝난다 / "unknown" — 오디오가 있는데 어디서 끝나는지 모른다 / "none" — 모자란
        것이 없다(오디오가 아예 없는 입력도 여기다)
    """
    if end > frames.frame_pts[-1] + frames.frame_duration / 2:
        return "frame"
    if frames.audio_start is None:
        return "none"  # 오디오가 없다
    if frames.audio_end is None:
        return "unknown"
    frame_end = frames.frame_pts[last_frame] + frames.frame_duration
    return "none" if frames.audio_end >= frame_end else "audio"


def _has_tail(frames: CutFrames, last_frame: int, end: float) -> bool:
    """입력에 구간의 끝 프레임과 그 프레임이 끝날 때까지의 오디오가 있는지.

    오디오가 어디서 끝나는지 모르는 입력은 있다고 보지 않는다.
    """
    return _tail_gap(frames, last_frame, end) == "none"
