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

계획을 세우는 방법(추정 → 실제 시각 확인 → 넓히기)은 세그먼트의 형식과 무관해
``core/utils/section_plan.py``에 있다. 이 모듈은 fMP4 세그먼트에서 시각과 프레임 정보를
읽어 그 계획에 주는 공급자(``_Fmp4Source``)와, fMP4의 프레임률 정하기를 맡는다. 위의
설명은 그 계획이 fMP4 입력에서 하는 일이다.

세그먼트의 프레임 정보는 주입받은 함수로 읽는다 — 이 모듈은 네트워크를 모른다.
"""

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fractions import Fraction

from core.api.fmp4 import _seconds, build_fmp4_index, fmp4_origin
from core.api.hls import HlsPlaylist
from core.api.mp4 import MP4_INVALID, Mp4Error
from core.models.cut import CutFrames
from core.models.fmp4_index import Fmp4Init, Fmp4Segment
from core.models.plan import TimeRange
from core.utils.hybrid_cut import cut_frames_from_fmp4
from core.utils.section_plan import (
    FPS_DECLARED,
    FPS_MEASURED,
    FPS_STANDARD,
    FrameRateChoice,
    measured_frame_rate,
    plan_sections,
    timeline,
)

# 구간의 시각이 놓인 세그먼트를 찾아가는 최대 걸음 수. 걸음마다 세그먼트 하나를 통째로
# 받는다. 추정이 세그먼트 여럿만큼 어긋나도 그 세그먼트의 실제 시작과 길이로 다시
# 추정하면 한두 걸음에 닿는다 — 이 횟수로 못 찾으면 플레이리스트와 세그먼트가 맞지 않는
# 것이라 실패로 끝낸다. plan_fmp4_sections가 부를 때마다 이 값을 읽어 계획에 넘긴다
_MAX_LOCATE_STEPS = 6

__all__ = [
    "FPS_DECLARED",
    "FPS_MEASURED",
    "FPS_STANDARD",
    "Fmp4Group",
    "Fmp4Section",
    "Fmp4Timeline",
    "FrameRateChoice",
    "choose_frame_rate",
    "fmp4_timeline",
    "plan_fmp4_sections",
]


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
    measured = measured_frame_rate(times, timescale)
    if measured is not None:
        return measured
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


class _Fmp4Source:
    """fMP4 세그먼트에서 구간 계획에 줄 시각과 프레임 정보를 읽는 공급자.

    원래 시각은 초기화 세그먼트의 timescale로 나눈 분수(초)다. 세그먼트는 주입받은 함수로
    읽는다 — 부를 때마다 그 함수를 한 번씩 부른다(보관은 주는 쪽이 한다).
    """

    def __init__(self, init: Fmp4Init, segment_at: Callable[[int], Fmp4Segment]):
        self._init = init
        self._segment_at = segment_at

    def frame_rate(self) -> Fraction:
        """첫 세그먼트로 정한 프레임률."""
        return choose_frame_rate(self._init, [self._segment_at(0)]).rate

    def origin_of(self, index: int) -> Fraction:
        """세그먼트에서 영상·오디오를 통틀어 가장 이른 표시 시각."""
        return fmp4_origin(self._init, self._segment_at(index))

    def span_of(self, index: int) -> tuple[Fraction, Fraction]:
        """세그먼트의 영상(없으면 오디오)이 차지하는 원래 시각."""
        return _original_span(self._init, self._segment_at(index))

    def has_video(self, index: int) -> bool:
        """세그먼트에 영상 샘플이 있는지."""
        return bool(self._segment_at(index).video.presentation_times)

    def frames_of(self, first: int, last: int, origin: Fraction) -> CutFrames:
        """세그먼트 first~last의 프레임 정보 — 시각은 VOD 시각이다."""
        segments = [self._segment_at(index) for index in range(first, last + 1)]
        return cut_frames_from_fmp4(
            self._init, segments, build_fmp4_index(self._init, segments, origin)
        )


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
    axis = timeline(playlist, _Fmp4Source(init, segment_at))
    groups = tuple(
        Fmp4Group(first=g.first, last=g.last, start=g.start, end=g.end, origin=g.origin)
        for g in axis.groups
    )
    return Fmp4Timeline(groups=groups, duration=axis.duration)


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
    planned = plan_sections(
        playlist,
        _Fmp4Source(init, segment_at),
        selections,
        fps,
        locate_steps=_MAX_LOCATE_STEPS,  # 부를 때 모듈 전역에서 읽는다
    )
    return tuple(
        Fmp4Section(
            selection=section.selection,
            first_segment=section.first_segment,
            last_segment=section.last_segment,
            first_pts=section.first_pts,
            last_pts=section.last_pts,
            origin=section.origin,
        )
        for section in planned
    )


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
