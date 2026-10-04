"""구간 → 받을 세그먼트와 잘라 낼 프레임 (HLS MPEG-TS) (#309).

암호화 VOD(hls_aes 경로)는 MPEG-TS 세그먼트로 내려온다. 구간 계획을 세우는 방법은
인코딩 전 다시보기(fMP4)와 같아 ``core/utils/section_plan.py``가 한다. 이 모듈은 TS
세그먼트에서 시각과 프레임 정보를 읽어 그 계획에 주는 공급자(``TsSectionSource``)와,
TS의 프레임률 정하기를 맡는다.

fMP4와 다른 점:

- **프레임 길이가 적혀 있지 않다.** VOD 전체에 하나로 정한 프레임률(``choose_ts_frame_rate``)의
  역수를 모든 세그먼트의 프레임 길이로 쓴다. 세그먼트마다 재지 않는다 — 프레임이 하나뿐인
  세그먼트는 잴 간격이 없다
- **타임스탬프가 33비트다.** 세그먼트가 VOD의 어디쯤인지를 플레이리스트의 시각으로 알려
  되감김을 푼다(``build_ts_index``의 ``expected_start``)
- **오디오가 끝나는 시각을 모를 수 있다.** AAC(ADTS)가 아닌 오디오는 PES에 든 프레임 수를
  셀 수 없다. 그때 프레임 정보의 ``audio_end``는 None이고, 계획은 끝 프레임이 든
  세그먼트의 다음 세그먼트까지 넣는다 — 오디오가 없는 것으로 다루지 않는다
- 영상 프레임이 없는 세그먼트(오디오만 든 마지막 세그먼트 등)는 fMP4와 같이 오디오로 잰다

원래 시각의 기준은 묶음(끊긴 자리 사이)마다 그 묶음의 첫 세그먼트에서 가장 이른 PTS다.
시각은 90kHz 틱의 분수로 잰다 — 소수로 더해 가면 영상 길이가 마지막 자리에서 어긋나,
끝을 영상 길이에 꼭 맞춘 구간이 범위 밖으로 판정될 수 있다.

세그먼트는 주입받은 함수로 읽는다 — 이 모듈은 네트워크도 복호화도 모른다. 받은 함수는
**복호화한** 세그먼트를 ``parse_ts``로 읽은 결과를 돌려준다.
"""

from bisect import bisect_right
from collections.abc import Callable, Sequence
from fractions import Fraction
from itertools import accumulate

from core.api.hls import HlsPlaylist
from core.api.mpegts import (
    TS_CLOCK,
    TS_INVALID,
    TS_UNSUPPORTED,
    TsError,
    build_ts_index,
    join_ts_streams,
    ts_audio_span,
    ts_origin,
)
from core.models.cut import CutFrames
from core.models.plan import TimeRange
from core.models.ts_index import TsStreams
from core.utils.section_plan import (
    FPS_DECLARED,
    FrameRateChoice,
    PlannedSection,
    SectionTimeline,
    measured_frame_rate,
    plan_sections,
    timeline,
)


def choose_ts_frame_rate(
    segments: Sequence[TsStreams], declared: Fraction | None = None
) -> FrameRateChoice:
    """프레임률을 정한다 — 선언값, 없으면 읽은 프레임의 평균 간격.

    정하는 순서는 fMP4(``core.utils.fmp4_sections.choose_frame_rate``)와 같다.

    1. ``declared``가 있으면 그 값 그대로다 (``FPS_DECLARED``)
    2. 없으면 segments의 영상 프레임 전체에서 평균 간격을 재고, 표준 비율과 0.1% 안이면
       그 표준 비율(``FPS_STANDARD``), 아니면 잰 값 그대로다(``FPS_MEASURED``)

    TS에는 프레임 길이가 적혀 있지 않아, 프레임이 하나뿐이면 정할 수 없다.

    Args:
        segments: 프레임 간격을 잴 세그먼트(``parse_ts``의 결과). 보통 첫 세그먼트 하나다.
            여럿이면 이어지는 세그먼트를 순서대로 준다
        declared: 매니페스트가 선언한 프레임률. 없으면 None

    Raises:
        ValueError: 선언값도 없고 segments의 영상 프레임이 둘보다 적은 경우
    """
    if declared is not None and declared > 0:
        return FrameRateChoice(declared, FPS_DECLARED)
    joined = join_ts_streams(segments)
    if joined.video_pts:
        index = build_ts_index(joined, ts_origin(joined))
        times = [round(pts * TS_CLOCK) for pts in index.frame_pts]
        measured = measured_frame_rate(times, TS_CLOCK)
        if measured is not None:
            return measured
    raise ValueError("프레임률을 정할 수 없다 — 영상 프레임이 둘보다 적다")


class TsSectionSource:
    """TS 세그먼트에서 구간 계획에 줄 시각과 프레임 정보를 읽는 공급자.

    세그먼트는 주입받은 함수로 읽는다 — 부를 때마다 그 함수를 부른다(보관은 주는 쪽이
    한다). 묶음의 기준을 알려고 그 묶음의 첫 세그먼트도 함께 읽는다.
    """

    def __init__(
        self,
        playlist: HlsPlaylist,
        segment_at: Callable[[int], TsStreams],
        fps: Fraction,
    ):
        """
        Args:
            playlist: 미디어 플레이리스트 — 세그먼트 길이(#EXTINF)와 끊긴 자리를 쓴다
            segment_at: 세그먼트 인덱스를 받아 그 세그먼트(복호화한 것)의 ``parse_ts``
                결과를 돌려주는 함수
            fps: VOD 전체의 프레임률(``choose_ts_frame_rate``). 이 값의 역수가 모든
                세그먼트의 프레임 길이다
        """
        self._segment_at = segment_at
        self._fps = fps
        self._frame_duration = float(1 / fps)
        # 세그먼트마다 플레이리스트가 말하는 시작 시각(#EXTINF 누적) — 되감김을 풀 때의 추정
        self._starts = [0.0, *accumulate(playlist.durations)]
        self._group_starts = [0, *playlist.discontinuities]  # 묶음마다 첫 세그먼트의 인덱스

    def frame_rate(self) -> Fraction:
        """VOD 전체의 프레임률."""
        return self._fps

    def origin_of(self, index: int) -> Fraction:
        """세그먼트에서 영상·오디오를 통틀어 가장 이른 PTS(원래 시각).

        Raises:
            TsError: 세그먼트에 영상 프레임도 오디오 PES도 없는 경우(``TS_INVALID``)
        """
        video, audio = self._spans(index)
        starts = [span[0] for span in (video, audio) if span is not None]
        if not starts:
            raise TsError(TS_INVALID, "세그먼트에 영상도 오디오도 없다")
        return min(starts)

    def span_of(self, index: int) -> tuple[Fraction, Fraction]:
        """세그먼트가 차지하는 원래 시각 [시작, 끝) — 영상으로 재고, 영상이 없으면 오디오로 잰다.

        영상의 끝은 (가장 늦은 PTS + VOD 전체의 프레임 길이)다.

        Raises:
            TsError: 영상도 오디오도 없는 경우(``TS_INVALID``), 영상이 없는데 오디오가
                끝나는 시각을 모르는 경우(``TS_UNSUPPORTED``)
        """
        video, audio = self._spans(index)
        if video is not None:
            return video
        if audio is None:
            raise TsError(TS_INVALID, "세그먼트에 영상도 오디오도 없다")
        if audio[1] is None:
            raise TsError(TS_UNSUPPORTED, "영상이 없는 세그먼트의 오디오가 끝나는 시각을 모른다")
        return audio[0], audio[1]

    def has_video(self, index: int) -> bool:
        """세그먼트에 영상 프레임이 있는지."""
        return bool(self._segment_at(index).video_pts)

    def frames_of(self, first: int, last: int, origin: Fraction | float) -> CutFrames:
        """세그먼트 first~last(포함)의 프레임 정보 — 시각은 (원래 시각 − origin), 곧 VOD 시각이다.

        오디오가 있는데 끝나는 시각을 모르면 ``audio_start``는 값이 있고 ``audio_end``는
        None이다.

        Raises:
            TsError: 그 범위에 영상 프레임이 하나도 없는 경우(``TS_INVALID``)
        """
        joined = join_ts_streams([self._segment_at(index) for index in range(first, last + 1)])
        index = build_ts_index(joined, self._base(first), expected_start=self._expected(first))

        def shifted(seconds: float) -> float:
            return float(_exact(seconds) - origin)

        return CutFrames(
            frame_pts=tuple(shifted(pts) for pts in index.frame_pts),
            frame_dts=tuple(shifted(index.decode_times[sample]) for sample in index.frame_samples),
            keyframes=index.keyframes,
            timescale=TS_CLOCK,
            frame_duration=self._frame_duration,
            audio_start=shifted(min(index.audio_pts)) if index.audio_pts else None,
            audio_end=float(Fraction(index.audio_end) - origin)
            if index.audio_end is not None
            else None,
        )

    def _group_first(self, index: int) -> int:
        """세그먼트 index가 든 묶음의 첫 세그먼트."""
        return self._group_starts[bisect_right(self._group_starts, index) - 1]

    def _base(self, index: int) -> int:
        """세그먼트 index가 든 묶음의 기준 — 그 묶음의 첫 세그먼트에서 가장 이른 PTS(원시 틱)."""
        return ts_origin(self._segment_at(self._group_first(index)))

    def _expected(self, index: int) -> float:
        """세그먼트 index가 묶음의 시작에서 얼마나 뒤인지 — 플레이리스트의 추정(초)."""
        return self._starts[index] - self._starts[self._group_first(index)]

    def _spans(
        self, index: int
    ) -> tuple[tuple[Fraction, Fraction] | None, tuple[Fraction, Fraction | None] | None]:
        """세그먼트의 (영상 범위, 오디오 범위) — 원래 시각. 없는 쪽은 None이다.

        영상의 끝은 (가장 늦은 PTS + VOD 전체의 프레임 길이)다. 오디오의 끝은 모르면 None이다.
        """
        streams = self._segment_at(index)
        base, expected = self._base(index), self._expected(index)
        heard = ts_audio_span(streams, base, expected)
        audio = None
        if heard is not None:
            # 오디오가 끝나는 시각은 틱에 맞지 않을 수 있다(표본화율에 따라) — 소수 그대로 옮긴다
            audio = (_exact(heard[0]), Fraction(heard[1]) if heard[1] is not None else None)
        if not streams.video_pts:
            return None, audio
        shown = build_ts_index(streams, base, expected).frame_pts
        # 세그먼트에서 잰 프레임 길이를 쓰지 않는다 — 프레임이 하나뿐이면 잰 값이 없다
        return (_exact(shown[0]), _exact(shown[-1]) + 1 / self._fps), audio


def _exact(seconds: float) -> Fraction:
    """색인의 시각(틱 ÷ 90,000을 소수로 옮긴 값)을 틱의 분수로 되살린다."""
    return Fraction(round(seconds * TS_CLOCK), TS_CLOCK)


def ts_timeline(
    playlist: HlsPlaylist, segment_at: Callable[[int], TsStreams], fps: Fraction
) -> SectionTimeline:
    """세그먼트의 실제 시각으로 VOD의 시각 축과 영상 길이를 정한다.

    VOD의 0초는 첫 세그먼트에서 영상·오디오를 통틀어 가장 이른 PTS이고, 영상 길이는 마지막
    영상 프레임이 끝나는 시각이다.

    Raises:
        TsError: 세그먼트에서 시각을 읽지 못한 경우
        ValueError: 플레이리스트에 세그먼트가 없는 경우
    """
    return timeline(playlist, TsSectionSource(playlist, segment_at, fps))


def plan_ts_sections(
    playlist: HlsPlaylist,
    selections: Sequence[TimeRange],
    segment_at: Callable[[int], TsStreams],
    fps: Fraction,
) -> tuple[PlannedSection, ...]:
    """구간마다 받을 세그먼트 범위와 첫·끝 프레임을 정한다.

    Args:
        playlist: 미디어 플레이리스트 — 세그먼트 길이(#EXTINF)가 있어야 한다
        selections: 구간 목록. 순서가 구간 번호다
        segment_at: 세그먼트 인덱스를 받아 그 세그먼트(복호화한 것)의 ``parse_ts`` 결과를
            돌려주는 함수. 같은 인덱스로 여러 번 불린다 — 주는 쪽이 보관한다
        fps: VOD 전체의 프레임률(``choose_ts_frame_rate``). 구간의 시각을 만든 프레임률과
            같아야 한다

    Raises:
        SelectionError: 구간이 검증을 통과하지 못했거나, 녹화가 끊긴 자리를 넘거나, 구간의
            시각이 놓인 세그먼트를 찾지 못한 경우
        TsError: 세그먼트에서 시각을 읽지 못한 경우
        ValueError: 플레이리스트에 세그먼트 길이가 없는 경우
    """
    return plan_sections(playlist, TsSectionSource(playlist, segment_at, fps), selections, fps)
