"""구간 → 받을 세그먼트 범위 (#309).

HLS 플레이리스트의 세그먼트 길이(``#EXTINF``)를 누적해, 구간을 잘라 내는 데 필요한
세그먼트가 몇 번째부터 몇 번째까지인지 계산한다. 플레이리스트만 보고 정하므로
세그먼트를 받기 전에 답이 나온다.

**구간이 놓인 세그먼트보다 하나 앞의 세그먼트부터 받는다.** 플레이리스트에는
키프레임이 어디 있는지 적혀 있지 않다. 구간의 첫 프레임을 디코드하려면 그 앞의
키프레임부터 필요한데, 구간이 놓인 세그먼트가 키프레임으로 시작한다는 보장이
없다 — 실제 VOD에는 길이가 고르지 않은 GOP가 섞여 있었다. 하나 앞의 세그먼트를
함께 받으면:

- 구간 시작 앞의 키프레임이 앞 세그먼트에 있어도 받은 범위 안에 들어온다
- 오디오(AAC)는 첫 프레임을 깨끗하게 디코드하려면 앞 프레임이 하나 필요한데,
  그 프레임도 받은 범위 안에 들어온다
- 플레이리스트의 누적 시각과 세그먼트 안의 실제 PTS가 조금 달라도 시작 쪽은
  세그먼트 하나만큼 여유가 있다

구간이 첫 세그먼트에 놓이면 더 앞이 없으므로 첫 세그먼트부터 받는다.
"""

from bisect import bisect_right
from itertools import accumulate

from core.api.hls import HlsPlaylist
from core.models.plan import TimeRange
from core.models.ts_index import SegmentSpan


def selection_segments(playlist: HlsPlaylist, selection: TimeRange) -> SegmentSpan:
    """구간을 잘라 내는 데 필요한 세그먼트 인덱스 범위를 구한다.

    세그먼트 i는 플레이리스트 시각 [i 앞 세그먼트 길이의 합, 거기에 자기 길이를 더한
    값)을 차지한다. 구간의 시작 시각과 끝 시각이 놓인 세그먼트를 찾고(끝 시각의
    프레임도 받는다), 시작 쪽은 하나 앞의 세그먼트부터 받는다.

    구간의 끝이 플레이리스트 길이를 넘으면 마지막 세그먼트까지로 한다.

    Raises:
        ValueError: 플레이리스트에 세그먼트 길이가 없거나, 구간의 시작이 플레이리스트
            길이와 같거나 뒤인 경우
    """
    if not playlist.durations or len(playlist.durations) != len(playlist.segments):
        raise ValueError("플레이리스트에 세그먼트 길이(#EXTINF)가 없다")
    starts = [0.0, *accumulate(playlist.durations)]  # starts[i] = 세그먼트 i의 시작 시각
    total = starts.pop()
    if selection.start >= total:
        raise ValueError(f"구간의 시작({selection.start})이 플레이리스트 길이({total}) 밖이다")
    last_index = len(playlist.segments) - 1
    cover_first = max(bisect_right(starts, selection.start) - 1, 0)
    cover_last = min(bisect_right(starts, selection.end) - 1, last_index)
    return SegmentSpan(
        first=max(cover_first - 1, 0),
        last=cover_last,
        cover_first=cover_first,
        cover_last=cover_last,
    )
